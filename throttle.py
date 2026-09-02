#!/usr/bin/env python3
"""
Serializes Discord write calls (message.edit/channel.send) per channel, so
many simultaneously-polling trackers in one busy channel queue up instead of
all firing PATCH/POST at once and colliding on Discord's shared per-channel
rate-limit bucket. Confirmed live: without this, a tracker's edit could lose
that race indefinitely - discord.py retries 429s forever with no cap - while
dozens of other trackers in the same channel kept the bucket saturated,
leaving that one card silently stuck for over an hour.

Concurrency-limiting alone (_MAX_CONCURRENT_WRITES_PER_CHANNEL) caps how many
requests are ever in flight at once, but says nothing about how FAST they're
sent - two trackers waiting on the same 2-slot semaphore can still both fire
the instant a slot frees up, and with enough trackers polling one busy
channel that's still often enough to trip Discord's bucket and kick off a
429-retry cascade (confirmed live: a channel with just 12 active trackers
produced dozens of 429s within two minutes, visibly stalling card updates -
and parlaytracker's own summary card repeatedly found its message already
gone by the time an edit landed, since Discord's real 5-requests-per-5-
seconds channel bucket was already saturated well before the semaphore's own
2 concurrent slots ran out). _wait_for_rate_slot below rations requests to
stay under that bucket proactively, instead of only reacting to 429s after
they've already happened.
"""

import asyncio
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

# How many writes to the same channel are allowed in flight at once. 1 (not
# 2) - a call whose OWN request already got a 429 keeps retrying it
# internally inside discord.py, invisible to _wait_for_rate_slot below (that
# only gates when a NEW call is dispatched, not how many times discord.py
# retries one already in flight) - with 2 concurrent slots, a second call
# could still get dispatched (and 429'd) while the first is mid-retry,
# adding to an already-saturated bucket instead of waiting it out. At 1, a
# stuck retry fully blocks every other write to this channel until it
# resolves, which is exactly the backpressure needed to let the bucket
# drain instead of compounding it further.
_MAX_CONCURRENT_WRITES_PER_CHANNEL = 1

_semaphores: dict[int, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(_MAX_CONCURRENT_WRITES_PER_CHANNEL)
)

# Well under Discord's real channel-scoped write bucket - confirmed live
# that 3-per-5s (the first, more optimistic budget tried here) still let a
# 12-tracker channel produce dozens of 429s within minutes, so this errs a
# lot more conservative rather than guessing again.
_MAX_REQUESTS_PER_WINDOW = 1
_WINDOW_SECONDS = 3.0

_recent_requests: dict[int, deque] = defaultdict(deque)
_rate_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _wait_for_rate_slot(channel_id: int):
    """Blocks until sending one more request to this channel would stay
    within the rolling window - a simple sliding-window rate limiter, not
    just a concurrency cap. Locked per channel so two callers racing to
    claim "the next slot" can't both read the same not-yet-full window and
    both proceed."""
    async with _rate_locks[channel_id]:
        q = _recent_requests[channel_id]
        while True:
            now = time.monotonic()
            while q and now - q[0] > _WINDOW_SECONDS:
                q.popleft()
            if len(q) < _MAX_REQUESTS_PER_WINDOW:
                q.append(now)
                return
            await asyncio.sleep(q[0] + _WINDOW_SECONDS - now)


async def run(channel_id: int, func: Callable[[], Awaitable[T]]) -> T:
    """Runs func() (a zero-arg callable returning an awaitable) under this
    channel's semaphore, after waiting for a free rate-limit slot."""
    async with _semaphores[channel_id]:
        await _wait_for_rate_slot(channel_id)
        return await func()

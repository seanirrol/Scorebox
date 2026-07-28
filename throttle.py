#!/usr/bin/env python3
"""
Serializes Discord write calls (message.edit/channel.send) per channel, so
many simultaneously-polling trackers in one busy channel queue up instead of
all firing PATCH/POST at once and colliding on Discord's shared per-channel
rate-limit bucket. Confirmed live: without this, a tracker's edit could lose
that race indefinitely - discord.py retries 429s forever with no cap - while
dozens of other trackers in the same channel kept the bucket saturated,
leaving that one card silently stuck for over an hour.
"""

import asyncio
from collections import defaultdict
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

# How many writes to the same channel are allowed in flight at once. Keeping
# this low means a call stuck retrying a 429 blocks new attempts from piling
# on top of it, instead of every tracker hammering the same saturated bucket.
_MAX_CONCURRENT_WRITES_PER_CHANNEL = 2

_semaphores: dict[int, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(_MAX_CONCURRENT_WRITES_PER_CHANNEL)
)


async def run(channel_id: int, func: Callable[[], Awaitable[T]]) -> T:
    """Runs func() (a zero-arg callable returning an awaitable) under this
    channel's semaphore."""
    async with _semaphores[channel_id]:
        return await func()

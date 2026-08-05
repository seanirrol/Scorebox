#!/usr/bin/env python3
"""
scores365.find_soccer_player() only searches matches that are live or
kicking off within the next 2 hours (see its own docstring) - a soccer prop
pick posted further ahead of kickoff than that has no candidate match yet
and would otherwise just be silently dropped as "player not found".

This module queues that pick and retries the lookup periodically until the
match's kickoff enters the 2-hour search window (at which point the retry
succeeds exactly like a normal auto-track would have) or a bounded wait
elapses without ever finding a match (typo'd name, or the match never
materializes). The queue is persisted so a bot restart mid-wait doesn't lose
it - same approach as pendingdelete.py, and for the same reason: an
in-memory-only asyncio.sleep can't survive a deploy.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable

import state

log = logging.getLogger("scorebox.pendingsoccerprops")

# Comfortably shorter than the 2h search window, so a pick doesn't sit
# resolved-but-unclaimed for long once its match actually enters it.
RETRY_INTERVAL_SECONDS = 30 * 60
# Picks are virtually always same-day, but this covers a pick dropped up to
# two days ahead of kickoff without retrying forever on a bad name.
MAX_WAIT_SECONDS = 48 * 3600


def _persist(entry_id: str, entry: dict):
    data = state.load_pending_soccer_props()
    data[entry_id] = entry
    state.save_pending_soccer_props(data)


def _forget(entry_id: str):
    data = state.load_pending_soccer_props()
    data.pop(entry_id, None)
    state.save_pending_soccer_props(data)


def list_pending() -> list[dict]:
    """Every soccer prop pick still waiting for its match to enter the
    lookup window."""
    return list(state.load_pending_soccer_props().values())


async def _retry_loop(entry_id: str, entry: dict, resolve: Callable[[dict], Awaitable[bool]]):
    expires_at = entry["queued_at"] + MAX_WAIT_SECONDS
    while True:
        remaining = expires_at - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(RETRY_INTERVAL_SECONDS, remaining))
        try:
            found = await resolve(entry)
        except Exception:
            log.exception("Pending soccer prop resolve callback failed for %s %s", entry.get("player"), entry.get("stat"))
            found = False
        if found:
            _forget(entry_id)
            return
    _forget(entry_id)
    log.info("Gave up waiting for soccer prop pick to resolve: %s %s", entry.get("player"), entry.get("stat"))


def queue(
    channel_id: int, player: str, stat: str, stat_name: str, direction, line,
    section, label, origin_channel_id, resolve: Callable[[dict], Awaitable[bool]],
) -> dict:
    """Called right after a fresh auto-track attempt's find_soccer_player
    comes back empty. stat_name is the already-validated catalog key (kept
    separate from stat, the raw pick text, so the retry never has to
    re-derive it). resolve is awaited on every retry and on resume_all -
    return True once the pick has been fully posted/tracked so the entry
    can be dropped from the queue."""
    entry_id = f"{channel_id}:{player}:{stat}:{int(time.time() * 1000)}"
    entry = {
        "channel_id": channel_id, "player": player, "stat": stat, "stat_name": stat_name,
        "direction": direction, "line": line, "section": section, "label": label,
        "origin_channel_id": origin_channel_id, "queued_at": time.time(),
    }
    _persist(entry_id, entry)
    asyncio.create_task(_retry_loop(entry_id, entry, resolve))
    return entry


async def resume_all(resolve: Callable[[dict], Awaitable[bool]]):
    """Called once from on_ready. Re-arms every still-pending lookup so a
    restart doesn't reset (or lose) its wait."""
    for entry_id, entry in list(state.load_pending_soccer_props().items()):
        asyncio.create_task(_retry_loop(entry_id, entry, resolve))
        log.info("Resumed pending soccer prop lookup: %s %s", entry.get("player"), entry.get("stat"))

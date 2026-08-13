#!/usr/bin/env python3
"""
scores365.find_match_for_team()'s bulk "current games" feed (see
_fetch_games_for_sport) has its own hard pagination limit, independent of
how far away a match's kickoff actually is - confirmed live, a real,
correctly-scheduled tennis match (~14 hours out) never appeared in the feed
at all, despite existing and being directly fetchable by game id, simply
because the bulk feed's fixed forward/backward page walk doesn't reach every
match once a sport has enough concurrent games (tennis, many tournaments
running at once). A fresh /track-style pick posted while its match is stuck
outside that window would otherwise just be silently dropped as "no match
found", even though the match is perfectly real.

This module queues that pick and retries the lookup periodically until the
match's game rotates into the feed's reachable window (at which point the
retry succeeds exactly like a normal auto-track would have) or a bounded
wait elapses without ever finding one (typo'd team name, or the match never
materializes) - same pattern as pendingsoccerprops.py, which already solves
the analogous gap in scores365.find_soccer_player's own 2-hour search
window. The queue is persisted so a bot restart mid-wait doesn't lose it.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import state

log = logging.getLogger("scorebox.pendingtrack")

RETRY_INTERVAL_SECONDS = 30 * 60
# A missing-from-the-feed match is expected to resolve itself well within a
# day as the bulk feed's window rotates - this just bounds the wait so a
# genuinely bad team name doesn't retry forever.
MAX_WAIT_SECONDS = 48 * 3600


def _persist(entry_id: str, entry: dict):
    data = state.load_pending_track()
    data[entry_id] = entry
    state.save_pending_track(data)


def _forget(entry_id: str):
    data = state.load_pending_track()
    data.pop(entry_id, None)
    state.save_pending_track(data)


def list_pending() -> list[dict]:
    """Every /track-style pick still waiting for its match to rotate into
    the bulk feed's reachable window."""
    return list(state.load_pending_track().values())


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
            log.exception("Pending track resolve callback failed for %s", entry.get("team"))
            found = False
        if found:
            _forget(entry_id)
            return
    _forget(entry_id)
    log.info("Gave up waiting for track pick to resolve: %s", entry.get("team"))


def queue(
    channel_id: int, sport_value: str, team: str,
    total_direction: Optional[str], total_line: Optional[float], team_total: Optional[str],
    section, label, origin_channel_id, resolve: Callable[[dict], Awaitable[bool]],
) -> dict:
    """Called right after a fresh auto-track attempt's find_match_for_team
    comes back empty. resolve is awaited on every retry and on resume_all -
    return True once the pick has been fully posted/tracked so the entry can
    be dropped from the queue."""
    entry_id = f"{channel_id}:{team}:{sport_value}:{int(time.time() * 1000)}"
    entry = {
        "channel_id": channel_id, "sport_value": sport_value, "team": team,
        "total_direction": total_direction, "total_line": total_line, "team_total": team_total,
        "section": section, "label": label, "origin_channel_id": origin_channel_id, "queued_at": time.time(),
    }
    _persist(entry_id, entry)
    asyncio.create_task(_retry_loop(entry_id, entry, resolve))
    return entry


async def resume_all(resolve: Callable[[dict], Awaitable[bool]]):
    """Called once from on_ready. Re-arms every still-pending lookup so a
    restart doesn't reset (or lose) its wait."""
    for entry_id, entry in list(state.load_pending_track().items()):
        asyncio.create_task(_retry_loop(entry_id, entry, resolve))
        log.info("Resumed pending track lookup: %s", entry.get("team"))

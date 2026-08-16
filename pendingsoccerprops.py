#!/usr/bin/env python3
"""
scores365.find_soccer_player() only searches matches kicking off within
_SOCCER_PROP_SEARCH_WINDOW_SECONDS (see its own docstring) - a soccer prop
pick posted further ahead of kickoff than that has no candidate match yet
and would otherwise just be silently dropped as "player not found".

This module queues that pick and retries the lookup periodically until the
match's kickoff enters that search window (at which point the retry
succeeds exactly like a normal auto-track would have) or the day it was
queued ends (Eastern midnight) without ever finding a match (typo'd name,
or the match never materializes) - a pick is always about the day it was
posted, so it's voided rather than left silently retrying into the next
day.

A queued pick gets its own placeholder dailylog entry the moment it's
queued (keyed on the same channel:player:stat:queued-at-ms id used
internally), so it shows up in /summary as pending instead of being
invisible for however long it takes to resolve - confirmed live, a picks
poster had no way to tell "still waiting on this one" apart from "silently
dropped" without checking the log channel by hand. Once the real lookup
succeeds, the normal event-id-keyed entry it creates takes over and this
placeholder is deleted; if it times out instead, the exact same
placeholder is updated to void (with a reason) rather than replaced. The
queue is persisted so a bot restart mid-wait doesn't lose it - same
approach as pendingdelete.py, and for the same reason: an in-memory-only
asyncio.sleep can't survive a deploy.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import dailylog
import scores365
import state

log = logging.getLogger("scorebox.pendingsoccerprops")

# Comfortably shorter than the search window, so a pick doesn't sit
# resolved-but-unclaimed for long once its match actually enters it.
RETRY_INTERVAL_SECONDS = 30 * 60


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


def is_queued(channel_id: int, player: str, stat: str, stat_name: str, direction, line) -> bool:
    """True if an equivalent pick is already sitting in this queue - see
    pendingtrack.py's identical guard for why this matters. Doubly worth
    avoiding here specifically: queue() also creates a placeholder dailylog
    "pending" entry immediately, so an unchecked duplicate would show up
    twice in /summary until one side resolves (or times out and voids)."""
    for entry in state.load_pending_soccer_props().values():
        if (
            entry["channel_id"] == channel_id and entry["player"] == player and entry["stat"] == stat
            and entry["stat_name"] == stat_name and entry["direction"] == direction and entry["line"] == line
        ):
            return True
    return False


def _give_up(entry_id: str, entry: dict):
    """Voids the placeholder dailylog entry queue() already created for a
    pick that never resolved before its day ended - updated in place
    rather than created fresh here, same as any other tracker grading its
    own already-logged pending pick."""
    dailylog.record_result(
        entry["channel_id"], "pendingsoccerprops", entry_id, "void",
        "Match not found before end of day",
    )
    _forget(entry_id)
    log.info("Gave up waiting for soccer prop pick to resolve: %s %s", entry.get("player"), entry.get("stat"))


async def _retry_loop(entry_id: str, entry: dict, resolve: Callable[[dict], Awaitable[bool]]):
    # Two Eastern-midnight rollovers out, not one - confirmed live, a pick
    # queued at 10:31 PM (for a match the next day, a completely normal
    # "posted the night before" pattern) got voided within minutes of a
    # restart because "midnight after the day it was queued" had already
    # passed by the time the bot came back up, hours before the match it
    # was actually about even kicked off. Giving it through the END of the
    # day AFTER it was queued covers that same-night-for-tomorrow case
    # without meaningfully weakening the safety net for a pick that
    # genuinely never resolves (typo'd name, match never materializes).
    expires_at = scores365.next_eastern_midnight_epoch(scores365.next_eastern_midnight_epoch(entry["queued_at"]))
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
            # resolve() already created the real, event-id-keyed dailylog
            # entry (same path a normal auto-track uses) - drop the
            # placeholder queue() logged at queue time so it doesn't linger
            # as a stale duplicate "pending" line in /summary forever.
            dailylog.forget(entry["channel_id"], "pendingsoccerprops", entry_id)
            _forget(entry_id)
            return
    _give_up(entry_id, entry)


def queue(
    channel_id: int, player: str, stat: str, stat_name: str, direction, line,
    section, label, origin_channel_id, resolve: Callable[[dict], Awaitable[bool]],
    queued_detail: str = "Queued - waiting for match lineup to be published",
    extra: Optional[dict] = None,
) -> dict:
    """Called right after a fresh auto-track attempt comes back empty -
    either find_soccer_player itself found nothing yet, or (extra passed)
    the player WAS found but a second, stat-specific lookup (e.g.
    playerstats.football, see bot.py's _resolve_soccer_psf_match) came back
    empty. stat_name is the already-validated catalog key (kept separate
    from stat, the raw pick text, so the retry never has to re-derive it).
    extra is merged into the persisted entry as-is - lets a retry scoped to
    just that second lookup carry whatever it needs (game id, resolved
    player id, ...) without find_soccer_player-specific fields leaking into
    every entry. resolve is awaited on every retry and on resume_all -
    return True once the pick has been fully posted/tracked so the entry
    can be dropped from the queue."""
    entry_id = f"{channel_id}:{player}:{stat}:{int(time.time() * 1000)}"
    entry = {
        "channel_id": channel_id, "player": player, "stat": stat, "stat_name": stat_name,
        "direction": direction, "line": line, "section": section, "label": label,
        "origin_channel_id": origin_channel_id, "queued_at": time.time(),
    }
    if extra:
        entry.update(extra)
    _persist(entry_id, entry)
    dailylog.record_pick(
        channel_id, "pendingsoccerprops", entry_id, section, label,
        message_id=0, origin_channel_id=origin_channel_id, sport="Soccer",
    )
    dailylog.touch(channel_id, "pendingsoccerprops", entry_id, queued_detail)
    asyncio.create_task(_retry_loop(entry_id, entry, resolve))
    return entry


async def resume_all(resolve: Callable[[dict], Awaitable[bool]]):
    """Called once from on_ready. Re-arms every still-pending lookup so a
    restart doesn't reset (or lose) its wait."""
    for entry_id, entry in list(state.load_pending_soccer_props().items()):
        asyncio.create_task(_retry_loop(entry_id, entry, resolve))
        log.info("Resumed pending soccer prop lookup: %s %s", entry.get("player"), entry.get("stat"))

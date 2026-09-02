#!/usr/bin/env python3
"""
Generic queue-and-retry for any auto-track pick kind whose first lookup came
back empty - same gap pendingtrack.py/pendingsoccerprops.py already solve
for their own kinds (a match/player briefly missing from a bulk feed, not a
real "doesn't exist"), extended here to every other kind that previously
just gave up immediately on a miss: F5, 1H, tennis extra-markets, tennis
props, YRFI/NRFI, 1st-inning-result, ESPN player props.

Kept generic (one module instead of one queue per kind) since every one of
these kinds needs the exact same retry/persist/resume shape, just with a
different resolve callback and payload. `kind` is a plain string tag (e.g.
"f5", "tennis_market") registered against its resolve callback in bot.py's
on_ready via resume_all - see pendingtrack.py's own docstring for the full
"why a queue at all" reasoning, unchanged here.
"""

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

import state

log = logging.getLogger("scorebox.pendingauto")

RETRY_INTERVAL_SECONDS = 30 * 60
MAX_WAIT_SECONDS = 24 * 3600

# entry_id -> its running _retry_loop task, so cancel() can actually stop the
# loop instead of just deleting the persisted entry out from under it (which
# would leave the task sleeping up to RETRY_INTERVAL_SECONDS before it wakes,
# finds its own entry_id already gone, and silently no-ops - harmless but
# pointless to leave running).
_active_tasks: dict[str, asyncio.Task] = {}


def _persist(entry_id: str, entry: dict):
    data = state.load_pending_auto()
    data[entry_id] = entry
    state.save_pending_auto(data)


def _forget(entry_id: str):
    data = state.load_pending_auto()
    data.pop(entry_id, None)
    state.save_pending_auto(data)


def list_pending() -> list[dict]:
    return list(state.load_pending_auto().values())


def is_queued(kind: str, payload: dict) -> bool:
    """True if an equivalent pick (same kind, same payload) is already
    queued - same duplicate-repost guard as pendingtrack.is_queued."""
    for entry in state.load_pending_auto().values():
        if entry["kind"] == kind and entry["payload"] == payload:
            return True
    return False


async def _retry_loop(entry_id: str, entry: dict, resolve: Callable[[dict], Awaitable[bool]]):
    try:
        expires_at = entry["queued_at"] + MAX_WAIT_SECONDS
        while True:
            remaining = expires_at - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(RETRY_INTERVAL_SECONDS, remaining))
            try:
                found = await resolve(entry["payload"])
            except Exception:
                log.exception("Pending auto resolve callback failed for kind=%s payload=%s", entry["kind"], entry["payload"])
                found = False
            if found:
                _forget(entry_id)
                return
        _forget(entry_id)
        log.info("Gave up waiting for pick to resolve: kind=%s payload=%s", entry["kind"], entry["payload"])
    finally:
        _active_tasks.pop(entry_id, None)


def queue(kind: str, payload: dict, resolve: Callable[[dict], Awaitable[bool]]) -> dict:
    """Called right after a fresh auto-track attempt's lookup comes back
    empty. resolve is awaited on every retry and on resume_all - return
    True once the pick has been fully posted/tracked so the entry can be
    dropped from the queue."""
    entry_id = uuid.uuid4().hex
    entry = {"kind": kind, "payload": payload, "queued_at": time.time()}
    _persist(entry_id, entry)
    _active_tasks[entry_id] = asyncio.create_task(_retry_loop(entry_id, entry, resolve))
    return entry


async def resume_all(resolvers: dict[str, Callable[[dict], Awaitable[bool]]]):
    """Called once from on_ready. Re-arms every still-pending lookup so a
    restart doesn't reset (or lose) its wait. An entry whose kind has no
    registered resolver (a kind that's since been removed) is dropped
    rather than left stuck forever with nothing able to resolve it."""
    for entry_id, entry in list(state.load_pending_auto().items()):
        resolve = resolvers.get(entry["kind"])
        if not resolve:
            log.warning("No resolver registered for pending auto kind=%s - dropping stale entry", entry["kind"])
            _forget(entry_id)
            continue
        _active_tasks[entry_id] = asyncio.create_task(_retry_loop(entry_id, entry, resolve))
        log.info("Resumed pending auto lookup: kind=%s payload=%s", entry["kind"], entry["payload"])


def display_name(payload: dict) -> str:
    return payload.get("player") or payload.get("team") or payload.get("fighter") or "?"


def find_matching(channel_id: int, name: Optional[str] = None) -> list[tuple[str, dict]]:
    """Queued entries in this channel, optionally filtered to ones whose
    player/team name contains name (case-insensitive substring). Used by
    /untrack's queued-pick branch, both to list what would be cancelled
    and to actually cancel it."""
    lowered = name.lower() if name else None
    out = []
    for entry_id, entry in state.load_pending_auto().items():
        if entry["payload"].get("channel_id") != channel_id:
            continue
        if lowered and lowered not in display_name(entry["payload"]).lower():
            continue
        out.append((entry_id, entry))
    return out


def cancel(entry_id: str) -> bool:
    """Stops the retry loop (if still running) and drops the persisted
    entry - the queued-pick counterpart to every tracker's own
    stop_tracking. Returns False if entry_id isn't currently queued."""
    if entry_id not in state.load_pending_auto():
        return False
    task = _active_tasks.pop(entry_id, None)
    if task:
        task.cancel()
    _forget(entry_id)
    return True

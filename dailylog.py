#!/usr/bin/env python3
"""
Per-day log of every auto-tracked pick, independent of each tracker's own
state.py file (which only ever holds *currently active* picks - a resolved
entry is unconditionally removed the moment its track loop ends, see e.g.
tracker.py's `finally: ... _forget(...)`). This module exists purely to
back /summary: a manually-triggered, per-date report of everything the bot
tracked on a given day and how each pick resolved.

Keyed by f"{channel_id}:{module}:{track_key}" - the exact same
(module, track_key) identity parlaytracker.py already uses to address a
leg across all 10 tracker modules, so there's no new identity scheme to
learn. That key is never reused (365scores/ESPN/esports event ids are
permanent, never recycled for a different fixture), so entries are simply
kept forever rather than needing a timestamp suffix to disambiguate reuse.

Only picks that came from the auto-track pipeline (i.e. have a `section` -
the verbatim header text from the picks channel, e.g. "MLB", "WNBA") are
logged at all - manual /track or /playerprops usage has no section and is
deliberately left out of the daily report entirely, matching "picks posted
will be from the picks being tracked on that same day."

A pick's `date` is fixed at creation time (record_pick) and never moves,
even if grading happens after an Eastern midnight rollover - the report is
about when a pick was POSTED, not when it finished.

Every entry also carries `origin_channel_id` - the picks-SOURCE channel the
pick was parsed from (message.channel.id in bot.py's on_message, captured
before it resolves to that source's own mapped target/scores channel via
config.PICKS_CHANNEL_MAP) - distinct from `channel_id`, which is whatever
channel_id the owning tracker module itself uses internally (the target
channel, for message ownership/throttling/etc., unrelated to reporting).
/summary's config.SUMMARY_ROUTES groups one or more origin channels under a
single report destination, so every read here filters by a SET of origin
ids rather than one - e.g. two separate picks-source channels whose reports
both belong combined into the same downstream report."""

import datetime
import logging
from typing import Iterable, Optional

import state
from scores365 import EASTERN

log = logging.getLogger("scorebox.dailylog")

WINMARK = "<:winmark:1532115635071488221>"
LOSSMARK = "<:lossmark:1532115600162422894>"
PUSHMARK = "➖"  # matches tracker.py's own plain "➖ Push" wording
VOIDMARK = "<:cashback:1533844020839841832>"

_RESULT_MARKS = {"won": WINMARK, "lost": LOSSMARK, "push": PUSHMARK, "void": VOIDMARK}

_PENDING_DETAIL = "⏳ Not Started"  # ⏳ - overwritten by the first touch() once a tracker actually polls


def _key(channel_id: int, module: str, track_key_str: str) -> str:
    return f"{channel_id}:{module}:{track_key_str}"


def today_str() -> str:
    return datetime.datetime.now(tz=EASTERN).strftime("%Y-%m-%d")


def record_pick(
    channel_id: int, module: str, track_key_str: str, section: Optional[str], label: str, message_id: int,
    origin_channel_id: Optional[int] = None,
):
    """Called once, right alongside each tracker's start_tracking. No-op if
    section is None - i.e. this pick didn't come from the auto-track
    pipeline (a bare picks-channel header to attribute it to), so it has no
    place in a per-section daily report."""
    if not section:
        return
    data = state.load_daily_log()
    data[_key(channel_id, module, track_key_str)] = {
        "channel_id": channel_id, "origin_channel_id": origin_channel_id, "module": module, "date": today_str(),
        "section": section, "label": label, "message_id": message_id,
        "status": "pending", "detail": _PENDING_DETAIL, "reported": False,
    }
    state.save_daily_log(data)


def touch(channel_id: int, module: str, track_key_str: str, detail: str):
    """Updates the human-readable detail shown for a still-unresolved pick
    (e.g. "NOT STARTED - <t:...:f>", "LIVE, Top 3rd 2-1", the same text
    already computed for parlay leg progress reporting elsewhere) - a no-op
    if this pick was never logged (no section) or already has a final
    result (touch() calls can race a grading call across the same poll
    cycle in theory; a final result always wins)."""
    data = state.load_daily_log()
    entry = data.get(_key(channel_id, module, track_key_str))
    if not entry or entry["status"] != "pending":
        return
    entry["detail"] = detail
    state.save_daily_log(data)


def record_result(channel_id: int, module: str, track_key_str: str, status: str):
    """status is one of won/lost/push/void - called once, at the same
    moment each tracker grades the pick and reports it to its parlay
    group (if any)."""
    if status not in _RESULT_MARKS:
        log.warning("dailylog.record_result got unknown status %r for %s/%s", status, module, track_key_str)
        return
    data = state.load_daily_log()
    entry = data.get(_key(channel_id, module, track_key_str))
    if not entry:
        return
    entry["status"] = status
    entry["detail"] = status.upper()
    state.save_daily_log(data)


def picks_for_date(origin_channel_ids: Iterable[int], date_str: str) -> list[dict]:
    """Every not-yet-reported pick whose origin_channel_id is in
    origin_channel_ids, logged on this date, in original tracking order
    (insertion order - both dict iteration and JSON round-tripping preserve
    it). Pass a single int for a one-channel route."""
    ids = {origin_channel_ids} if isinstance(origin_channel_ids, int) else set(origin_channel_ids)
    data = state.load_daily_log()
    return [
        entry for entry in data.values()
        if entry.get("origin_channel_id") in ids and entry["date"] == date_str and not entry["reported"]
    ]


def mark_reported(origin_channel_ids: Iterable[int], date_str: str):
    """Called only when a rendered report is actually POSTED to the
    channel (never on a preview) - every currently-unreported pick from any
    of these origin channels on that date is flagged so a later /summary
    for the same date (catching picks that were still pending the first
    time around) doesn't repeat ones already published."""
    ids = {origin_channel_ids} if isinstance(origin_channel_ids, int) else set(origin_channel_ids)
    data = state.load_daily_log()
    changed = False
    for entry in data.values():
        if entry.get("origin_channel_id") in ids and entry["date"] == date_str and not entry["reported"]:
            entry["reported"] = True
            changed = True
    if changed:
        state.save_daily_log(data)


def available_dates(origin_channel_ids: Iterable[int], limit: int = 14) -> list[str]:
    """Most-recent-first distinct dates with at least one not-yet-reported
    pick from any of these origin channels - used for the /summary date
    autocomplete/dropdown."""
    ids = {origin_channel_ids} if isinstance(origin_channel_ids, int) else set(origin_channel_ids)
    data = state.load_daily_log()
    dates = {
        entry["date"] for entry in data.values()
        if entry.get("origin_channel_id") in ids and not entry["reported"]
    }
    return sorted(dates, reverse=True)[:limit]


def result_mark(status: str) -> str:
    return _RESULT_MARKS.get(status, "")


def is_final(status: str) -> bool:
    return status in _RESULT_MARKS

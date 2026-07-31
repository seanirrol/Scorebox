#!/usr/bin/env python3
"""
ESPN's free MMA scoreboard API - a separate module from espn.py because its
data shape doesn't match anything else there: an "event" (e.g. a UFC Fight
Night or a PFL card) is a whole card of individual bouts ("competitions"),
each with two "athlete" competitors and no team/roster concept at all.
Confirmed live: espn.py's usual per-event /summary endpoint 404s for MMA
event AND competition ids - there's no working per-bout detail call - so
this reads everything straight off the scoreboard's own flat
(non-header-wrapped) shape instead, including for live-refresh polling.

Covers every MMA promotion ESPN actually has a scoreboard for - confirmed
live: UFC, PFL, and Bellator (still its own separate feed despite PFL
having absorbed the Bellator brand) all return real events; ONE
Championship, Invicta, and Rizin were tried and don't exist on ESPN's site
at all (400s, not listed in ESPN's own MMA site navigation either).

Only moneyline (fight winner) and round totals are supported here - method
of victory (KO/TKO, submission, decision) has no clean field anywhere in
this API, only an inferrable play-by-play event log, which isn't reliable
enough to grade a real pick against.
"""

import datetime
import time
from typing import Optional

import requests

import scores365

# Every MMA league ESPN's site API actually covers (see module docstring).
LEAGUE_SLUGS = ["ufc", "pfl", "bellator"]
LEAGUE_LABELS = {"ufc": "UFC", "pfl": "PFL", "bellator": "Bellator"}
REQUEST_TIMEOUT = 6

# UFC/PFL/Bellator cards are weekly/biweekly at best, not daily - the
# default no-date-range scoreboard call only ever returns the single
# nearest card per league (confirmed live), unlike daily sports where
# "today" is always populated. Searching a wide window instead catches an
# event whether it's already started, imminent, or was just fought.
_SEARCH_DAYS_BACK = 3
_SEARCH_DAYS_AHEAD = 21


class EspnUfcError(Exception):
    pass


def _get(league_slug: str, **params) -> dict:
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/mma/{league_slug}/scoreboard",
            params=params, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise EspnUfcError(f"ESPN MMA request failed ({league_slug}): {e}") from e


def fighter_photo_url(fighter_id) -> str:
    """Confirmed live - a real headshot, not a placeholder, despite there
    being no explicit photo field on the competitor/athlete object itself."""
    return f"https://a.espncdn.com/i/headshots/mma/players/full/{fighter_id}.png"


def find_ufc_fight(fighter_name: str) -> Optional[tuple[dict, dict, dict, str]]:
    """Searches every MMA league ESPN covers for a bout involving this
    fighter, across a multi-week window (see module docstring). Prefers an
    in-progress bout, then the soonest upcoming, then the most recently
    finished - same preference order as scores365.find_match_for_team, now
    also spanning leagues (a fighter is normally only ever signed to one).
    Returns (event, competition, fighter_competitor, league_slug) or None."""
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=_SEARCH_DAYS_BACK)).strftime("%Y%m%d")
    end = (today + datetime.timedelta(days=_SEARCH_DAYS_AHEAD)).strftime("%Y%m%d")

    best = None
    best_rank = None
    best_ts = None
    now = time.time()
    last_error = None
    any_success = False
    for league_slug in LEAGUE_SLUGS:
        try:
            data = _get(league_slug, dates=f"{start}-{end}")
            any_success = True
        except EspnUfcError as e:
            # Only skip this one league (e.g. a transient blip on ESPN's
            # PFL endpoint specifically) - if every league fails, that's a
            # real "can't reach ESPN" condition the caller needs to know
            # about, not silently reported as "fighter not found".
            last_error = e
            continue
        for event in data.get("events", []):
            for comp in event.get("competitions", []):
                fighter = next(
                    (c for c in comp.get("competitors", [])
                     if scores365.names_match((c.get("athlete") or {}).get("displayName", ""), fighter_name)),
                    None,
                )
                if not fighter:
                    continue
                state = comp.get("status", {}).get("type", {}).get("state")
                rank = {"in": 0, "pre": 1, "post": 2}.get(state, 3)
                comp_ts = start_epoch(comp)
                if best is None or rank < best_rank or (rank == best_rank and abs(comp_ts - now) < abs(best_ts - now)):
                    best, best_rank, best_ts = (event, comp, fighter, league_slug), rank, comp_ts
    if not any_success and last_error:
        raise last_error
    return best


def refresh_ufc_fight(league_slug: str, event_id, competition_id, fighter_id, competition_date: str) -> Optional[tuple[dict, dict]]:
    """Re-fetches a specific already-found bout's current status/result for
    live polling. Queries just that single day's scoreboard for the one
    known league (cheap) rather than find_ufc_fight's wide multi-league
    window, since both are already known. Returns (competition,
    fighter_competitor) or None if it's no longer listed (e.g. card
    canceled)."""
    try:
        date_str = datetime.datetime.fromisoformat(competition_date.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        return None
    data = _get(league_slug, dates=date_str)
    for event in data.get("events", []):
        if str(event.get("id")) != str(event_id):
            continue
        for comp in event.get("competitions", []):
            if str(comp.get("id")) != str(competition_id):
                continue
            fighter = next((c for c in comp.get("competitors", []) if str(c.get("id")) == str(fighter_id)), None)
            if fighter:
                return comp, fighter
    return None


def start_epoch(competition: dict) -> float:
    start = competition.get("date")
    if not start:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def is_finished(competition: dict) -> bool:
    return bool(competition.get("status", {}).get("type", {}).get("completed"))


def status_text(competition: dict) -> str:
    status = competition.get("status", {})
    status_type = status.get("type", {})
    state = status_type.get("state")
    if state == "post":
        return status_type.get("detail") or "Final"
    if state == "pre":
        start_ts = start_epoch(competition)
        if not start_ts:
            return "Not Started"
        seconds = start_ts - time.time()
        if seconds <= 0:
            return "Starting soon"
        hours, minutes = divmod(int(seconds // 60), 60)
        return f"Starts in {hours}h{minutes:02d}m" if hours else f"Starts in {minutes}m"
    round_num = status.get("period")
    clock = status.get("displayClock")
    if round_num and clock:
        return f"Round {round_num} ({clock})"
    return status_type.get("detail") or "Live"


def grade_ufc_moneyline(competition: dict, fighter_competitor: dict) -> Optional[str]:
    """Grades a fight-winner pick. Returns None if not decided yet. A
    completed bout where neither fighter is marked winner is a draw or
    no-contest - treated as a push (void/refund), same as an exact-tie
    moneyline elsewhere in this codebase, rather than guessing a side."""
    if not is_finished(competition):
        return None
    competitors = competition.get("competitors", [])
    if not any(c.get("winner") for c in competitors):
        return "push"
    return "won" if fighter_competitor.get("winner") else "lost"


def grade_ufc_round_total(competition: dict, direction: str, line: float) -> Optional[str]:
    """Grades a round-total pick against the round the fight actually ended
    in - ESPN's own "period" field on a completed bout (confirmed live: 1
    for a 1st-round finish, 3 for a fight that went the full scheduled
    distance)."""
    if not is_finished(competition):
        return None
    rounds = competition.get("status", {}).get("period")
    if rounds is None:
        return None
    if rounds == line:
        return "push"
    if direction == "over":
        return "won" if rounds > line else "lost"
    return "won" if rounds < line else "lost"

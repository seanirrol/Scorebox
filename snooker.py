#!/usr/bin/env python3
"""
scores24.live's own internal JSON API - snooker live scores. Same site/API
family as tabletennis.py (see that module's own docstring for why
scores24.live and not 365scores - 365scores has no snooker coverage
either), just a different sport slug.

Two endpoints matter, same shape as tabletennis.py's:
- /sport/snooker/leagues (with a date_between[] range + with_live) - every
  league with a match that day, each with its own live-scored matches
  embedded. Used to search for a specific player by name.
- /localized/matches/snooker/{matchSlug} - one match's own current state,
  used for live-refresh polling once a match is already found.

Unlike table tennis, a snooker match's own running FRAMES-won tally lives
in a top-level result_score field ("1:0", "4:3", ...) rather than an "FT"
entry inside result_scores - confirmed live watching a real day's full
listing: result_score is populated identically whether the match is live,
finished, or a walkover, so it's the one field worth reading for frames
won, rather than tabletennis.py's "scan for an FT entry" approach (which
doesn't apply here - a live snooker match's result_scores has no FT entry
at all until the match actually finishes).

result_scores itself is a list of per-FRAME point scores (type "1", "2",
...), with a leading empty list (`[]`) as its first element for reasons
unclear from the API itself - skipped over the same way a missing/invalid
entry would be. A finished match confirmed live also carries a trailing
{"type": "FT", "value": "<same as result_score>"} entry, but since
result_score already carries this value at every stage (live or finished),
reading result_score directly means frames_won() doesn't need to special-
case "is there an FT entry yet".

status.code "0" is not-started, "20" is live/started, "100" is a normal
finish (winner is 1 or 2), "91" showed up live as both "walkover" (a
retirement/no-show - is_finished True, winner set, but no scores/empty
result_scores) and, once, as a transitional "ended"-named state with
is_finished still False - is_finished/winner are trusted over the status
code's own name for exactly this reason, same as tabletennis.py's own
"trust the flags, not the code" approach. A walkover's winner is never 0
(unlike a genuine cancellation), so grade_moneyline handles it fine via
the winner field alone even though frames_won() has nothing to read;
is_cancelled (winner 0 + finished) hasn't been confirmed live for snooker
specifically, but is kept for parity with tabletennis.py's own defensive
check, since it's the same API family.
"""

import datetime
import logging
import time
from typing import Optional

import requests

import scores365

log = logging.getLogger("scorebox.snooker")

BASE_URL = "https://scores24.live/rapi"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}
REQUEST_TIMEOUT = 10

# Ranking/invitational snooker events run in single-table blocks across a
# day rather than table tennis's bulk always-on leagues, but the same
# adjacent-day search window still covers a pick made close to a UTC day
# boundary - see tabletennis.py's own comment on this exact reasoning.
_SEARCH_DAYS_BACK = 1
_SEARCH_DAYS_AHEAD = 1


class SnookerError(Exception):
    pass


def _get(path: str, **params) -> Optional[dict]:
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise SnookerError(f"scores24.live request failed: {e}") from e


def _fetch_matches_for_day(day: datetime.date) -> list[dict]:
    """Every snooker match (any league, live/finished/upcoming) whose
    match_date falls on this UTC day - flattened out of the day's own
    per-league grouping, same shape as tabletennis._fetch_matches_for_day."""
    start = f"{day.isoformat()} 00:00:00"
    end = f"{day.isoformat()} 23:59:59"
    data = _get(
        "/sport/snooker/leagues",
        **{
            "date_between[]": [start, end],
            "is_bot": "false", "is_open": "true", "lang": "en",
            "match_filter": "all", "with_live": "true",
        },
    )
    if not data:
        return []
    matches = []
    for entry in data.get("data", []):
        matches.extend(entry.get("matches", []))
    return matches


def _match_epoch(match: dict) -> Optional[float]:
    raw = match.get("match_date")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_match_for_team(team: str, opponent: Optional[str] = None) -> Optional[dict]:
    """Searches today (and the adjacent days) for a match involving this
    player, across every league. Prefers an in-progress match, then the
    soonest upcoming, then the most recently finished - same preference
    order as tabletennis.find_match_for_team.

    opponent, when given, requires the OTHER side of the match to also
    match by name - same "same player, two simultaneous matches against
    different opponents" disambiguation confirmed live for table tennis
    (see tabletennis.find_match_for_team's own comment); kept here for
    parity even though not yet independently confirmed for snooker."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    days = [today + datetime.timedelta(days=d) for d in range(-_SEARCH_DAYS_BACK, _SEARCH_DAYS_AHEAD + 1)]

    best = None
    best_rank = None
    best_ts = None
    now = time.time()
    for day in days:
        try:
            matches = _fetch_matches_for_day(day)
        except SnookerError as e:
            log.warning("Couldn't fetch snooker matches for %s: %s", day, e)
            continue
        for match in matches:
            teams = match.get("teams") or []
            found = next((t for t in teams if scores365.names_match(t.get("name", ""), team)), None)
            if not found:
                continue
            if opponent:
                other = next((t for t in teams if t is not found), None)
                if not other or not scores365.names_match(other.get("name", ""), opponent):
                    continue
            if match.get("is_live"):
                rank = 0
            elif match.get("is_finished"):
                rank = 2
            else:
                rank = 1
            match_ts = _match_epoch(match) or now
            if best is None or rank < best_rank or (rank == best_rank and abs(match_ts - now) < abs(best_ts - now)):
                best, best_rank, best_ts = match, rank, match_ts
    return best


def get_live_update(match: dict) -> Optional[dict]:
    """Re-fetches a specific match's current state by its own slug - same
    shape as tabletennis.get_live_update."""
    slug = match.get("slug")
    if not slug:
        return None
    try:
        data = _get(f"/localized/matches/snooker/{slug}", lang="en")
    except SnookerError as e:
        log.warning("Couldn't refresh snooker match %s: %s", slug, e)
        return None
    return (data or {}).get("data")


def is_live(match: dict) -> bool:
    return bool(match.get("is_live"))


def is_finished(match: dict) -> bool:
    return bool(match.get("is_finished"))


def is_cancelled(match: dict) -> bool:
    """A finished match with no actual winner recorded - see module
    docstring (not yet confirmed live for snooker, kept for parity with
    tabletennis.is_cancelled since it's the same API family)."""
    return bool(match.get("is_finished")) and not match.get("winner")


def _frame_scores(match: dict) -> list[tuple[int, int]]:
    """Every individual frame's own point score, in order (including the
    currently in-progress one, if any) - skips the leading empty-list
    placeholder entry and the trailing FT entry, same idea as
    tabletennis._game_scores."""
    scores = []
    for entry in match.get("result_scores") or []:
        if not isinstance(entry, dict) or entry.get("type") == "FT":
            continue
        raw = entry.get("value") or ""
        if ":" not in raw:
            continue
        home, _, away = raw.partition(":")
        try:
            scores.append((int(home), int(away)))
        except ValueError:
            continue
    return scores


def frames_won(match: dict) -> Optional[tuple[int, int]]:
    """(home, away) frames won so far - reads the top-level result_score
    field directly (see module docstring for why this, not an "FT" entry,
    is the one source of truth here)."""
    raw = match.get("result_score") or ""
    if ":" not in raw:
        return None
    home, _, away = raw.partition(":")
    try:
        return int(home), int(away)
    except ValueError:
        return None


def current_frame_score(match: dict) -> Optional[tuple[int, int]]:
    """The most recent frame's own point score - the in-progress one while
    live, or the deciding frame's final score once finished."""
    scores = _frame_scores(match)
    return scores[-1] if scores else None


def status_line(match: dict) -> str:
    if is_finished(match):
        return "Final"
    if is_live(match):
        frame_number = len(_frame_scores(match))
        return f"Frame {frame_number}" if frame_number else "Live"
    return ""


def grade_moneyline(match: dict, picked_team: str) -> Optional[str]:
    """Returns "won"/"lost"/"void", or None if not finished yet or
    picked_team doesn't match either side. No draws in snooker (someone
    always wins the deciding frame), so there's no "push" case. Reads the
    winner field directly rather than frames_won, so a walkover (real
    winner, no scores recorded) still grades correctly."""
    if not is_finished(match):
        return None
    if is_cancelled(match):
        return "void"
    teams = match.get("teams") or []
    if len(teams) != 2:
        return None
    home_name, away_name = teams[0].get("name", ""), teams[1].get("name", "")
    winner = match.get("winner")
    if winner == 1:
        winning_name = home_name
    elif winner == 2:
        winning_name = away_name
    else:
        return None
    if scores365.names_match(winning_name, picked_team):
        return "won"
    if scores365.names_match(home_name, picked_team) or scores365.names_match(away_name, picked_team):
        return "lost"
    return None


def grade_frame_handicap(match: dict, picked_team: str, line: float) -> Optional[str]:
    """Frames won by the picked player, plus the (already-signed) line,
    versus the opponent's own frames won - the standard snooker sportsbook
    "Frame Handicap" market, same shape as tennis's games handicap. Only
    settles once the match itself finishes."""
    if not is_finished(match):
        return None
    if is_cancelled(match):
        return "void"
    teams = match.get("teams") or []
    if len(teams) != 2:
        return None
    home_name, away_name = teams[0].get("name", ""), teams[1].get("name", "")
    frames = frames_won(match)
    if not frames:
        return None
    home_frames, away_frames = frames
    if scores365.names_match(home_name, picked_team):
        picked_frames, other_frames = home_frames, away_frames
    elif scores365.names_match(away_name, picked_team):
        picked_frames, other_frames = away_frames, home_frames
    else:
        return None
    margin = (picked_frames + line) - other_frames
    if margin > 0:
        return "won"
    if margin < 0:
        return "lost"
    return "push"

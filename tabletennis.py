#!/usr/bin/env python3
"""
scores24.live's own internal JSON API - table tennis live scores. Needed
because 365scores (this bot's usual score-data backbone - see scores365.py)
has no table tennis coverage at all: confirmed live via its own sports
listing endpoint (https://webws.365scores.com/web/sports/ - exactly 10
sports, none of them table tennis) and a broad sportId sweep (10-300)
against its live-games endpoint turning up nothing.

scores24.live is a bookmaker-affiliate live-odds site, not Cloudflare-
protected (confirmed live: works fine via plain `requests` with an
ordinary browser User-Agent, both from this machine and from the
production host - unlike Sofascore, which the sibling My Bookies project's
own gosugamers.js comments flag as Cloudflare-blocked) - no need for
esports.py's curl-subprocess workaround here.

Two endpoints matter:
- /sport/table-tennis/leagues (with a date_between[] range + with_live) -
  every league with a match that day, each with its own live-scored
  matches embedded. Used to search for a specific player by name - no
  dedicated search-by-name endpoint was found (the site's own /search
  endpoint always came back empty regardless of query param name tried).
- /localized/matches/table-tennis/{matchSlug} - one match's own current
  state, used for live-refresh polling once a match is already found.

A match's own score is result_scores: a list of per-GAME point scores
(type "1", "2", "3", ...) plus a running "FT" entry - games actually
completed so far. Confirmed live watching a real match update across
several polls: [{"type":"1","value":"13:15"},{"type":"2","value":
"17:15"},{"type":"3","value":"11:7"},{"type":"4","value":"2:9"},
{"type":"FT","value":"2:1"}] means games 1-3 are final (FT tallies them
2-1) and game 4's "2:9" is the CURRENTLY IN-PROGRESS game's own live point
score, not yet folded into FT.

status.code "0" is not-started, "9"/"10"/"11" are all live (is_live
already covers every one of these), "100" is a normal finish (winner is 1
or 2), "70" is a cancelled/no-result match (is_finished=True but winner=0
and every score sits at "0:0") - confirmed live, several of these came
through a single day's full listing with no games ever played.
"""

import datetime
import logging
import time
from typing import Optional

import requests

import scores365

log = logging.getLogger("scorebox.tabletennis")

BASE_URL = "https://scores24.live/rapi"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}
REQUEST_TIMEOUT = 10

# Table tennis leagues run close to 24/7 (Setka Cup, TT Cup, Czech Liga
# Pro, ...) - a day-scoped search easily covers "right now" plus whatever's
# already live from a bit earlier, but a pick made close to a UTC day
# boundary needs the adjacent day too, since match_date on these bulk
# always-on leagues doesn't reliably reflect actual play time (confirmed
# live: a match still showing "00:00:00" as its match_date was already
# hours into being played). Mirrors espn_ufc.py's own multi-day window
# reasoning, just narrower since table tennis turns over must faster than
# a UFC card.
_SEARCH_DAYS_BACK = 1
_SEARCH_DAYS_AHEAD = 1


class TableTennisError(Exception):
    pass


def _get(path: str, **params) -> Optional[dict]:
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise TableTennisError(f"scores24.live request failed: {e}") from e


def _fetch_matches_for_day(day: datetime.date) -> list[dict]:
    """Every table tennis match (any league, live/finished/upcoming) whose
    match_date falls on this UTC day - flattened out of the day's own
    per-league grouping."""
    start = f"{day.isoformat()} 00:00:00"
    end = f"{day.isoformat()} 23:59:59"
    data = _get(
        "/sport/table-tennis/leagues",
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


def find_match_for_team(team: str) -> Optional[dict]:
    """Searches today (and the adjacent days - see _SEARCH_DAYS_BACK/
    _SEARCH_DAYS_AHEAD) for a match involving this player, across every
    league. Prefers an in-progress match, then the soonest upcoming, then
    the most recently finished - same preference order as
    scores365.find_match_for_team/espn_ufc.find_ufc_fight."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    days = [today + datetime.timedelta(days=d) for d in range(-_SEARCH_DAYS_BACK, _SEARCH_DAYS_AHEAD + 1)]

    best = None
    best_rank = None
    best_ts = None
    now = time.time()
    for day in days:
        try:
            matches = _fetch_matches_for_day(day)
        except TableTennisError as e:
            log.warning("Couldn't fetch table tennis matches for %s: %s", day, e)
            continue
        for match in matches:
            teams = match.get("teams") or []
            found = next((t for t in teams if scores365.names_match(t.get("name", ""), team)), None)
            if not found:
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
    "re-resolve every poll cycle" shape as scores365.get_live_update,
    except keyed by a slug instead of a numeric game id (scores24.live's
    own match ids are opaque strings, not stable-looking integers - see
    the module docstring's "ts_"/"ba_"-prefixed id examples)."""
    slug = match.get("slug")
    if not slug:
        return None
    try:
        data = _get(f"/localized/matches/table-tennis/{slug}", lang="en")
    except TableTennisError as e:
        log.warning("Couldn't refresh table tennis match %s: %s", slug, e)
        return None
    return (data or {}).get("data")


def is_live(match: dict) -> bool:
    return bool(match.get("is_live"))


def is_finished(match: dict) -> bool:
    return bool(match.get("is_finished"))


def is_cancelled(match: dict) -> bool:
    """A finished match with no actual winner recorded - confirmed live,
    status.code "70" carries this exact shape (winner 0, every game's
    score sitting at "0:0") for a match that was scheduled but never
    played."""
    return bool(match.get("is_finished")) and not match.get("winner")


def _game_scores(match: dict) -> list[tuple[int, int]]:
    """Every individual game's own point score, in order (including the
    currently in-progress one, if any - see module docstring)."""
    scores = []
    for entry in match.get("result_scores") or []:
        if entry.get("type") == "FT":
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


def games_won(match: dict) -> Optional[tuple[int, int]]:
    """(home, away) games won so far - the running "FT" tally. Only counts
    games that have actually finished (the live in-progress game's own
    partial score is never folded in here - see _game_scores)."""
    for entry in match.get("result_scores") or []:
        if entry.get("type") != "FT":
            continue
        raw = entry.get("value") or ""
        if ":" not in raw:
            return None
        home, _, away = raw.partition(":")
        try:
            return int(home), int(away)
        except ValueError:
            return None
    return None


def total_points(match: dict) -> Optional[tuple[int, int]]:
    """Combined points scored across every game so far (completed AND the
    current in-progress one) - used for Point Handicap grading, the same
    "sum every game's own score" shape as scores365.tennis_match_games."""
    scores = _game_scores(match)
    if not scores:
        return None
    return sum(h for h, _ in scores), sum(a for _, a in scores)


def current_game_score(match: dict) -> Optional[tuple[int, int]]:
    """The most recent game's own point score - the in-progress one while
    live, or the deciding game's final score once finished. Used as the
    scorecard's own sub-row, same idea as tennis's current-set score."""
    scores = _game_scores(match)
    return scores[-1] if scores else None


def status_line(match: dict) -> str:
    if is_finished(match):
        return "Final"
    if is_live(match):
        game_number = len(_game_scores(match))
        return f"Game {game_number}" if game_number else "Live"
    return ""


def grade_moneyline(match: dict, picked_team: str) -> Optional[str]:
    """Returns "won"/"lost"/"void", or None if not finished yet or
    picked_team doesn't match either side. No draws in table tennis
    (someone always wins the deciding game), so there's no "push" case."""
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


def grade_point_handicap(match: dict, picked_team: str, line: float) -> Optional[str]:
    """Combined total points scored by the picked player, plus the
    (already-signed) line, versus the opponent's total - same shape as
    scores365.grade_games_handicap. Only settles once the match itself
    finishes (a mid-match total can still climb for either side)."""
    if not is_finished(match):
        return None
    if is_cancelled(match):
        return "void"
    teams = match.get("teams") or []
    if len(teams) != 2:
        return None
    home_name, away_name = teams[0].get("name", ""), teams[1].get("name", "")
    totals = total_points(match)
    if not totals:
        return None
    home_total, away_total = totals
    if scores365.names_match(home_name, picked_team):
        picked_total, other_total = home_total, away_total
    elif scores365.names_match(away_name, picked_team):
        picked_total, other_total = away_total, home_total
    else:
        return None
    margin = (picked_total + line) - other_total
    if margin > 0:
        return "won"
    if margin < 0:
        return "lost"
    return "push"

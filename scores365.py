#!/usr/bin/env python3
"""
365scores.com live score client.

Ported from the same approach used in the Torn-BetSync project: 365scores runs
an open, unauthenticated JSON API with no anti-bot wall, covering soccer,
basketball, tennis, hockey, NFL, baseball, volleyball, and rugby in one
consistent shape - a better fit here than TheSportsDB's free tier, which has
no true live-score endpoint for most sports.
"""

import datetime
import re
import time
from typing import Optional

import requests

BASE_URL = "https://webws.365scores.com/web"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 6

# Torn sport name (lowercased) -> 365scores' own numeric sport id.
SPORT_IDS = {
    "football": 1,
    "soccer": 1,
    "basketball": 2,
    "tennis": 3,
    "hockey": 4,
    "american football": 6,
    "nfl": 6,
    "baseball": 7,
    "volleyball": 8,
    "rugby": 9,
    "rugby league": 9,
    "rugby union": 9,
}
UNIQUE_SPORT_IDS = sorted(set(SPORT_IDS.values()))

# statusGroup: 2 = not started, 3 = in progress, 4+ = terminal (ended/postponed/...)
_STATUS_RANK = {"inprogress": 0, "notstarted": 1, "finished": 2}

GAMES_CACHE_SECONDS = 8
EXTRA_PAGES = 4
_games_cache: dict[int, dict] = {}  # sport_id -> {"games": [...], "fetched_at": monotonic ts}


class ScoresError(Exception):
    pass


def _get(url: str, **params) -> dict:
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json() or {}
    except (requests.RequestException, ValueError) as e:
        raise ScoresError(f"365scores request failed: {e}") from e


def _fetch_games_for_sport(sport_id: int) -> list[dict]:
    """Current games for a sport (live + today's schedule), lightly cached."""
    cached = _games_cache.get(sport_id)
    if cached and time.monotonic() - cached["fetched_at"] < GAMES_CACHE_SECONDS:
        return cached["games"]

    data = _get(f"{BASE_URL}/games/current/", langId=1, timezoneName="UTC", userCountryId=1, sports=sport_id)
    games = list(data.get("games") or [])

    # The bulk endpoint is paginated; follow the cursor a few pages forward so
    # a high-volume sport (e.g. football) isn't limited to the next ~3 hours.
    next_page = (data.get("paging") or {}).get("nextPage")
    for _ in range(EXTRA_PAGES):
        if not next_page:
            break
        try:
            page = _get(f"https://webws.365scores.com{next_page}", sports=sport_id)
        except ScoresError:
            break
        games.extend(page.get("games") or [])
        next_page = (page.get("paging") or {}).get("nextPage")

    _games_cache[sport_id] = {"games": games, "fetched_at": time.monotonic()}
    return games


def map_status_type(status_group) -> str:
    if status_group == 2:
        return "notstarted"
    if status_group == 3:
        return "inprogress"
    return "finished"


def is_finished(game: dict) -> bool:
    return map_status_type(game.get("statusGroup")) == "finished"


# --- fuzzy team-name matching ----------------------------------------------

def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _meaningful_words(name: str) -> set[str]:
    words = set(re.sub(r"[^a-z0-9\s]", " ", (name or "").lower()).split())
    filtered = {w for w in words if len(w) > 2}
    return filtered or words


def names_match(a: str, b: str) -> bool:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = _meaningful_words(a), _meaningful_words(b)
    if not wa or not wb:
        return False
    smaller, larger = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return smaller.issubset(larger)


def _start_epoch(game: dict) -> float:
    start = game.get("startTime")
    if not start:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _candidates_for_team(games: list[dict], team: str) -> list[dict]:
    out = []
    for g in games:
        home = (g.get("homeCompetitor") or {}).get("name", "")
        away = (g.get("awayCompetitor") or {}).get("name", "")
        if names_match(home, team) or names_match(away, team):
            out.append(g)
    return out


def find_match_for_team(team: str, sport: Optional[str] = None) -> Optional[tuple[dict, int]]:
    """
    Search 365scores' live game lists for a team, across all supported sports
    unless a specific one is given. Prefers a live match, then the
    soonest/most-recently scheduled or finished one. Returns (game, sport_id).
    """
    sport_ids = [SPORT_IDS[sport.lower()]] if sport and sport.lower() in SPORT_IDS else UNIQUE_SPORT_IDS

    best = None
    best_sport_id = None
    best_rank = None
    now = time.time()

    for sport_id in sport_ids:
        try:
            games = _fetch_games_for_sport(sport_id)
        except ScoresError:
            continue
        for game in _candidates_for_team(games, team):
            status = map_status_type(game.get("statusGroup"))
            rank = _STATUS_RANK.get(status, 3)
            if best is None or rank < best_rank or (
                rank == best_rank and abs(_start_epoch(game) - now) < abs(_start_epoch(best) - now)
            ):
                best, best_sport_id, best_rank = game, sport_id, rank

    return (best, best_sport_id) if best else None


def get_live_update(sport_id: int, game_id) -> Optional[dict]:
    """Re-fetch a specific game's current state from the (cached) bulk list."""
    try:
        games = _fetch_games_for_sport(sport_id)
    except ScoresError:
        return None
    for g in games:
        if g.get("id") == game_id:
            return g
    return None


# --- formatting --------------------------------------------------------

def format_score_line(game: dict) -> str:
    home = (game.get("homeCompetitor") or {}).get("name", "?")
    away = (game.get("awayCompetitor") or {}).get("name", "?")
    home_score = (game.get("homeCompetitor") or {}).get("score")
    away_score = (game.get("awayCompetitor") or {}).get("score")

    status = map_status_type(game.get("statusGroup"))
    # -1 is 365scores' own "no score yet" sentinel.
    if status == "notstarted" or home_score is None or away_score is None or home_score < 0 or away_score < 0:
        return f"{home} vs {away} — not started"
    return f"{home} {home_score} - {away_score} {away}"


def status_line(game: dict) -> str:
    status = map_status_type(game.get("statusGroup"))
    if status == "notstarted":
        return f"Kickoff: {game.get('startTime', 'TBD')}"
    if status == "finished":
        return game.get("statusText") or "Final"
    text = game.get("statusText") or "Live"
    clock = game.get("gameTimeDisplay")
    return f"{text} ({clock})" if clock else text

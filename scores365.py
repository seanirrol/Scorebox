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
from zoneinfo import ZoneInfo

import requests

EASTERN = ZoneInfo("America/New_York")

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

# Sport id -> display label, for embeds (matches the /score, /track dropdown labels).
SPORT_ID_LABELS = {
    1: "Soccer",
    2: "Basketball",
    3: "Tennis",
    4: "Hockey",
    6: "NFL",
    7: "Baseball",
    8: "Volleyball",
    9: "Rugby",
}


def sport_label(sport_id: Optional[int]) -> Optional[str]:
    return SPORT_ID_LABELS.get(sport_id)


_LOGO_URL_TEMPLATE = "https://imagecache.365scores.com/image/upload/f_png,w_100,h_100,c_limit,q_auto:eco,dpr_2/v{version}/Competitors/{id}"


def competitor_logo_url(competitor: dict) -> Optional[str]:
    """Confirmed live against a real competitor (id 7428, imageVersion 3 -> the St. Louis Cardinals' crest)."""
    comp_id, version = competitor.get("id"), competitor.get("imageVersion")
    if not comp_id or version is None:
        return None
    return _LOGO_URL_TEMPLATE.format(version=version, id=comp_id)

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


GAME_DETAIL_CACHE_SECONDS = 5
_game_detail_cache: dict[int, dict] = {}  # game_id -> {"detail": ..., "fetched_at": monotonic ts}


def _get_game_detail(game_id) -> Optional[dict]:
    """
    Per-game detail call - the only place 365scores exposes volleyball's
    per-set score. Deliberately omits langId/timezoneName/userCountryId -
    confirmed (in the My Bookies port) that this exact endpoint returns a
    stripped response with no "game" key at all when they're present.
    """
    cached = _game_detail_cache.get(game_id)
    if cached and time.monotonic() - cached["fetched_at"] < GAME_DETAIL_CACHE_SECONDS:
        return cached["detail"]
    try:
        data = _get("https://webws.365scores.com/web/game/", gameId=game_id)
    except ScoresError:
        return None
    detail = data.get("game")
    _game_detail_cache[game_id] = {"detail": detail, "fetched_at": time.monotonic()}
    return detail


def _norm_score(v):
    """-1 is 365scores' own "no score yet" sentinel (seen on not-yet-played sets)."""
    return None if v is None or v < 0 else v


def volleyball_set_scores(sport_id: int, status: str, game_id) -> Optional[list[dict]]:
    """
    Volleyball-only per-set breakdown. Unlike tennis, the bulk list's own game
    object has no `stages` array for volleyball at all, so this needs its own
    per-game detail call (see _get_game_detail above).
    """
    if status == "notstarted" or sport_id != SPORT_IDS["volleyball"] or not game_id:
        return None
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    stages = detail.get("stages") or []
    set_stages = [s for s in stages if re.match(r"^Set \d+$", s.get("name") or "") and (s.get("isEnded") or s.get("isLive"))]
    if not set_stages:
        return None
    return [
        {
            "set_number": int(s["name"].replace("Set ", "")),
            "home": _norm_score(s.get("homeCompetitorScore")),
            "away": _norm_score(s.get("awayCompetitorScore")),
            "is_live": bool(s.get("isLive")),
        }
        for s in set_stages
    ]


def tennis_current_set_score(game: dict) -> Optional[tuple[int, int]]:
    """Tennis-only current-set game score - already sitting on the bulk list's own `stages` array."""
    stages = game.get("stages") or []
    set_stages = [s for s in stages if re.match(r"^Set \d+$", s.get("name") or "")]
    if not set_stages:
        return None
    current = next((s for s in set_stages if s.get("isLive")), set_stages[-1])
    home = _norm_score(current.get("homeCompetitorScore"))
    away = _norm_score(current.get("awayCompetitorScore"))
    return (home, away) if home is not None and away is not None else None


def tennis_first_set_result(game: dict) -> Optional[tuple[int, int]]:
    """(home_games, away_games) for Set 1 once it's fully complete
    (isEnded=True), or None if it hasn't finished yet - same `stages` array
    as tennis_current_set_score, already sitting on the bulk list's own game
    object, no separate detail call needed (unlike innings_breakdown's
    baseball equivalent)."""
    stages = {s.get("name"): s for s in (game.get("stages") or [])}
    stage = stages.get("Set 1")
    if not stage or not stage.get("isEnded"):
        return None
    home = _norm_score(stage.get("homeCompetitorScore"))
    away = _norm_score(stage.get("awayCompetitorScore"))
    return (int(home), int(away)) if home is not None and away is not None else None


def grade_tennis_set(game: dict, home_games: int, away_games: int, picked_team: str) -> Optional[str]:
    """Grades a tennis set-winner pick (e.g. "1st Set Moneyline"). A
    completed tennis set is always decided one way or the other (by games or
    a tiebreak) - no tie/push case exists here, unlike full-match moneyline
    grading elsewhere in this module."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, picked_team):
        picked_games, other_games = home_games, away_games
    elif names_match(away, picked_team):
        picked_games, other_games = away_games, home_games
    else:
        return None
    return "won" if picked_games > other_games else "lost"


# Our display label -> 365scores' own stat name (confirmed live against the
# /web/game/stats/ endpoint). "Winners"/"Unforced Errors" aren't tracked by
# 365scores at all for tennis - confirmed live across 46 real finished
# matches in one day's slate, none had either stat present - so they're
# deliberately left out here (see tennispropstracker.py's docstring).
TENNIS_STAT_CATALOG = {
    "Aces": "Aces",
    "Double Faults": "Double Faults",
    "Break Points Won": "Break Points Won",
}


def _parse_tennis_stat_value(raw: Optional[str]) -> Optional[float]:
    """365scores tennis stats come as either a plain integer ("3") or an
    "X/Y (Z%)" opportunities-converted fraction (e.g. "2/6 (33%)", used for
    stats like Break Points Won) - only the leading X is the actual count a
    prop line would grade against."""
    if raw is None:
        return None
    m = re.match(r"^(\d+)", raw.strip())
    return float(m.group(1)) if m else None


def tennis_player_stat(game_id, competitor_id, stat_name: str) -> Optional[float]:
    """One competitor's live/final value for a named stat, from this game's
    own stats breakdown. A not-yet-started match carries no "statistics" key
    at all (confirmed live) - returns None cleanly rather than erroring."""
    try:
        data = _get(f"{BASE_URL}/game/stats/", games=game_id)
    except ScoresError:
        return None
    for item in data.get("statistics", []):
        if item.get("competitorId") == competitor_id and item.get("name") == stat_name:
            return _parse_tennis_stat_value(item.get("value"))
    return None


def grade_over_under(value, direction: str, line: float) -> Optional[str]:
    """Grades an over/under prop against a finished event's final stat value.
    Returns "won"/"lost"/"push" (exactly on the line), or None if value isn't
    a usable number."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric == line:
        return "push"
    if direction == "over":
        return "won" if numeric > line else "lost"
    return "won" if numeric < line else "lost"


def current_set_score(game: dict, sport_id: Optional[int]) -> Optional[tuple[int, int]]:
    """Current in-progress set/game score - tennis and volleyball only."""
    if map_status_type(game.get("statusGroup")) != "inprogress":
        return None
    if sport_id == SPORT_IDS["tennis"]:
        return tennis_current_set_score(game)
    if sport_id == SPORT_IDS["volleyball"]:
        live_set = next((s for s in volleyball_set_scores(sport_id, "inprogress", game.get("id")) or [] if s["is_live"]), None)
        if live_set and live_set["home"] is not None and live_set["away"] is not None:
            return (live_set["home"], live_set["away"])
    return None


def tennis_current_game_points(game: dict) -> Optional[tuple[int, int]]:
    """Tennis-only points in the current game - the `stages` entry named "Game" while live."""
    stages = game.get("stages") or []
    game_stage = next((s for s in stages if s.get("name") == "Game" and s.get("isLive")), None)
    if not game_stage:
        return None
    home = _norm_score(game_stage.get("homeCompetitorScore"))
    away = _norm_score(game_stage.get("awayCompetitorScore"))
    return (home, away) if home is not None and away is not None else None


# 365scores encodes a standard game's points as 0/15/30/40/50 - 50 means
# Advantage, not a real point count. A tiebreak game's own points aren't in
# this sequence at all (plain incrementing integers), so anything outside
# this map is shown as its raw number rather than mis-labeled.
_TENNIS_POINT_LABELS = {0: "0", 15: "15", 30: "30", 40: "40", 50: "AD"}


def tennis_point_label(v) -> str:
    return _TENNIS_POINT_LABELS.get(int(v), fmt_score(v))


_IRREGULAR_TERMINAL_STATUS_TEXTS = {"interrupted"}


def map_status_type(status_group, status_text: Optional[str] = None) -> str:
    if status_group == 2:
        return "notstarted"
    if status_group == 3:
        return "inprogress"
    if (status_text or "").strip().lower() in _IRREGULAR_TERMINAL_STATUS_TEXTS:
        # A paused/suspended match (rain delay, darkness, etc.) isn't
        # actually over - confirmed live 365scores puts it under the same
        # statusGroup (4, normally a real final result) as a genuinely
        # finished match. Treated as still in-progress so grading and the
        # "Final" pill don't fire against a snapshot score from before play
        # stopped - a real "Peyton Stearns ML" pick got wrongly tagged Pick
        # Lost this way while the match was still just interrupted, not over.
        return "inprogress"
    return "finished"


def is_finished(game: dict) -> bool:
    return map_status_type(game.get("statusGroup"), game.get("statusText")) == "finished"


# --- fuzzy team-name matching ----------------------------------------------

def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _meaningful_words(name: str) -> set[str]:
    # Fuse hyphens/apostrophes into the word instead of splitting on them
    # (e.g. "Xin-Yu" -> "xinyu", not "xin"/"yu" - confirmed live that
    # splitting broke matching a pick's "Xinyu" against 365scores' own
    # "Xin-Yu Wang" once the 2-letter "yu" piece got filtered out below).
    #
    # Only single-character tokens get filtered - confirmed live that
    # filtering anything <=2 chars caused a real mismatch: "LG Twins"
    # collapsed to just {"twins"} (since "lg" got dropped), which is then
    # trivially a subset of "Minnesota Twins"'s {"minnesota", "twins"} -
    # names_match() picked the wrong KBO team for a pick meant for the MLB
    # Minnesota Twins. Two-letter team prefixes (LG, KT, SK, NC in KBO; LA,
    # NY elsewhere) are exactly the kind of disambiguating qualifier this
    # match needs to keep, not discard.
    collapsed = re.sub(r"[-']", "", (name or "").lower())
    words = set(re.sub(r"[^a-z0-9\s]", " ", collapsed).split())
    filtered = {w for w in words if len(w) > 1}
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


def start_epoch(game: dict) -> float:
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
                rank == best_rank and abs(start_epoch(game) - now) < abs(start_epoch(best) - now)
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

def fmt_score(v) -> str:
    """365scores sends scores as floats (e.g. 13.0); drop the .0 for whole numbers."""
    return str(int(v)) if float(v).is_integer() else str(v)


def main_scores(game: dict) -> Optional[tuple]:
    """Raw (home, away) score pair, or None if the match hasn't started / has no score yet."""
    home_score = (game.get("homeCompetitor") or {}).get("score")
    away_score = (game.get("awayCompetitor") or {}).get("score")
    status = map_status_type(game.get("statusGroup"))
    # -1 is 365scores' own "no score yet" sentinel.
    if status == "notstarted" or home_score is None or away_score is None or home_score < 0 or away_score < 0:
        return None
    return (home_score, away_score)


def grade_moneyline(game: dict, picked_team: str) -> Optional[str]:
    """Grades a moneyline pick against a finished game's final score.
    Returns "won"/"lost"/"push" (a tie), or None if there's no final score
    yet or picked_team doesn't match either side."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    scores = main_scores(game)
    if not scores:
        return None
    home_score, away_score = scores
    if names_match(home, picked_team):
        picked_score, other_score = home_score, away_score
    elif names_match(away, picked_team):
        picked_score, other_score = away_score, home_score
    else:
        return None
    if picked_score == other_score:
        return "push"
    return "won" if picked_score > other_score else "lost"


_INNING_STAGE_NAMES = {
    1: "1st Inning", 2: "2nd Inning", 3: "3rd Inning", 4: "4th Inning", 5: "5th Inning",
    6: "6th Inning", 7: "7th Inning", 8: "8th Inning", 9: "9th Inning",
}


def innings_breakdown(game_id, through_inning: int) -> Optional[tuple[int, int]]:
    """Sums each side's runs through the given inning (e.g. through_inning=5
    for an F5/"First 5 Innings" moneyline pick), once that inning is fully
    complete. Confirmed live via the per-game detail call's `stages` array -
    each inning is its own entry (e.g. "5th Inning") with isEnded=True once
    it's actually finished (the current live inning shows isLive=True
    instead, and not-yet-reached innings carry neither flag with a -1
    sentinel score) - and this works for any league 365scores covers under
    a sport (confirmed live for both MLB and KBO games), unlike espn.py's
    equivalent used for YRFI/NRFI, which is hardcoded to the MLB endpoint
    only. Returns None if the target inning hasn't finished yet, an earlier
    inning is missing (e.g. a postponed/rain-shortened game), or the detail
    call failed."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    innings = {s.get("name"): s for s in (detail.get("stages") or [])}
    home_total = away_total = 0
    for n in range(1, through_inning + 1):
        stage = innings.get(_INNING_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            return None
        home_total += stage.get("homeCompetitorScore") or 0
        away_total += stage.get("awayCompetitorScore") or 0
    return int(home_total), int(away_total)


def grade_f5_moneyline(game: dict, home_runs: int, away_runs: int, picked_team: str) -> Optional[str]:
    """Grades an F5 (First 5 Innings) moneyline pick against the summed
    1st-5th inning score - same push-on-tie rule as grade_moneyline, just
    against the partial-game total instead of the final score."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, picked_team):
        picked_runs, other_runs = home_runs, away_runs
    elif names_match(away, picked_team):
        picked_runs, other_runs = away_runs, home_runs
    else:
        return None
    if picked_runs == other_runs:
        return "push"
    return "won" if picked_runs > other_runs else "lost"


def grade_f5_handicap(game: dict, home_runs: int, away_runs: int, team: str, line: float) -> Optional[str]:
    """Grades an F5 (First 5 Innings) run-line/handicap pick - the line is
    added to the picked team's own 1st-5th inning runs before comparing
    against the other side's (e.g. team +0.5 wins if it doesn't lose the F5
    window outright; team -1.5 needs to win the F5 window by 2+). A whole-
    number line can land on an exact tie after adjustment (push); a
    half-point line never can."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        picked_runs, other_runs = home_runs, away_runs
    elif names_match(away, team):
        picked_runs, other_runs = away_runs, home_runs
    else:
        return None
    adjusted = picked_runs + line
    if adjusted == other_runs:
        return "push"
    return "won" if adjusted > other_runs else "lost"


def grade_f5_team_total(
    game: dict, home_runs: int, away_runs: int, team: str, direction: str, line: float
) -> Optional[str]:
    """Grades an F5 (First 5 Innings) *team* total pick - one side's own
    1st-5th inning runs against a line, not compared against the other
    side (see grade_f5_moneyline for that). Returns "won"/"lost"/"push"
    (exact match), or None if team doesn't match either side."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        value = home_runs
    elif names_match(away, team):
        value = away_runs
    else:
        return None
    if value == line:
        return "push"
    if direction == "over":
        return "won" if value > line else "lost"
    return "won" if value < line else "lost"


def partial_f5_team_total(game_id, team: str, home_name: str, away_name: str) -> Optional[int]:
    """Sums whichever of the team's 1st-5th innings have actually completed
    so far, stopping at the first one that hasn't - unlike
    innings_breakdown, doesn't require the whole window to be done. Lets an
    Over pick be tagged a win the moment the partial total already clears
    the line, same idea as tracker.py/proptracker.py's early-win tagging,
    rather than waiting for all 5 innings to finish. Returns None if the
    team doesn't match either side, or nothing's completed yet."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    if names_match(home_name, team):
        key = "homeCompetitorScore"
    elif names_match(away_name, team):
        key = "awayCompetitorScore"
    else:
        return None
    innings = {s.get("name"): s for s in (detail.get("stages") or [])}
    total = 0
    counted_any = False
    for n in range(1, 6):
        stage = innings.get(_INNING_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            break
        total += stage.get(key) or 0
        counted_any = True
    return int(total) if counted_any else None


def partial_f5_combined_total(game_id) -> Optional[int]:
    """Sums both sides' 1st-5th inning runs combined, using whichever
    innings have completed so far (see partial_f5_team_total for the
    single-side equivalent) - lets a combined F5 Over pick get tagged a win
    early, same idea as the other early-win tagging. Returns None if
    nothing's completed yet."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    innings = {s.get("name"): s for s in (detail.get("stages") or [])}
    total = 0
    counted_any = False
    for n in range(1, 6):
        stage = innings.get(_INNING_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            break
        total += (stage.get("homeCompetitorScore") or 0) + (stage.get("awayCompetitorScore") or 0)
        counted_any = True
    return int(total) if counted_any else None


def grade_inning1_result(game: dict, home_runs: int, away_runs: int, pick: str) -> Optional[str]:
    """Grades a 3-way "1st inning result" pick - pick is either the literal
    "DRAW" or a team name backed to lead after the 1st inning. Unlike
    grade_f5_moneyline's push-on-tie, this is a genuine 3-way market: a tie
    is Draw's own winning outcome, and a team pick loses outright (not a
    push/void) if the inning ties. Returns None if pick is a team name that
    doesn't match either side."""
    if pick.upper() == "DRAW":
        return "won" if home_runs == away_runs else "lost"
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, pick):
        picked_runs, other_runs = home_runs, away_runs
    elif names_match(away, pick):
        picked_runs, other_runs = away_runs, home_runs
    else:
        return None
    return "won" if picked_runs > other_runs else "lost"


def grade_total(game: dict, direction: str, line: float) -> Optional[str]:
    """Grades a game-total (Over/Under combined final score) pick. Returns
    "won"/"lost"/"push" (exact match), or None if there's no final score yet."""
    scores = main_scores(game)
    if not scores:
        return None
    total = scores[0] + scores[1]
    if total == line:
        return "push"
    if direction == "over":
        return "won" if total > line else "lost"
    return "won" if total < line else "lost"


def grade_team_total(game: dict, team: str, direction: str, line: float) -> Optional[str]:
    """Grades a single team's own full-game score (not combined with the
    other side - see grade_total for that) against a line. Returns
    "won"/"lost"/"push" (exact match), or None if there's no final score yet
    or team doesn't match either side."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    scores = main_scores(game)
    if not scores:
        return None
    home_score, away_score = scores
    if names_match(home, team):
        value = home_score
    elif names_match(away, team):
        value = away_score
    else:
        return None
    if value == line:
        return "push"
    if direction == "over":
        return "won" if value > line else "lost"
    return "won" if value < line else "lost"


def grade_f5_combined_total(home_runs: int, away_runs: int, direction: str, line: float) -> Optional[str]:
    """Grades an F5 (First 5 Innings) combined-total pick - both sides'
    1st-5th inning runs summed together (see grade_f5_team_total for a
    single side's own total instead). Returns "won"/"lost"/"push" (exact
    match)."""
    total = home_runs + away_runs
    if total == line:
        return "push"
    if direction == "over":
        return "won" if total > line else "lost"
    return "won" if total < line else "lost"


def format_score_line(game: dict) -> str:
    home = (game.get("homeCompetitor") or {}).get("name", "?")
    away = (game.get("awayCompetitor") or {}).get("name", "?")
    scores = main_scores(game)
    if not scores:
        return f"{home} vs {away} — not started"
    return f"{home} {fmt_score(scores[0])} - {fmt_score(scores[1])} {away}"


def score_only_line(game: dict) -> str:
    """Just the number pair, e.g. '3 - 7', for a large embed headline."""
    scores = main_scores(game)
    if not scores:
        return "Not started"
    return f"{fmt_score(scores[0])} - {fmt_score(scores[1])}"


def _starts_in_text(game: dict) -> str:
    """Fixed Eastern-time kickoff, e.g. "17:00 Today" / "18:00 Tomorrow" /
    "18:00 Jul 29" for anything further out."""
    kickoff = start_epoch(game)
    if not kickoff:
        return "Kickoff: TBD"
    dt = datetime.datetime.fromtimestamp(kickoff, tz=EASTERN)
    today = datetime.datetime.now(tz=EASTERN).date()
    if dt.date() == today:
        day_label = "Today"
    elif dt.date() == today + datetime.timedelta(days=1):
        day_label = "Tomorrow"
    else:
        day_label = dt.strftime("%b %d")
    return f"{dt.strftime('%H:%M')} {day_label}"


def next_eastern_midnight_epoch(from_ts: float) -> float:
    """Epoch timestamp of the next Eastern-time midnight strictly after from_ts."""
    now = datetime.datetime.fromtimestamp(from_ts, tz=EASTERN)
    next_midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_midnight.timestamp()


def status_line(game: dict, sport_id: Optional[int] = None) -> str:
    status = map_status_type(game.get("statusGroup"))
    if status == "notstarted":
        return _starts_in_text(game)
    if status == "finished":
        return game.get("statusText") or "Final"

    text = game.get("statusText") or "Live"
    # Only basketball and soccer/football carry a real running clock here -
    # confirmed in the My Bookies port (threesixfive.js's liveClockInfo()
    # deliberately limits clock-building to just these two sports).
    # gameTimeDisplay/gameTime on every other sport (baseball, tennis, hockey,
    # NFL, volleyball, rugby) isn't a meaningful clock or counter at all.
    if sport_id == SPORT_IDS["basketball"]:  # gameTimeDisplay is already "MM:SS remaining"
        clock = game.get("gameTimeDisplay")
        return f"{text} ({clock})" if clock else text
    if sport_id == SPORT_IDS["soccer"]:  # gameTime is plain elapsed minutes (365scores gives no seconds)
        minutes = game.get("gameTime")
        return f"{text} ({int(minutes)}:00)" if minutes is not None else text
    return text

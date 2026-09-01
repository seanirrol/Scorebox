#!/usr/bin/env python3
"""
365scores.com live score client.

Ported from the same approach used in the Torn-BetSync project: 365scores runs
an open, unauthenticated JSON API with no anti-bot wall, covering soccer,
basketball, tennis, hockey, NFL, baseball, volleyball, and rugby in one
consistent shape - a better fit here than TheSportsDB's free tier, which has
no true live-score endpoint for most sports.
"""

import concurrent.futures
import datetime
import re
import time
import unicodedata
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


def sport_label(sport_id: Optional[int], competition_name: Optional[str] = None) -> Optional[str]:
    """competition_name (a game's own competitionDisplayName) lets basketball
    resolve to "WNBA" specifically instead of the generic "Basketball" -
    365scores' sport_id doesn't distinguish NBA from WNBA at all (both are
    sport_id 2), which used to split a WNBA game's moneyline/spread pick
    (tracker.py, generic across every sport) into a different /summary
    section than that same league's player props (proptracker.py, which
    already had "wnba" as an explicit string) - the same real league
    showing up under both "Basketball" and "WNBA" for no reason a bettor
    would expect. Confirmed live: competitionDisplayName is literally
    "WNBA" for a WNBA game.

    Same reasoning for baseball - 365scores' sport_id 7 spans MLB, KBO,
    NPB, etc. all at once (confirmed live: competitionDisplayName is
    literally "MLB" or "KBO" for those games), so a plain "Baseball" label
    used to lump every league together and away from proptracker.py's own
    league-specific "MLB"/kboproptracker.py's "KBO" label for the exact
    same real game - one real-world league is the whole point of
    /performance-style grouping (see dailylog.py's PERFORMANCE_CHANNEL_IDS
    and sport_tournament_win_loss).

    NBA checked here too (not just WNBA) so a tracker.py-driven NBA
    moneyline/spread pick tags "NBA" - same specific label
    proptracker.py's own NBA player props already use - instead of
    falling back to the generic "Basketball" bucket both WNBA and NBA used
    to share. Checked after "wnba" - "nba" is a substring of "wnba", so
    order matters here."""
    if sport_id == SPORT_IDS["basketball"] and competition_name:
        lowered = competition_name.lower()
        if "wnba" in lowered:
            return "WNBA"
        if "nba" in lowered:
            return "NBA"
    if sport_id == SPORT_IDS["baseball"] and competition_name:
        lowered = competition_name.lower()
        if "kbo" in lowered:
            return "KBO"
        if "mlb" in lowered:
            return "MLB"
    return SPORT_ID_LABELS.get(sport_id)


def tournament_name(game: dict) -> Optional[str]:
    """The specific tournament/competition/league a game belongs to (e.g.
    "MLB", "KBO", "Cincinnati" for a tennis event, "Premier League" for
    soccer) - confirmed live, 365scores' own competitionDisplayName
    already carries exactly this, it just needs a tennis-style trailing
    round suffix stripped first (e.g. "Cincinnati - 3rd Round", "Hamburg -
    Final" both need to fold into plain "Cincinnati"/"Hamburg" - a
    tournament's win rate should combine every round of it, not fragment
    further round by round)."""
    name = game.get("competitionDisplayName")
    if not name:
        return None
    return name.split(" - ")[0].strip() or None


_LOGO_URL_TEMPLATE = "https://imagecache.365scores.com/image/upload/f_png,w_100,h_100,c_limit,q_auto:eco,dpr_2/v{version}/Competitors/{id}"


def competitor_logo_url(competitor: dict) -> Optional[str]:
    """Confirmed live against a real competitor (id 7428, imageVersion 3 -> the St. Louis Cardinals' crest)."""
    comp_id, version = competitor.get("id"), competitor.get("imageVersion")
    if not comp_id or version is None:
        return None
    return _LOGO_URL_TEMPLATE.format(version=version, id=comp_id)


_ATHLETE_PHOTO_URL_TEMPLATE = "https://imagecache.365scores.com/image/upload/f_png,w_100,h_100,c_limit,q_auto:eco,dpr_2/v{version}/Athletes/{id}"


def athlete_photo_url(member: dict) -> Optional[str]:
    """A soccer roster member's own headshot - confirmed live this uses
    their athleteId (a stable cross-match person id), not their id (a
    per-match roster-entry id, which is what "members" is otherwise keyed
    by everywhere else, e.g. play-by-play events' playerId)."""
    athlete_id, version = member.get("athleteId"), member.get("imageVersion")
    if not athlete_id or version is None:
        return None
    return _ATHLETE_PHOTO_URL_TEMPLATE.format(version=version, id=athlete_id)

# statusGroup: 2 = not started, 3 = in progress, 4+ = terminal (ended/postponed/...)
_STATUS_RANK = {"inprogress": 0, "notstarted": 1, "finished": 2}

GAMES_CACHE_SECONDS = 8
EXTRA_PAGES = 4
# A match that started before "now" and is still not finished (e.g.
# suspended for rain/darkness and never resumed) would otherwise be
# invisible here forever - forward pagination only ever extends further
# into the future from "now", never backward. Confirmed live: a real
# tennis match interrupted the previous day and still unresolved could
# not be found by find_match_for_team no matter how many forward pages
# were walked, but appeared on the very first page back. Smaller than
# EXTRA_PAGES since this is specifically for catching stale
# still-in-progress stragglers, not for general list coverage - most of
# what a backward page turns up is long-finished games nobody needs.
EXTRA_PAGES_BACK = 2
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


PAGE_FETCH_RETRIES = 2


def _get_retrying(url: str, **params) -> Optional[dict]:
    """Same as _get, but retries once (PAGE_FETCH_RETRIES total attempts)
    before giving up - a single transient timeout/5xx on just ONE page of
    _fetch_games_for_sport's own multi-page walk used to silently truncate
    the whole list rather than retry, which could make a game that's
    genuinely still live "vanish" for one poll cycle even though it's
    sitting right there in the feed a moment before and after. Confirmed
    live: a real, still-in-progress volleyball match (Latvia vs Hungary)
    got auto-voided by a tracker's MAX_CONSECUTIVE_MISSES safety net after
    3 such misses in a row. Returns None (not raising) on total failure -
    every call site already treats a missing/short page the same way a
    genuinely-empty one is treated, so a caller doesn't need a new
    exception path here."""
    for attempt in range(PAGE_FETCH_RETRIES):
        try:
            return _get(url, **params)
        except ScoresError:
            if attempt == PAGE_FETCH_RETRIES - 1:
                return None
    return None


def _fetch_games_for_sport(sport_id: int) -> list[dict]:
    """Current games for a sport (live + today's schedule), lightly cached."""
    cached = _games_cache.get(sport_id)
    if cached and time.monotonic() - cached["fetched_at"] < GAMES_CACHE_SECONDS:
        return cached["games"]

    data = _get_retrying(f"{BASE_URL}/games/current/", langId=1, timezoneName="UTC", userCountryId=1, sports=sport_id)
    if data is None:
        raise ScoresError("365scores request failed after retries")

    # A base page can come back HTTP 200 with a technically-valid but empty
    # games list AND no paging object at all - not an exception _get_retrying
    # would ever catch, but just as transient (confirmed live: back-to-back
    # calls for the same sport/params flipped between 100 games and 0 games
    # with nothing else changing). A sport covering many leagues, several
    # pages forward/back, essentially never has zero real games - that
    # combination is the signal something's actually wrong, so retry the
    # base call a couple more times before trusting it, the same tolerance
    # PAGE_FETCH_RETRIES already gives an outright exception. Confirmed
    # live: this exact gap auto-voided a real in-progress volleyball pick,
    # after all 6 poll cycles in a row hit this empty response.
    if not data.get("games") and not data.get("paging"):
        for _ in range(PAGE_FETCH_RETRIES):
            data = _get_retrying(f"{BASE_URL}/games/current/", langId=1, timezoneName="UTC", userCountryId=1, sports=sport_id)
            if data and (data.get("games") or data.get("paging")):
                break

    if data is None:
        raise ScoresError("365scores request failed after retries")
    games = list(data.get("games") or [])

    paging = data.get("paging") or {}

    # The bulk endpoint is paginated; follow the cursor a few pages forward so
    # a high-volume sport (e.g. football) isn't limited to the next ~3 hours.
    next_page = paging.get("nextPage")
    for _ in range(EXTRA_PAGES):
        if not next_page:
            break
        page = _get_retrying(f"https://webws.365scores.com{next_page}", sports=sport_id)
        if page is None:
            break
        games.extend(page.get("games") or [])
        next_page = (page.get("paging") or {}).get("nextPage")

    # Also walk a couple pages backward - see EXTRA_PAGES_BACK's own comment.
    previous_page = paging.get("previousPage")
    for _ in range(EXTRA_PAGES_BACK):
        if not previous_page:
            break
        page = _get_retrying(f"https://webws.365scores.com{previous_page}", sports=sport_id)
        if page is None:
            break
        games.extend(page.get("games") or [])
        previous_page = (page.get("paging") or {}).get("previousPage")

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


def soccer_game_detail(game_id) -> Optional[dict]:
    """Public wrapper around _get_game_detail - soccerpropstracker.py's own
    polling needs the same full single-game object find_soccer_player
    already fetches (for its "events"/"members" - not present on the bulk
    games/current/ list), so this is just named for that call site rather
    than something volleyball-specific."""
    return _get_game_detail(game_id)


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


def volleyball_first_set_result(game: dict) -> Optional[tuple[int, int]]:
    """(home_points, away_points) for Set 1 once it's actually ended - None
    while Set 1 is still live or the match hasn't started yet. Lets a "1st
    Set" market settle as soon as Set 1 itself finishes, without waiting on
    the whole match - same early-decision shape as tennis_first_set_result."""
    status = map_status_type(game.get("statusGroup"))
    sets = volleyball_set_scores(SPORT_IDS["volleyball"], status, game.get("id"))
    if not sets:
        return None
    set1 = next((s for s in sets if s["set_number"] == 1 and not s["is_live"]), None)
    if not set1 or set1["home"] is None or set1["away"] is None:
        return None
    return (set1["home"], set1["away"])


def grade_volleyball_set1_handicap(game: dict, team: str, line: float) -> Optional[str]:
    """Grades a volleyball 1st-Set point-margin handicap pick (e.g. "Serbia
    -4.5 1st Set") - same adjust-then-compare shape as grade_games_handicap,
    but against Set 1's own final point score rather than games/sets won
    across the whole match."""
    breakdown = volleyball_first_set_result(game)
    if breakdown is None:
        return None
    home_points, away_points = breakdown
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        picked, other = home_points, away_points
    elif names_match(away, team):
        picked, other = away_points, home_points
    else:
        return None
    adjusted = picked + line
    if adjusted == other:
        return "push"
    return "won" if adjusted > other else "lost"


def volleyball_match_points(game: dict) -> tuple[int, int]:
    """(home_points, away_points) - combined rally points across every set
    played so far, NOT sets won (see main_scores, which is sets won e.g.
    3-1) - the running/final total a "Total Points"/"Points Handicap"
    market (distinct from "Total Sets"/"Set Handicap") grades against.
    Includes the current in-progress set's own partial score, same live-
    running shape as tennis_match_games. Returns (0, 0), not None, when
    nothing's been played yet - every call site already gates on match
    state (is_finished for final grading, status != "notstarted" for live
    display) same as tennis_match_games does."""
    status = map_status_type(game.get("statusGroup"))
    sets = volleyball_set_scores(SPORT_IDS["volleyball"], status, game.get("id"))
    if not sets:
        return (0, 0)
    home_total = sum(s["home"] for s in sets if s["home"] is not None)
    away_total = sum(s["away"] for s in sets if s["away"] is not None)
    return (home_total, away_total)


def grade_volleyball_match_point_handicap(game: dict, team: str, line: float) -> Optional[str]:
    """Grades a volleyball match-wide points-margin handicap pick (e.g.
    "Poland -4.5 Points") - same adjust-then-compare shape as
    grade_games_handicap, but against the combined rally-point total across
    the whole match (volleyball_match_points) rather than games/sets won.
    Only call once the match has actually finished. Voids on a walkover
    (see is_walkover) - that check's own underlying signature (finished,
    0-0 main_scores, exactly one side flagged isWinner) is sport-agnostic,
    not actually tennis-specific despite its docstring's own framing."""
    if is_walkover(game):
        return "void"
    home_points, away_points = volleyball_match_points(game)
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        picked, other = home_points, away_points
    elif names_match(away, team):
        picked, other = away_points, home_points
    else:
        return None
    adjusted = picked + line
    if adjusted == other:
        return "push"
    return "won" if adjusted > other else "lost"


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
    grading elsewhere in this module.

    Voids on a walkover (see is_walkover) - the home_games/away_games
    passed in reflect whatever partial set data existed before the
    opponent withdrew, not a real completed set, so there's nothing
    genuine to grade a set-winner pick against."""
    if is_walkover(game):
        return "void"
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, picked_team):
        picked_games, other_games = home_games, away_games
    elif names_match(away, picked_team):
        picked_games, other_games = away_games, home_games
    else:
        return None
    return "won" if picked_games > other_games else "lost"


def grade_games_handicap(game: dict, team: str, line: float) -> Optional[str]:
    """Grades a tennis games-margin handicap pick (e.g. "Brandon Nakashima
    -2.5 Games") - the line is added to the picked player's own total games
    won across the whole match before comparing against the opponent's
    (player -2.5 needs to win the match by 3+ games; player +2.5 wins as
    long as they don't lose by 3+). Same adjust-then-compare shape as
    grade_f5_handicap - a whole-number line can land on an exact tie after
    adjustment (push); a half-point line never can.

    Voids on a walkover (see is_walkover) - there's no real games-played
    data to adjust/compare against."""
    if is_walkover(game):
        return "void"
    home_games, away_games = tennis_match_games(game)
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        picked_games, other_games = home_games, away_games
    elif names_match(away, team):
        picked_games, other_games = away_games, home_games
    else:
        return None
    adjusted = picked_games + line
    if adjusted == other_games:
        return "push"
    return "won" if adjusted > other_games else "lost"


def grade_sets_handicap(game: dict, team: str, line: float) -> Optional[str]:
    """Grades a tennis sets-margin handicap pick (e.g. "Wang Xiyu +1.5
    Sets") - same adjust-then-compare shape as grade_games_handicap, but
    against sets won (main_scores, tennis's own win/loss score) rather than
    games won. Only call once the match has actually finished - main_scores
    mid-match reflects sets won so far, not the final tally, so grading
    early would be premature.

    Voids on a walkover (see is_walkover) - main_scores sits at 0-0 in
    that case (no set was ever actually decided), not a real "0 sets won"
    result to adjust/compare against."""
    if is_walkover(game):
        return "void"
    scores = main_scores(game)
    if not scores:
        return None
    home_sets, away_sets = scores
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        picked_sets, other_sets = home_sets, away_sets
    elif names_match(away, team):
        picked_sets, other_sets = away_sets, home_sets
    else:
        return None
    adjusted = picked_sets + line
    if adjusted == other_sets:
        return "push"
    return "won" if adjusted > other_sets else "lost"


def tennis_match_games(game: dict) -> tuple[int, int]:
    """(home_games, away_games) running totals across every set so far -
    unlike tennis_first_set_result, not gated on each set being fully
    complete (includes whatever the current, still-live set's partial score
    is too). Confirmed live: an unplayed Set N stage sits at
    homeCompetitorScore/awayCompetitorScore -1/-1 (365scores' own "not yet"
    sentinel, handled by _norm_score already), contributing 0 rather than
    needing special-casing here. Used both for live per-side display and,
    once the match itself finishes, final grading (home+away summed)
    against a "Total Games" line."""
    home_total = away_total = 0
    for stage in game.get("stages") or []:
        if not re.match(r"^Set \d+$", stage.get("name") or ""):
            continue
        home = _norm_score(stage.get("homeCompetitorScore"))
        away = _norm_score(stage.get("awayCompetitorScore"))
        if home is not None:
            home_total += home
        if away is not None:
            away_total += away
    return home_total, away_total


def tennis_sets_won(game: dict) -> tuple[int, int]:
    """(home_sets, away_sets) derived directly from each completed Set N
    stage's own score - deliberately NOT main_scores' aggregate
    homeCompetitor/awayCompetitor "score" field (what a plain sets-won
    tally would normally read). Confirmed live that field doesn't get
    incremented when a match ends by mid-set retirement, even though an
    earlier set fully completed (a real WTA match: Set 1 ended 5-3, the
    match then ended by retirement in Set 2, and main_scores still showed
    0-0 despite Set 1 clearly having a winner) - the settracker card for
    a "Win a Set"/sets-handicap pick showed a misleading 0-0 as if nothing
    had been played at all. A tie within one completed set can't happen in
    real tennis, so ties are simply skipped rather than needing a push
    case."""
    home_sets = away_sets = 0
    for stage in game.get("stages") or []:
        if not re.match(r"^Set \d+$", stage.get("name") or "") or not stage.get("isEnded"):
            continue
        home = _norm_score(stage.get("homeCompetitorScore"))
        away = _norm_score(stage.get("awayCompetitorScore"))
        if home is None or away is None or home == away:
            continue
        if home > away:
            home_sets += 1
        else:
            away_sets += 1
    return home_sets, away_sets


def grade_win_a_set(game: dict, picked_team: str, direction: str) -> Optional[str]:
    """Grades a "Player to Win a Set" (direction="yes") or "Player Not to
    Win a Set" (direction="no") pick. Winning at least one set is safe to
    grade a "yes" pick a win as soon as it happens - a player can't un-win a
    set - but a "no" pick (or a "yes" pick that hasn't happened yet) can
    only be graded once the whole match is over with the picked player
    still on zero sets won.

    Voids on a walkover (see is_walkover) - confirmed live, a real "Player
    to Win a Set" pick graded LOST for the player who actually won the
    match outright via walkover, since main_scores sits at 0-0 (no set was
    ever actually played) and used to be indistinguishable here from a
    genuine "lost every set" result."""
    if is_walkover(game):
        return "void"
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    scores = main_scores(game)
    if not scores:
        return None
    if names_match(home, picked_team):
        picked_sets = scores[0]
    elif names_match(away, picked_team):
        picked_sets = scores[1]
    else:
        return None
    if picked_sets >= 1:
        won_a_set = True
    elif is_finished(game):
        won_a_set = False
    else:
        return None
    return ("won" if won_a_set else "lost") if direction == "yes" else ("lost" if won_a_set else "won")


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


# --- soccer player props -----------------------------------------------

# Labels this bot can grade a soccer player prop against - "Assists" is
# handled specially in soccer_player_stat (365scores logs no separate
# "Assist" event type at all, only Goal events with an assisting player
# attached). Shots/Shots on Target aren't here - confirmed live 365scores
# only exposes those as team-level aggregates (see the /game/stats/
# endpoint used by tennis_player_stat above), never broken out per player.
SOCCER_STAT_CATALOG = {"Goals": "Goals", "Assists": "Assists", "Yellow Cards": "Yellow Cards", "Red Cards": "Red Cards"}

_SOCCER_EVENT_TYPES = {"Goals": "Goal", "Yellow Cards": "Yellow Card", "Red Cards": "Red Card"}

# How far ahead of kickoff a not-yet-started match is still worth searching
# for a named player - see find_soccer_player. Widened from 2h to 24h -
# confirmed live, 365scores' probable-lineup data (what find_soccer_player
# actually reads via _get_game_detail's "members" list) is already
# populated 16+ hours before kickoff, well outside the old 2h window - a
# soccer prop pick is virtually always about a same-day match anyway (same
# assumption dailylog's own date field already makes), so 24h covers the
# realistic case immediately instead of leaving it queued for hours. Costs
# more candidate matches per lookup (~90 at 24h vs ~0-50 at 2h, confirmed
# live), but fetches already run in parallel and are cached.
_SOCCER_PROP_SEARCH_WINDOW_SECONDS = 24 * 3600


def soccer_player_stat(game: dict, member_id, stat_label: str) -> Optional[int]:
    """Counts a player's occurrences of a stat from this game's own
    play-by-play event log (game["events"], from _get_game_detail) - unlike
    tennis, 365scores has no continuous per-player stat endpoint for soccer
    at all, only individually-logged events. Confirmed live against real
    matches (goals, assists via a goal's extraPlayers, and cards all
    resolve correctly this way). member_id must be a roster member's "id"
    (matches events' playerId/extraPlayers), not its "athleteId" (see
    athlete_photo_url) - the two are different id spaces."""
    events = game.get("events") or []
    if stat_label == "Assists":
        return sum(
            1 for e in events
            if e.get("eventType", {}).get("name") == "Goal" and member_id in (e.get("extraPlayers") or [])
        )
    event_type = _SOCCER_EVENT_TYPES.get(stat_label)
    if not event_type:
        return None
    return sum(1 for e in events if e.get("eventType", {}).get("name") == event_type and e.get("playerId") == member_id)


def find_soccer_player(player_name: str) -> Optional[tuple[dict, dict]]:
    """
    Searches currently live soccer matches, plus ones kicking off within the
    next couple hours, for a player by name. Unlike every other find_*
    lookup in this module, soccer's bulk game list only carries TEAM names
    (a "competitor" here is a club, not a person, unlike tennis) - there's
    no way to find which match a named player is in without actually
    opening each candidate match's own roster via _get_game_detail. Bounded
    to live + imminent matches (confirmed live: ~40-50 at any moment) and
    fetched in parallel, rather than every match 365scores currently has
    scheduled (860+) - a soccer prop pick is virtually never about a match
    still hours away with no lineup out yet, and scanning all of them would
    be both far too slow for a single auto-track lookup and needlessly
    heavy on 365scores.

    Returns (game, member) - game is the full single-game detail object
    (see _get_game_detail), member is that player's own entry from its
    "members" roster list. Prefers a live match over a not-yet-started one,
    then the soonest kickoff among not-yet-started candidates.
    """
    games = _fetch_games_for_sport(SPORT_IDS["soccer"])  # raises ScoresError if entirely unreachable

    now = time.time()
    candidates = []
    for game in games:
        status = map_status_type(game.get("statusGroup"), game.get("statusText"))
        if status == "inprogress":
            candidates.append((0, 0.0, game["id"]))
        elif status == "notstarted":
            kickoff = start_epoch(game)
            if kickoff and 0 < kickoff - now < _SOCCER_PROP_SEARCH_WINDOW_SECONDS:
                candidates.append((1, kickoff, game["id"]))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))

    game_ids = [c[2] for c in candidates]
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as pool:
        details = dict(zip(game_ids, pool.map(_get_game_detail, game_ids)))

    for _, _, game_id in candidates:
        detail = details.get(game_id)
        if not detail:
            continue
        for member in detail.get("members", []):
            if names_match(member.get("name", ""), player_name) or names_match(member.get("shortName", ""), player_name):
                return detail, member
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


def is_interrupted(game: dict) -> bool:
    """A paused/suspended match (see map_status_type) - used by trackers to
    tag a pick Voided/No Action if it's still sitting interrupted once
    MAX_TRACK_HOURS runs out, rather than leaving the card stuck showing
    "Interrupted" forever with no result and no cleanup."""
    return (game.get("statusText") or "").strip().lower() in _IRREGULAR_TERMINAL_STATUS_TEXTS


_CANCELLED_STATUS_TEXTS = {"cancelled", "canceled"}


def is_cancelled(game: dict) -> bool:
    """A match that will never be played at all - unlike is_interrupted
    (which might still resume) or a postponement (which waits for a new
    schedule), a cancelled match has definitively nothing left to wait for.
    Confirmed live: map_status_type maps this statusText the same as any
    other terminal state (only "interrupted" gets special treatment), so
    with no score ever recorded (main_scores stays at 365scores' -1 "no
    score yet" sentinel forever), a cancelled pick fell through to the
    generic "finished, nothing to grade" green fallback color - the same
    one meant for a manual /track with no pick attached - misleadingly
    showing green as if the match had settled normally. Trackers check
    this to force an immediate void instead."""
    return (game.get("statusText") or "").strip().lower() in _CANCELLED_STATUS_TEXTS


# --- fuzzy team-name matching ----------------------------------------------

def _strip_accents(text: str) -> str:
    # NFKD decomposes an accented character into its base letter plus a
    # separate combining-mark codepoint, which the [^a-z0-9] filters below
    # can then just drop - without this, the accent isn't stripped, the
    # whole letter is (a-z0-9 doesn't match "é" at all), silently mangling
    # the word instead of folding it to its unaccented equivalent.
    # Confirmed live: 365scores stores "Club América" with the accent, so a
    # pick parsed as plain "Club America" (no accent) never matched at all.
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _strip_accents(name or "").lower())


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
    collapsed = re.sub(r"[-']", "", _strip_accents(name or "").lower())
    words = set(re.sub(r"[^a-z0-9\s]", " ", collapsed).split())
    filtered = {w for w in words if len(w) > 1}
    return filtered or words


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def _fuzzy_word_match(a: str, b: str) -> bool:
    # Guarded by a length floor so short, unrelated words (e.g. "Al" vs
    # "Ed") can't collide on a small edit distance - only meaningfully
    # long words are compared this loosely.
    if len(a) < 5 or len(b) < 5:
        return a == b
    return _levenshtein(a, b) <= 2


_RESERVE_QUALIFIER_RE = re.compile(
    r"\b(u1[4-9]|u2[0-3]|ii|iii|reserves?|youth|academy|womens?|ladies|girls|juniors?|b)\b"
)


def _reserve_qualifiers(name: str) -> frozenset[str]:
    """Youth/reserve/women's-team qualifier tokens found in a raw team
    name (e.g. "u21", "b", "women") - kept separate from
    _meaningful_words()'s normal word set because that set can't see
    them: _normalize() fuses "U21" into "arsenalu21" as one
    substring-matchable blob, and _meaningful_words() filters out
    single-char tokens like "B". Both behaviors are correct for their
    own purpose but would otherwise let e.g. "Arsenal" match "Arsenal
    U21" or "Real Madrid" match "Real Madrid B" - two different teams
    that happen to share a name (confirmed live: a plain "Arsenal ML"
    pick auto-tracked against a same-day Arsenal U21 fixture instead of
    the actual first-team match, since 365scores lists both under the
    same club name)."""
    lowered = _strip_accents(name or "").lower()
    return frozenset(_RESERVE_QUALIFIER_RE.findall(lowered))


def names_match(a: str, b: str) -> bool:
    if _reserve_qualifiers(a) != _reserve_qualifiers(b):
        return False
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = _meaningful_words(a), _meaningful_words(b)
    if not wa or not wb:
        return False
    smaller, larger = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if smaller.issubset(larger):
        return True
    # Same word count and every word but one already matches exactly -
    # allow that single remaining pair to differ by a small edit distance,
    # since a first name is sometimes transliterated differently by two
    # sources (confirmed live: "Liudmila Samsonova" vs 365scores' own
    # "Ludmilla Samsonova" for the same player, edit distance 2). The
    # surname (or any other shared word) still has to match exactly, which
    # anchors this against matching two genuinely different people.
    if len(wa) == len(wb):
        unmatched_a, unmatched_b = wa - wb, wb - wa
        if len(unmatched_a) == 1 and len(unmatched_b) == 1:
            (word_a,), (word_b,) = unmatched_a, unmatched_b
            if _fuzzy_word_match(word_a, word_b):
                return True
    return False


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


def find_match_for_team(
    team: str, sport: Optional[str] = None, days_ahead: int = 1, days_back: int = 0, allow_finished: bool = False,
    reference_date: Optional[datetime.date] = None,
) -> Optional[tuple[dict, int]]:
    """
    Search 365scores' live game lists for a team, across all supported sports
    unless a specific one is given. Prefers a live match, then the
    soonest not-yet-started one within the allowed window. Returns
    (game, sport_id).

    "inprogress" is always accepted regardless of date - a live match is
    unambiguous. A "notstarted" candidate is only accepted within
    [today, today + days_ahead] (Eastern) - never in the past, and never
    further ahead than days_ahead lets it. A "finished" candidate is only
    ever accepted at all if allow_finished is set, and then only within
    [today - days_back, today].

    Defaults (days_ahead=1, days_back=0, allow_finished=False) are the
    auto-track pipeline's own rule: a fresh pick posted from a picks
    channel is always about a live match, or the soonest upcoming one
    today or tomorrow - never an already-finished game, at any date, and
    never anything further out than tomorrow (confirmed live: GreenFox
    only ever posts for today's or the next day's slate - a team's actual
    next game sitting several days out, e.g. a bye day, must resolve to
    nothing here rather than silently attaching a pick to a match it was
    never about). bot.py's /tracktoday command is the one caller that
    passes different bounds (days_ahead=0, days_back=1, allow_finished=
    True) - today's or yesterday's match, live/upcoming/already finished,
    whichever is most recent - since it exists specifically to manually
    track a pick against a match that's already wrapped up.

    reference_date, when given, replaces "today" as both the window's own
    anchor AND the tie-break's notion of "now" (see below) - lets a caller
    re-resolve a pick against the SAME game it originally meant, even
    after the team has since played again. Confirmed live: masterparlay.py
    re-resolving a days-old slip against "Philadelphia Phillies" picked up
    a brand-new Phillies game instead of the one the slip actually
    tracked, because the tie-break below always preferred whichever
    candidate was closest to the real current moment - which, once a new
    game exists, is never the old one anymore. None (the default) keeps
    every other caller's existing "as of right now" behavior unchanged."""
    sport_ids = [SPORT_IDS[sport.lower()]] if sport and sport.lower() in SPORT_IDS else UNIQUE_SPORT_IDS
    today = reference_date or datetime.datetime.now(tz=EASTERN).date()
    now = (
        datetime.datetime.combine(today, datetime.time(12, 0), tzinfo=EASTERN).timestamp()
        if reference_date else time.time()
    )
    earliest_finished_date = today - datetime.timedelta(days=days_back)
    latest_notstarted_date = today + datetime.timedelta(days=days_ahead)

    best = None
    best_sport_id = None
    best_rank = None
    for sport_id in sport_ids:
        try:
            games = _fetch_games_for_sport(sport_id)
        except ScoresError:
            continue
        for game in _candidates_for_team(games, team):
            status = map_status_type(game.get("statusGroup"))
            date = eastern_date(start_epoch(game))
            if status == "finished":
                if not allow_finished or date < earliest_finished_date or date > today:
                    continue
            elif status == "notstarted":
                if date < today or date > latest_notstarted_date:
                    continue
            elif status != "inprogress":
                continue
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


def is_walkover(game: dict) -> bool:
    """True for a tennis match that ended before any actual play happened -
    an opponent withdrew before the first ball was struck (see
    grade_moneyline's own comment for how this shows up in 365scores'
    data: both sides sitting at a 0-0 main_scores with one side flagged
    isWinner). A genuine completed tennis match can never end 0-0 - at
    least one game in one set is always played - so `finished` + 0-0 is an
    unambiguous walkover signature, not a coincidence.

    grade_moneyline handles this fine on its own (the isWinner flag alone
    settles who "won" regardless of score) but every other tennis grading
    function needs this check explicitly - games handicap, sets handicap,
    set-winner, win-a-set, and total-games markets all need real per-set/
    per-game data that simply doesn't exist on a walkover, and would
    otherwise silently compute a wrong result from the empty scoreline
    (confirmed live: a real "Player to Win a Set" pick graded LOST for the
    player who won the match outright via walkover)."""
    if not is_finished(game):
        return False
    if main_scores(game) != (0, 0):
        return False
    home_is_winner = (game.get("homeCompetitor") or {}).get("isWinner")
    away_is_winner = (game.get("awayCompetitor") or {}).get("isWinner")
    return bool(home_is_winner) != bool(away_is_winner)


def grade_moneyline(game: dict, picked_team: str) -> Optional[str]:
    """Grades a moneyline pick against a finished game's final score.
    Returns "won"/"lost"/"push" (a tie), or None if there's no final score
    yet or picked_team doesn't match either side.

    Confirmed live: a tennis WalkOver has both competitors' score sitting
    at 0-0 (no sets ever played) - naively comparing those as equal scores
    graded it a "push", when 365scores' own homeCompetitor/awayCompetitor
    "isWinner" flag already correctly says who actually won (the player
    who didn't withdraw). Checked first, ahead of the score comparison -
    exactly one side being flagged the winner settles it regardless of
    score; a genuine tie (neither side flagged, e.g. a soccer draw) falls
    through to the existing score-based push logic unaffected."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    home, away = home_competitor.get("name", ""), away_competitor.get("name", "")
    home_is_winner, away_is_winner = home_competitor.get("isWinner"), away_competitor.get("isWinner")
    if home_is_winner and not away_is_winner:
        winner = home
    elif away_is_winner and not home_is_winner:
        winner = away
    else:
        winner = None
    if winner is not None:
        if names_match(winner, picked_team):
            return "won"
        if names_match(home, picked_team) or names_match(away, picked_team):
            return "lost"
        return None
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


def grade_double_chance(game: dict, covered: tuple[str, str]) -> Optional[str]:
    """Grades a soccer Double Chance pick - covers two of the three
    possible full-time outcomes (home win/draw/away win) in one pick, e.g.
    ("Paris FC", "DRAW") for "Paris FC or Draw" ("1X"), ("DRAW", "Nice")
    for "Draw or Nice" ("X2"), or (home, away) for "Paris FC or Nice"
    ("12" - anyone but a draw). Wins if the match's actual full-time result
    matches EITHER covered outcome, otherwise loses - never a push, unlike
    a plain moneyline (that's the whole point of covering two outcomes at
    once). Graded off main_scores directly rather than isWinner (unlike
    grade_moneyline) so a cup match decided by extra time/penalties still
    settles on the scoreboard result - Double Chance is a full-time-only
    market at every real sportsbook, same convention as grade_ht_ft's own
    fulltime leg."""
    if not is_finished(game):
        return None
    scores = main_scores(game)
    if not scores:
        return None
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    home_score, away_score = scores
    if home_score > away_score:
        actual = home
    elif away_score > home_score:
        actual = away
    else:
        actual = "DRAW"
    for side in covered:
        if side == "DRAW":
            if actual == "DRAW":
                return "won"
        elif names_match(side, actual):
            return "won"
    return "lost"


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


_QUARTER_STAGE_NAMES = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}
THROUGH_1H_QUARTER = 2


def quarters_breakdown(game_id, through_quarter: int) -> Optional[tuple[int, int]]:
    """Football/basketball equivalent of innings_breakdown - sums each
    side's points through the given quarter (through_quarter=2 for a 1st
    Half pick), once that quarter is fully complete. Confirmed live via
    the per-game detail call's `stages` array - each quarter is its own
    entry ("Q1", "Q2", ...) with the same homeCompetitorScore/
    awayCompetitorScore/isEnded shape as baseball's innings, for both
    football (NFL/CFL) and basketball (WNBA/NBA) alike. Returns None if
    the target quarter hasn't finished yet, an earlier quarter is missing,
    or the detail call failed."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    quarters = {s.get("name"): s for s in (detail.get("stages") or [])}
    home_total = away_total = 0
    for n in range(1, through_quarter + 1):
        stage = quarters.get(_QUARTER_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            return None
        home_total += stage.get("homeCompetitorScore") or 0
        away_total += stage.get("awayCompetitorScore") or 0
    return int(home_total), int(away_total)


def grade_1h_team_total(
    game: dict, home_points: int, away_points: int, team: str, direction: str, line: float
) -> Optional[str]:
    """Grades a 1st Half *team* total pick - one side's own Q1+Q2 points
    against a line, not compared against the other side (see
    grade_1h_combined_total for that). Returns "won"/"lost"/"push" (exact
    match), or None if team doesn't match either side."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if names_match(home, team):
        value = home_points
    elif names_match(away, team):
        value = away_points
    else:
        return None
    if value == line:
        return "push"
    if direction == "over":
        return "won" if value > line else "lost"
    return "won" if value < line else "lost"


def grade_1h_combined_total(home_points: int, away_points: int, direction: str, line: float) -> Optional[str]:
    """Grades a 1st Half combined-total pick - both sides' Q1+Q2 points
    summed together (see grade_1h_team_total for a single side's own total
    instead). Returns "won"/"lost"/"push" (exact match)."""
    total = home_points + away_points
    if total == line:
        return "push"
    if direction == "over":
        return "won" if total > line else "lost"
    return "won" if total < line else "lost"


def grade_ht_ft(game: dict, ht_team: str, ft_team: str, sport_id: Optional[int] = None) -> Optional[str]:
    """Grades a Halftime/Fulltime pick - a compound bet needing BOTH legs
    to hit: ht_team must be strictly ahead (not tied) at the half, AND
    ft_team must be the final winner. Decided early (lost) the moment
    halftime ends with the wrong team leading (or tied), without waiting
    for the whole game to finish - once that leg fails, the compound bet
    is already lost regardless of how the rest of the game goes, same
    "can't come back from a wrong leg" shape as a real sportsbook grades
    this market. Doesn't support a "Draw" selection for either leg (not
    asked for, and a genuine fulltime tie can't happen in basketball/
    football/hockey anyway - only relevant for soccer, which isn't wired
    up to this market yet).

    sport_id switches the "first half" leg's own data source: volleyball
    has no halftime concept, but its own sportsbooks offer the identical
    compound shape as "Double Result (1st Set/Match)" - passing
    SPORT_IDS["volleyball"] here grades that leg off Set 1's own final
    score (volleyball_first_set_result) instead of quarters_breakdown.
    Every other sport_id (including None, the default) keeps the original
    quarters-based behavior."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    if sport_id == SPORT_IDS["volleyball"]:
        first_leg = volleyball_first_set_result(game)
    else:
        first_leg = quarters_breakdown(game["id"], THROUGH_1H_QUARTER)
    if first_leg is None:
        return None
    home_half, away_half = first_leg
    if home_half > away_half:
        ht_leader = home
    elif away_half > home_half:
        ht_leader = away
    else:
        ht_leader = None  # tied at the half/set - no named team can match
    if not (ht_leader and names_match(ht_leader, ht_team)):
        return "lost"
    if not is_finished(game):
        return None
    return grade_moneyline(game, ft_team)


def partial_1h_team_total(game_id, team: str, home_name: str, away_name: str) -> Optional[int]:
    """Sums whichever of the team's Q1/Q2 quarters have actually completed
    so far, stopping at the first one that hasn't - unlike
    quarters_breakdown, doesn't require the whole half to be done. Lets an
    Over pick be tagged a win the moment the partial total already clears
    the line. Returns None if the team doesn't match either side, or
    nothing's completed yet."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    if names_match(home_name, team):
        key = "homeCompetitorScore"
    elif names_match(away_name, team):
        key = "awayCompetitorScore"
    else:
        return None
    quarters = {s.get("name"): s for s in (detail.get("stages") or [])}
    total = 0
    counted_any = False
    for n in range(1, THROUGH_1H_QUARTER + 1):
        stage = quarters.get(_QUARTER_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            break
        total += stage.get(key) or 0
        counted_any = True
    return int(total) if counted_any else None


def partial_1h_combined_total(game_id) -> Optional[int]:
    """Sums both sides' Q1+Q2 points combined, using whichever quarters have
    completed so far (see partial_1h_team_total for the single-side
    equivalent) - lets a combined 1H Over pick get tagged a win early.
    Returns None if nothing's completed yet."""
    detail = _get_game_detail(game_id)
    if not detail:
        return None
    quarters = {s.get("name"): s for s in (detail.get("stages") or [])}
    total = 0
    counted_any = False
    for n in range(1, THROUGH_1H_QUARTER + 1):
        stage = quarters.get(_QUARTER_STAGE_NAMES.get(n))
        if not stage or not stage.get("isEnded"):
            break
        total += (stage.get("homeCompetitorScore") or 0) + (stage.get("awayCompetitorScore") or 0)
        counted_any = True
    return int(total) if counted_any else None


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


def grade_spread(game: dict, team: str, line: float) -> Optional[str]:
    """Grades a full-game point-spread (handicap) pick - the line is added
    to the picked team's own final score before comparing against the
    other side's (e.g. team -3.5 needs to win by 4+; team +3.5 covers
    unless it loses by 4+ - same math as grade_f5_handicap, just against
    the whole game's score instead of just the first 5 innings). Returns
    "won"/"lost"/"push" (a whole-number line landing on an exact tie after
    adjustment), or None if there's no final score yet or team doesn't
    match either side."""
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    scores = main_scores(game)
    if not scores:
        return None
    home_score, away_score = scores
    if names_match(home, team):
        picked_score, other_score = home_score, away_score
    elif names_match(away, team):
        picked_score, other_score = away_score, home_score
    else:
        return None
    adjusted = picked_score + line
    if adjusted == other_score:
        return "push"
    return "won" if adjusted > other_score else "lost"


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


def eastern_date(epoch: float) -> datetime.date:
    """The Eastern-time calendar date for an epoch timestamp - used to
    detect a genuine reschedule (kickoff moved to a different day) versus
    a match that's simply still hours away on the same day it was always
    scheduled for."""
    return datetime.datetime.fromtimestamp(epoch, tz=EASTERN).date()


def eastern_date_str(epoch: float) -> Optional[str]:
    """Same as eastern_date, but "YYYY-MM-DD" (matches dailylog.today_str's
    own format, since this is what every tracker passes as record_pick's
    game_date) - None for start_epoch's 0.0 "genuinely missing" sentinel
    rather than silently returning the 1970 epoch date, so callers fall
    back to today_str() instead of logging a nonsense date."""
    if not epoch:
        return None
    return eastern_date(epoch).isoformat()


def eastern_date_str_from_iso(iso_str: Optional[str]) -> Optional[str]:
    """Same as eastern_date_str, but takes a raw ISO 8601 UTC date string
    directly (ESPN/UFC/boxing's own "date" fields are always this shape,
    e.g. competition["date"]) instead of an already-computed epoch - saves
    every caller from repeating the same fromisoformat/"Z"-replace
    boilerplate. None for a missing or unparseable string."""
    if not iso_str:
        return None
    try:
        epoch = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return eastern_date_str(epoch)


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

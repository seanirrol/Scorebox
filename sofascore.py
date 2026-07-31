#!/usr/bin/env python3
"""
Sofascore.com client for player-level prop stat tracking (goals, RBI, earned
runs, etc.) - a different data source from scores365.py, needed because
365scores has no per-player stat data at all (confirmed live: neither its
bulk game list nor its per-game detail endpoint carry anything below
team/period level).

Sofascore's own API blocks Python's `requests` (and Node's `fetch`) via TLS
fingerprinting - confirmed live: `requests` gets a 403, the actual `curl`
binary gets a 200 for the exact same URL. Every call here shells out to curl
instead, the same workaround already used in the My Bookies project's
sofascore.js.
"""

import json
import subprocess
import time
import urllib.parse
from typing import Optional

BASE_URL = "https://api.sofascore.com/api/v1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CURL_TIMEOUT_SECONDS = 8


class SofascoreError(Exception):
    pass


def _curl_json(url: str) -> dict:
    try:
        result = subprocess.run(
            ["curl", "-s", "-A", USER_AGENT, url],
            capture_output=True, timeout=CURL_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        raise SofascoreError(f"Sofascore request failed: {e}") from e
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise SofascoreError("Sofascore is unreachable or blocked this request") from e
    if isinstance(data, dict) and "error" in data:
        raise SofascoreError(f"Sofascore error: {data['error']}")
    return data


# Our sport keys (matching scores365.SPORT_IDS) -> Sofascore's own sport name,
# as it appears on player.team.sport.name / team.sport.name. Confirmed live
# for each (see verification notes in the stat catalog below).
SPORT_NAMES = {
    "soccer": "Football",
    "basketball": "Basketball",
    "tennis": "Tennis",
    "hockey": "Ice Hockey",
    "nfl": "American Football",
    "baseball": "Baseball",
    "volleyball": "Volleyball",
    "rugby": "Rugby",
}

# Short display label for the card header - matches the /playerprops dropdown
# labels rather than Sofascore's own (sometimes ambiguous, e.g. "Football") names.
SPORT_DISPLAY_LABELS = {
    "soccer": "Soccer",
    "basketball": "Basketball",
    "tennis": "Tennis",
    "hockey": "Hockey",
    "nfl": "NFL",
    "baseball": "Baseball",
    "volleyball": "Volleyball",
    "rugby": "Rugby",
}

# Confirmed live: volleyball's /lineups endpoint 404s for every team/match
# tried (a Brazilian club, a Polish national team match) - Sofascore only
# exposes team-level aggregate stats for it, no per-player breakdown at all.
UNSUPPORTED_SPORTS = {"volleyball"}


def team_logo_url(team_id: int) -> str:
    """Confirmed live against Arsenal (team id 42) - a real PNG crest, not a placeholder."""
    return f"{BASE_URL}/team/{team_id}/image"


def player_photo_url(player_id: int) -> str:
    """Confirmed live against LeBron James (player id 817181) - a real headshot, not a placeholder."""
    return f"{BASE_URL}/player/{player_id}/image"

# Curated common stats per sport (label -> Sofascore's own field key), not
# the full 20-80+ fields each sport actually exposes. Every key below was
# confirmed live against a real finished match before being added here.
STAT_CATALOG: dict[str, dict[str, str]] = {
    "baseball": {
        "Hits": "battingHits",
        "RBI": "battingRbi",
        "Home Runs": "battingHomeRuns",
        "Runs": "battingRuns",
        "Walks": "battingBaseOnBalls",
        "Strikeouts": "pitchingStrikeOuts",
        "Earned Runs": "pitchingEarnedRuns",
        "ERA": "pitchingEarnedRunsAverage",
    },
    "basketball": {
        "Points": "points",
        "Rebounds": "rebounds",
        "Assists": "assists",
        "Steals": "steals",
        "Blocks": "blocks",
        "3-Pointers Made": "threePointsMade",
    },
    "soccer": {
        "Goals": "goals",
        "Assists": "goalAssist",
        "Shots": "totalShots",
        "Shots on Target": "onTargetScoringAttempt",
        "Tackles": "totalTackle",
    },
    "hockey": {
        "Goals": "goals",
        "Assists": "assists",
        "Points": "points",
        "Shots": "shots",
        "Power Play Points": "powerPlayPoints",
        "Penalty Minutes": "penaltyMinutes",
    },
    "nfl": {
        "Passing Yards": "passingYards",
        "Passing TDs": "passingTouchdowns",
        "Rushing Yards": "rushingYards",
        "Rushing TDs": "rushingTouchdowns",
        "Receiving Yards": "receivingYards",
        "Receiving TDs": "receivingTouchdowns",
        "Sacks": "defensiveSacks",
        "Interceptions": "defensiveInterceptions",
    },
    "tennis": {
        "Aces": "aces",
        "Double Faults": "doubleFaults",
        "Winners": "winnersTotal",
        "Unforced Errors": "unforcedErrorsTotal",
        "Break Points Won": "breakPointsScored",
    },
    "rugby": {
        "Tries": "tries",
        "Tackles": "tackles",
        "Meters Run": "metersRun",
        "Conversions": "conversions",
        "Penalty Goals": "penaltyGoals",
    },
}


def find_player(name: str, sport: str) -> Optional[dict]:
    """
    Search for a player by name within a sport.

    Tennis players are modeled as "team" entities on Sofascore (a singles
    player is a 1-person "team") - confirmed live via Carlos Alcaraz's
    search result. Every other sport uses normal "player" entities, whose
    search results don't reliably carry a sport field (confirmed: this is
    exactly what caused a same-named footballer to be picked up instead of
    the tennis player during testing) - so each candidate's sport is
    confirmed via a follow-up /player/{id} lookup's `team.sport.name`.

    Returns {"id": ..., "name": ..., "is_tennis": bool, "team_id": ...} or None.
    For tennis, "team_id" is the same as "id" (the player IS the team entity).
    """
    sport_name = SPORT_NAMES.get(sport)
    if not sport_name:
        return None

    data = _curl_json(f"{BASE_URL}/search/all?q={urllib.parse.quote(name)}")
    results = data.get("results", [])

    if sport == "tennis":
        for r in results:
            e = r.get("entity", {})
            if r.get("type") == "team" and e.get("sport", {}).get("name") == "Tennis":
                return {"id": e["id"], "name": e.get("name", name), "is_tennis": True, "team_id": e["id"]}
        return None

    candidates = [r["entity"] for r in results if r.get("type") == "player"][:5]
    for candidate in candidates:
        try:
            detail = _curl_json(f"{BASE_URL}/player/{candidate['id']}")
        except SofascoreError:
            continue
        player = detail.get("player", {})
        team = player.get("team", {})
        if team.get("sport", {}).get("name") == sport_name:
            return {"id": candidate["id"], "name": player.get("name", name), "is_tennis": False, "team_id": team.get("id")}
    return None


def _pick_event(events: list[dict]) -> Optional[dict]:
    """Prefer a currently in-progress event, else the most recent one."""
    if not events:
        return None
    live = [e for e in events if e.get("status", {}).get("type") == "inprogress"]
    return live[0] if live else events[-1]


def find_current_event(entity_id: int, is_tennis: bool) -> Optional[dict]:
    """
    The player's (or, for tennis, the player-as-team's) current live match,
    or their most recently finished one if nothing is live right now.
    """
    kind = "team" if is_tennis else "player"
    try:
        data = _curl_json(f"{BASE_URL}/{kind}/{entity_id}/events/last/0")
    except SofascoreError:
        return None
    return _pick_event(data.get("events", []))


def get_event(event_id) -> Optional[dict]:
    """Fresh details (status, scores, team names) for a specific event by id."""
    try:
        data = _curl_json(f"{BASE_URL}/event/{event_id}")
    except SofascoreError:
        return None
    return data.get("event")


def is_finished(event: dict) -> bool:
    return event.get("status", {}).get("type") == "finished"


def grade_over_under(value, direction: str, line: float) -> Optional[str]:
    """Grades an over/under prop against a finished event's final stat value.
    Returns "won"/"lost"/"push" (exactly on the line), or None if value isn't
    a usable number (e.g. the player never appeared in the match stats)."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric == line:
        return "push"
    if direction == "over":
        return "won" if numeric > line else "lost"
    return "won" if numeric < line else "lost"


# Sofascore's own status.description already gives the correct sport-specific
# label (confirmed live: "3rd quarter" for basketball, "1st half" for soccer),
# so this only adds a clock on top for the 3 sports it was asked for rather
# than reinventing that text. Basketball/hockey share the same countdown-clock
# shape (time.played/periodLength); hockey's specifically is inferred from
# basketball's (period-clock team sport, same broadcast convention) since no
# live hockey match was available anywhere to verify directly - genuinely
# zero games scheduled across a 5-day window when checked. Soccer counts up
# instead (time.currentPeriodStartTimestamp), confirmed live separately.
def match_status_text(event: dict, sport: str) -> str:
    status = event.get("status", {}) or {}
    status_type = status.get("type")

    if status_type == "finished":
        return "Finished"

    if status_type == "notstarted":
        start_ts = event.get("startTimestamp")
        if not start_ts:
            return "Not Started"
        seconds = start_ts - time.time()
        if seconds <= 0:
            return "Starting soon"
        hours, minutes = divmod(int(seconds // 60), 60)
        return f"Starts in {hours}h{minutes:02d}m" if hours else f"Starts in {minutes}m"

    description = status.get("description") or "In Play"
    time_info = event.get("time", {}) or {}

    if sport in ("basketball", "hockey"):
        played = time_info.get("played")
        period_length = time_info.get("periodLength")
        if played is not None and period_length:
            remaining = max(period_length - (played % period_length), 0)
            minutes, seconds = divmod(int(remaining), 60)
            return f"{description} ({minutes}:{seconds:02d})"
        return description

    if sport == "soccer":
        start_ts = time_info.get("currentPeriodStartTimestamp")
        if start_ts:
            # "initial" is the cumulative elapsed seconds from prior periods
            # (confirmed live: 0 for 1st half, 2700 (=45 min) for 2nd half) -
            # without it the 2nd half would restart counting from 0 instead
            # of continuing from 45'.
            prior_seconds = time_info.get("initial") or 0
            elapsed_min = max(int((prior_seconds + (time.time() - start_ts)) // 60), 0)
            return f"{description} ({elapsed_min}')"
        return description

    return description


def get_stat_value(
    event: dict, entity_id: int, is_tennis: bool, stat_key: str
) -> tuple[Optional[float], Optional[bool]]:
    """
    Returns (value, is_home). is_home is determined from this event's own
    roster/side data, NOT from the player's separately-cached team
    affiliation - confirmed live that field can be stale (a real, currently
    active NBA player's `player.team` showed a team he's never played for).
    is_home is None if the player couldn't be located in this event at all
    (e.g. lineups not posted yet for a not-yet-started match).
    """
    event_id = event["id"]

    if is_tennis:
        is_home = event.get("homeTeam", {}).get("id") == entity_id
        try:
            data = _curl_json(f"{BASE_URL}/event/{event_id}/statistics")
        except SofascoreError:
            return None, is_home
        for period in data.get("statistics", []):
            if period.get("period") != "ALL":
                continue
            for group in period.get("groups", []):
                for item in group.get("statisticsItems", []):
                    if item.get("key") == stat_key:
                        return (item.get("homeValue") if is_home else item.get("awayValue")), is_home
        return None, is_home

    try:
        data = _curl_json(f"{BASE_URL}/event/{event_id}/lineups")
    except SofascoreError:
        return None, None
    for side in ("home", "away"):
        for p in data.get(side, {}).get("players", []):
            if p.get("player", {}).get("id") == entity_id:
                return p.get("statistics", {}).get(stat_key), side == "home"
    return None, None

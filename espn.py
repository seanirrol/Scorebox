#!/usr/bin/env python3
"""
ESPN.com's free, unauthenticated site API - powers /playerprops for team
sports (baseball, basketball, NFL, hockey). Confirmed live: player search,
athlete->team resolution, team->current-event lookup via the league
scoreboard, and per-player boxscore stats - including real-time updates
during an actual in-progress MLB game (verified live, not assumed). No
TLS-fingerprint blocking encountered here (unlike Sofascore) - plain
`requests` works fine, no curl workaround needed.

Tennis and soccer aren't supported yet - both need bespoke per-competition
handling on ESPN's side (a tennis "event" is a whole tournament with many
matches nested inside; soccer needs a specific league code per competition)
rather than the team-based scoreboard lookup used here for the other sports.
"""

import datetime
import time
from typing import Optional

import requests

import scores365

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SEARCH_URL = "https://site.web.api.espn.com/apis/search/v2"
ATHLETE_BASE = "https://site.api.espn.com/apis/common/v3/sports"
REQUEST_TIMEOUT = 6

UNSUPPORTED_SPORTS = {"tennis", "soccer", "volleyball", "rugby"}

# Our sport keys -> (espn sport slug, espn league slug). Confirmed live.
SPORT_PATHS = {
    "baseball": ("baseball", "mlb"),
    "basketball": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "nfl": ("football", "nfl"),
    "hockey": ("hockey", "nhl"),
}

SPORT_DISPLAY_LABELS = {
    "baseball": "MLB",
    "basketball": "NBA",
    "wnba": "WNBA",
    "nfl": "NFL",
    "hockey": "NHL",
}

# Sentinel stat_key for computed stats that don't come from a simple boxscore
# label lookup - handled specially in get_stat_value.
TOTAL_BASES_KEY = ("__computed__", "total_bases")

# Combined-stat props (e.g. "P+R+A") aren't their own boxscore column -
# they're a sum of existing single-stat columns. Computed the same way as
# TOTAL_BASES_KEY: a sentinel key handled specially in get_stat_value, which
# sums the listed component (label, discriminator) pairs via ordinary
# recursive get_stat_value calls rather than a bespoke play-by-play scan
# (unlike Total Bases, every component here already has its own plain
# boxscore column, so no play-by-play digging is needed).
PRA_KEY = ("__computed__", "pra")
_COMBO_STAT_COMPONENTS = {
    PRA_KEY: (("PTS", None), ("REB", None), ("AST", None)),
}

# ESPN's raw "IP" boxscore field uses baseball notation - "5.2" means 5 full
# innings plus 2 outs (17 outs total), NOT 5.2 decimal innings - confirmed
# live against a real finished game's boxscore ("0.1"/"0.2"/"1.0" for 1/2/3
# outs). "Pitching Outs" is a real, distinct prop market (outs recorded, not
# innings) that needs this converted to a plain integer rather than read as
# a raw label - handled specially in get_stat_value like TOTAL_BASES_KEY.
PITCHING_OUTS_KEY = ("__computed__", "pitching_outs")

# ESPN reports these boxscore labels as a "made-attempted" string (e.g.
# "1-6"), never a plain number - confirmed live for every athlete in a real
# WNBA boxscore's "3PT" column. Passing that straight to float() in
# grade_over_under always raised, so a 3-Pointers Made prop could never
# grade won/lost and silently voided after timing out instead. Only the
# made count (before the "-") is what the line actually grades against.
_MADE_ATTEMPTED_LABELS = {"3PT"}

# label -> (label within its stat group, a second "discriminator" label that
# must also be present in that same group). ESPN's stat groups don't carry a
# usable "name" field for baseball/basketball (confirmed None live), and some
# labels are ambiguous across groups (e.g. baseball's "K" means strikeouts
# both batting and pitching) - the discriminator disambiguates which group is
# meant. None means "no ambiguity, match the label in any group".
STAT_CATALOG = {
    "baseball": {
        "Hits": ("H", "AB"),
        "Runs": ("R", "AB"),
        "RBIs": ("RBI", "AB"),
        "Home Runs": ("HR", "AB"),
        "Walks": ("BB", "AB"),
        "Strikeouts (Batting)": ("K", "AB"),
        "Strikeouts (Pitching)": ("K", "IP"),
        "Walks (Pitching)": ("BB", "IP"),
        "Earned Runs": ("ER", "IP"),
        "Innings Pitched": ("IP", "IP"),
        "Pitching Outs": PITCHING_OUTS_KEY,
        "Total Bases": TOTAL_BASES_KEY,
    },
    "basketball": {
        "Points": ("PTS", None),
        "Rebounds": ("REB", None),
        "Assists": ("AST", None),
        "Steals": ("STL", None),
        "Blocks": ("BLK", None),
        "Turnovers": ("TO", None),
        "3-Pointers Made": ("3PT", None),
        "Points + Rebounds + Assists": PRA_KEY,
    },
    "nfl": {
        # "RTG" (passer rating), not "QBR" - confirmed live that preseason
        # boxscores' passing group carries RTG but never QBR, while regular
        # season carries both. QBR as the discriminator silently skipped the
        # whole passing group (and its otherwise-present YDS/TD/INT values)
        # for every preseason game.
        "Passing Yards": ("YDS", "RTG"),
        "Passing TDs": ("TD", "RTG"),
        "Interceptions Thrown": ("INT", "RTG"),
        "Rushing Yards": ("YDS", "CAR"),
        "Rushing TDs": ("TD", "CAR"),
        "Receiving Yards": ("YDS", "TGTS"),
        "Receptions": ("REC", "TGTS"),
        "Receiving TDs": ("TD", "TGTS"),
        "Sacks": ("SACKS", "SOLO"),
        "Tackles": ("TOT", "SOLO"),
    },
    "hockey": {
        "Goals": ("G", None),
        "Assists": ("A", None),
        "Shots on Goal": ("SOG", None),
        "Hits": ("HT", None),
        "Blocked Shots": ("BS", None),
        "Penalty Minutes": ("PIM", None),
    },
}
# WNBA uses the same generic basketball boxscore labels as NBA.
STAT_CATALOG["wnba"] = STAT_CATALOG["basketball"]


class EspnError(Exception):
    pass


def _get(url: str, **params) -> dict:
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise EspnError(f"ESPN request failed: {e}") from e


def team_logo_url(team: dict) -> Optional[str]:
    logos = team.get("logos") or []
    default = next((lg for lg in logos if "default" in lg.get("rel", [])), None)
    return (default or logos[0])["href"] if (default or logos) else None


def find_player(name: str, sport: str) -> Optional[dict]:
    """Search ESPN for a player by name within the given sport. Returns
    {"id", "name", "team_id", "team_name", "team_logo_url", "photo_url",
    "sport"} or None."""
    data = _get(SEARCH_URL, query=name)
    player_result = next((r for r in data.get("results", []) if r.get("type") == "player"), None)
    if not player_result:
        return None

    sport_slug, league_slug = SPORT_PATHS[sport]
    for content in player_result.get("contents", []):
        if content.get("defaultLeagueSlug") != league_slug:
            continue
        athlete_id = content["link"]["web"].rstrip("/").rsplit("/id/", 1)[-1].split("/")[0]
        detail = _get(f"{ATHLETE_BASE}/{sport_slug}/{league_slug}/athletes/{athlete_id}")
        athlete = detail.get("athlete", {})
        team = athlete.get("team") or {}
        if not team.get("id"):
            return None
        return {
            "id": athlete_id,
            "name": athlete.get("displayName", content.get("displayName", name)),
            "team_id": team["id"],
            "team_name": team.get("displayName", ""),
            "team_logo_url": team_logo_url(team),
            "photo_url": (content.get("image") or {}).get("default"),
            "sport": sport,
        }
    return None


def find_team(name: str, sport: str) -> Optional[dict]:
    """Search ESPN's team list for a fuzzy name match (confirmed live: the
    site.api.espn.com teams endpoint returns all 30 MLB teams with id/
    displayName). Returns {"id", "name", "logo_url"} or None."""
    if sport not in SPORT_PATHS:
        return None
    sport_slug, league_slug = SPORT_PATHS[sport]
    data = _get(f"{SITE_BASE}/{sport_slug}/{league_slug}/teams")
    leagues = ((data.get("sports") or [{}])[0]).get("leagues") or [{}]
    for entry in leagues[0].get("teams", []):
        team = entry.get("team", {})
        display_name = team.get("displayName", "")
        if display_name and scores365.names_match(display_name, name):
            return {"id": team.get("id"), "name": display_name, "logo_url": team_logo_url(team)}
    return None


_STATUS_RANK = {"in": 0, "pre": 1, "post": 2}


def find_current_event_id(
    sport: str, team_id: str, days_ahead: int = 1, days_back: int = 0, allow_finished: bool = False,
) -> Optional[str]:
    """Search the league's current scoreboard for an event involving this
    team, preferring in-progress, then soonest scheduled, then most recent
    (within whatever window is allowed).

    Explicitly queries a window around today (Eastern) instead of relying
    on ESPN's own no-date-param default - confirmed live, that default can
    still be showing YESTERDAY's date as late as mid-morning Eastern
    (reproduced independently for both MLB and WNBA scoreboards), silently
    missing today's actual game from the results entirely.

    Same days_ahead/days_back/allow_finished bounding as scores365.
    find_match_for_team - see that function's own docstring for the full
    reasoning (both were built for the same confirmed-live bugs and now
    share the same fix). Defaults (days_ahead=1, days_back=0,
    allow_finished=False) are the auto-track pipeline's own rule: never an
    already-finished event, at any date - only live, or the soonest
    not-yet-started one today or tomorrow, never further out. bot.py's
    /tracktoday command passes the opposite bounds (days_ahead=0,
    days_back=1, allow_finished=True) to deliberately find today's or
    yesterday's event, whichever is most recent, including an
    already-finished one."""
    sport_slug, league_slug = SPORT_PATHS[sport]
    today = datetime.datetime.now(tz=scores365.EASTERN).date()
    start = (today - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    end = (today + datetime.timedelta(days=days_ahead)).strftime("%Y%m%d")
    data = _get(f"{SITE_BASE}/{sport_slug}/{league_slug}/scoreboard", dates=f"{start}-{end}")

    best = None
    best_key = None
    for event in data.get("events", []):
        competitors = event.get("competitions", [{}])[0].get("competitors", [])
        if not any(c.get("team", {}).get("id") == team_id for c in competitors):
            continue
        state = event.get("status", {}).get("type", {}).get("state")
        comp_date = event.get("competitions", [{}])[0].get("date")
        try:
            event_dt = datetime.datetime.fromisoformat(comp_date.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        event_date = event_dt.astimezone(scores365.EASTERN).date()
        if state == "post":
            if not allow_finished or event_date < today - datetime.timedelta(days=days_back) or event_date > today:
                continue
        elif state == "pre":
            if event_date < today or event_date > today + datetime.timedelta(days=days_ahead):
                continue
        rank = _STATUS_RANK.get(state, 3)
        # Within the same rank tier, a team can have more than one candidate
        # in the allowed window (e.g. games on both today and tomorrow) -
        # confirmed live, two "pre" (not-yet-started) games for the same
        # team a day apart, and this used to have no tie-break at all,
        # silently keeping whichever the API happened to list first rather
        # than the soonest one. Sort key ascending within a tier: soonest
        # first for scheduled/live, most recent first (negated epoch) for
        # an already-finished game.
        tiebreak = event_dt.timestamp() if state != "post" else -event_dt.timestamp()
        key = (rank, tiebreak)
        if best is None or key < best_key:
            best, best_key = event, key
    return best["id"] if best else None


def get_event(sport: str, event_id: str) -> Optional[dict]:
    """Fetches the full summary (status + boxscore in one call)."""
    sport_slug, league_slug = SPORT_PATHS[sport]
    try:
        return _get(f"{SITE_BASE}/{sport_slug}/{league_slug}/summary", event=event_id)
    except EspnError:
        return None


def eastern_date_str(event: dict) -> Optional[str]:
    """The event's own start date, "YYYY-MM-DD" Eastern (matches
    dailylog.today_str's own format) - for record_pick's game_date, same
    "file under the match's own day, not whenever it was posted" reasoning
    as scores365.eastern_date_str_from_iso's identical helper. None if the
    event's own date can't be parsed."""
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    return scores365.eastern_date_str_from_iso(comp.get("date"))


def is_finished(event: dict) -> bool:
    """True only for an actually-completed game - ESPN buckets postponed/
    suspended/canceled games under the same state="post" as a real finish
    (confirmed live: a postponed game had state="post" but completed=False,
    with name="STATUS_POSTPONED"), so state alone isn't enough."""
    status_type = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    return status_type.get("state") == "post" and status_type.get("completed", False)


def is_postponed(event: dict) -> bool:
    """True for a postponed/suspended/canceled event - terminal (state="post")
    but never completed, so is_finished() never returns True for it either.
    Lets callers still treat it as "won't produce a result" and clean up
    (nothing left to poll for), distinct from a real final score."""
    status_type = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    return status_type.get("state") == "post" and not status_type.get("completed", False)


def match_status_text(event: dict, sport: str) -> str:
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status_type = comp.get("status", {}).get("type", {})
    state = status_type.get("state")
    if state == "post":
        # ESPN's own detail text already distinguishes "Final" from
        # "Postponed"/"Canceled"/etc, so trust it rather than assuming Final.
        return status_type.get("detail") or "Final"
    if state == "pre":
        start = comp.get("date")
        if not start:
            return "Not Started"
        try:
            start_ts = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return "Not Started"
        seconds = start_ts - time.time()
        if seconds <= 0:
            return "Starting soon"
        hours, minutes = divmod(int(seconds // 60), 60)
        return f"Starts in {hours}h{minutes:02d}m" if hours else f"Starts in {minutes}m"
    return status_type.get("detail") or status_type.get("description") or "Live"


def _find_athlete_team(event: dict, entity_id: str) -> tuple[Optional[dict], Optional[bool]]:
    """Returns (team_dict, is_home) for whichever team this athlete appears
    under in the boxscore, regardless of which stat group - or (None, None)
    if they're not in the boxscore at all yet."""
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    home_team_id = next(
        (c.get("team", {}).get("id") for c in comp.get("competitors", []) if c.get("homeAway") == "home"), None
    )
    for team_entry in (event.get("boxscore") or {}).get("players", []):
        for group in team_entry.get("statistics", []):
            if any(a.get("athlete", {}).get("id") == entity_id for a in group.get("athletes", [])):
                team = team_entry.get("team", {})
                return team, team.get("id") == home_team_id
    return None, None


# play type -> bases earned. Confirmed live against a real finished game's
# play-by-play, cross-checked against boxscore H/HR totals.
_BASE_VALUES = {"single": 1, "double": 2, "triple": 3, "home-run": 4}


def _compute_total_bases(event: dict, entity_id: str) -> int:
    """Sums bases earned from this player's hits, from the event's own
    play-by-play (single=1, double=2, triple=3, HR=4) - ESPN's boxscore
    doesn't expose a Total Bases field or a hit-type breakdown directly."""
    total = 0
    for play in event.get("plays", []):
        play_type = (play.get("type") or {}).get("type")
        if play_type not in _BASE_VALUES:
            continue
        batter = next((p for p in play.get("participants", []) if p.get("type") == "batter"), None)
        if batter and batter.get("athlete", {}).get("id") == entity_id:
            total += _BASE_VALUES[play_type]
    return total


def get_stat_value(event: dict, entity_id: str, stat_key: tuple) -> tuple[Optional[str], Optional[bool], Optional[dict]]:
    """Returns (value, is_home, team). team/is_home are set whenever the
    player appears anywhere in the boxscore; value is None if they don't
    have a row in the specific group this stat belongs to (e.g. asking for
    a pitching stat on a position player who hasn't pitched)."""
    team, is_home = _find_athlete_team(event, entity_id)
    if team is None:
        return None, None, None

    # ESPN sometimes posts lineups (with placeholder 0 stat rows) before the
    # game actually starts - show "-" rather than a misleading 0 pre-game.
    status = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    if status.get("state") == "pre":
        return None, is_home, team

    if stat_key == TOTAL_BASES_KEY:
        return _compute_total_bases(event, entity_id), is_home, team

    if stat_key == PITCHING_OUTS_KEY:
        raw, _, _ = get_stat_value(event, entity_id, ("IP", "IP"))
        if raw is None:
            return None, is_home, team
        whole, _, partial = str(raw).partition(".")
        try:
            outs = int(whole) * 3 + (int(partial) if partial else 0)
        except ValueError:
            return None, is_home, team
        return outs, is_home, team

    if stat_key in _COMBO_STAT_COMPONENTS:
        # PTS/REB/AST are always whole numbers - int, not float, so display
        # matches every other integer stat (e.g. "30" not "30.0").
        total = 0
        any_found = False
        for component_key in _COMBO_STAT_COMPONENTS[stat_key]:
            raw, _, _ = get_stat_value(event, entity_id, component_key)
            if raw is None:
                continue
            try:
                total += int(raw)
                any_found = True
            except (TypeError, ValueError):
                continue
        return (total if any_found else None), is_home, team

    label, discriminator = stat_key
    for team_entry in (event.get("boxscore") or {}).get("players", []):
        if team_entry.get("team", {}).get("id") != team.get("id"):
            continue
        for group in team_entry.get("statistics", []):
            labels = group.get("labels", [])
            if label not in labels or (discriminator is not None and discriminator not in labels):
                continue
            idx = labels.index(label)
            for a in group.get("athletes", []):
                if a.get("athlete", {}).get("id") == entity_id:
                    stats = a.get("stats", [])
                    raw = stats[idx] if idx < len(stats) else None
                    if label in _MADE_ATTEMPTED_LABELS and raw:
                        raw = raw.split("-", 1)[0]
                    return raw, is_home, team
    return None, is_home, team


def grade_over_under(value, direction: str, line: float) -> Optional[str]:
    """Grades an over/under prop against a finished event's final stat value.
    Returns "won"/"lost"/"push" (exactly on the line), or None if value isn't
    a usable number (e.g. the player never appeared in the boxscore)."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric == line:
        return "push"
    if direction == "over":
        return "won" if numeric > line else "lost"
    return "won" if numeric < line else "lost"


def get_first_inning_breakdown(event: dict) -> Optional[tuple[int, int]]:
    """Returns (home_runs, away_runs) scored in the 1st inning, once it's
    fully complete - MLB's per-team `linescores` list only grows as innings
    are actually played (confirmed live: a finished game's list length
    matched its actual inning count), but the *current* inning's entry
    already appears mid-inning with a running value (confirmed live against
    a game in the bottom of the 1st) - so "1st inning complete" is judged by
    status.period advancing past 1 (or the game ending), not by linescores
    length alone. Returns None if the 1st inning isn't decided yet, or the
    game ended without ever recording a linescore (e.g. postponed)."""
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {})
    period = status.get("period") or 0
    state = status.get("type", {}).get("state")
    if period <= 1 and state != "post":
        return None

    home_runs = away_runs = None
    for c in comp.get("competitors", []):
        linescores = c.get("linescores") or []
        if not linescores:
            return None
        try:
            runs = int(linescores[0].get("displayValue", 0))
        except (TypeError, ValueError):
            return None
        if c.get("homeAway") == "home":
            home_runs = runs
        else:
            away_runs = runs
    if home_runs is None or away_runs is None:
        return None
    return home_runs, away_runs


def grade_yrfi(total_runs: int, pick_type: str) -> str:
    """pick_type is "YRFI" (yes runs 1st inning) or "NRFI" (no runs)."""
    scored = total_runs > 0
    if pick_type == "YRFI":
        return "won" if scored else "lost"
    return "lost" if scored else "won"

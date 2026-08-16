#!/usr/bin/env python3
"""
KBO (Korea Baseball Organization) player props, backed by the league's own
official site (eng.koreabaseball.com) - moneyline/game-level KBO picks
already go through 365scores fine (see scores365.py, both leagues share its
"baseball" sport type), but player props only ever had ESPN as a source
(see espn.py/proptracker.py), and ESPN has no KBO league at all, only MLB.
Confirmed live: a KBO prop for a former-MLB player used to silently match
that player's OLD MLB athlete record on ESPN instead - see picks.py's
_PROP_SPORT_OVERRIDE for the full story of why "kbo" is tagged as its own
sport now instead of collapsing into "baseball" like a real MLB prop.

No TLS-fingerprint blocking encountered (unlike hawk.live/GosuGamers/
BoxingScene) - plain `requests` works fine, confirmed live. mykbostats.com
and BoxRec were both evaluated and rejected as alternatives - both sit
behind a Cloudflare managed JS challenge that this bot's scraping can't
pass at all (confirmed live: a plain request to either gets a "Just a
moment..." challenge page on every data page, not real content).

Player search is scoped to the Batting/Pitching Leaders pages (roughly the
top ~20 in each major counting stat) rather than the site's own full-roster
PlayerSearch.aspx, which needs a scripted ASP.NET postback (view state/
event validation tokens) rather than a plain GET - out of scope for now.
Covers the common case (a prop is typically posted on a notable/rostered
player) but won't find a deep-bench/rarely-used player who's never ranked -
that fails as a clean "player not found," not a mismatch.

Unlike ESPN's live in-game boxscore, there's no live/in-progress stat feed
here at all - a player's game log only gets a new row once that game is
actually final, same "notstarted" -> "finished" shape as boxing.py (no
live polling in-between), not proptracker.py's richer live-tracking shape.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

import scores365

_BATTING_LEADERS_URL = "https://eng.koreabaseball.com/stats/BattingLeaders.aspx"
_PITCHING_LEADERS_URL = "https://eng.koreabaseball.com/stats/PitchingLeaders.aspx"
_HITTER_GAMELOGS_URL = "https://eng.koreabaseball.com/Teams/PlayerInfoHitter/GameLogs.aspx?pcode={pcode}"
_PITCHER_GAMELOGS_URL = "https://eng.koreabaseball.com/Teams/PlayerInfoPitcher/GameLogs.aspx?pcode={pcode}"
# Confirmed live on a player's own Summary.aspx page (an <img> tag literally
# builds this same URL from pcode + the current season year, falling back
# to a "noimg.png" placeholder client-side on a 404) - deriving it directly
# here avoids a second page fetch just to find a photo. A player with no
# photo on file 404s, which _fetch_circular_logo (scoreimage.py) already
# handles gracefully (falls back to no photo), so this is never validated
# ahead of time.
_PHOTO_URL = "https://6ptotvmi5753.edge.naverncp.com/KBO_IMAGE/person/middle/{year}/{pcode}.jpg"
_REQUEST_TIMEOUT = 10

# Leaders lists barely move within a day - no need to refetch every poll.
_LEADERS_CACHE_SECONDS = 3600
# A game log only ever gains a new row once a day at most (KBO plays one
# game per team per day) - a shorter cache would just be wasted requests.
_GAMELOG_CACHE_SECONDS = 15 * 60

# KBO games are scheduled and played on the Korean calendar day, not
# whatever day it happens to be wherever this bot's picks get posted from -
# same "grade against the league's own day" reasoning as scores365's
# Eastern-date handling for MLB elsewhere in this bot.
_KST = ZoneInfo("Asia/Seoul")

_leaders_cache: dict = {}  # url -> (players, fetched_at)
_gamelog_cache: dict = {}  # (pcode, is_pitcher) -> (rows, fetched_at)


class KboError(Exception):
    pass


def _get(url: str) -> str:
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        raise KboError(str(e)) from e
    return resp.text


_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
# Case-insensitive - confirmed live the site renders this URL inconsistently
# between pages (mixed-case "/Teams/PlayerInfoHitter/..." on the batting
# leaders page, all-lowercase "/teams/playerinfopitcher/..." on the
# pitching one).
_PLAYER_CELL_RE = re.compile(
    r'playerinfo(?:hitter|pitcher)/summary\.aspx\?pcode=(\d+)"[^>]*>([^<]+)</a>', re.IGNORECASE
)
_TEAM_CELL_RE = re.compile(r'<td title="TEAM">([^<]*)</td>')


def _players_for(url: str, is_pitcher: bool) -> list[dict]:
    cached = _leaders_cache.get(url)
    if cached and time.time() - cached[1] < _LEADERS_CACHE_SECONDS:
        return cached[0]
    html = _get(url)
    players = []
    for row in _ROW_RE.findall(html):
        pm = _PLAYER_CELL_RE.search(row)
        if not pm:
            continue
        tm = _TEAM_CELL_RE.search(row)
        players.append({
            "pcode": pm.group(1), "name": pm.group(2).strip(),
            "team": tm.group(1).strip() if tm else None, "is_pitcher": is_pitcher,
            "photo_url": photo_url(pm.group(1)),
        })
    _leaders_cache[url] = (players, time.time())
    return players


def photo_url(pcode: str, now: Optional[float] = None) -> str:
    year = datetime.fromtimestamp(now if now is not None else time.time(), tz=_KST).year
    return _PHOTO_URL.format(year=year, pcode=pcode)


def find_player(name: str) -> Optional[dict]:
    """Searches both Batting and Pitching leaders pages for a name match -
    KBO's site lists import players "Surname Firstname" (e.g. "DEAN
    Austin"), reversed from how a picks message would naturally type it
    ("Austin Dean") - scores365.names_match's word-set comparison handles
    that automatically, same as it already does for reordered names
    elsewhere in this bot (no special-casing needed here). Returns
    {"pcode", "name", "team", "is_pitcher", "photo_url"} or None."""
    for url, is_pitcher in ((_BATTING_LEADERS_URL, False), (_PITCHING_LEADERS_URL, True)):
        try:
            players = _players_for(url, is_pitcher)
        except KboError:
            continue
        for player in players:
            if scores365.names_match(player["name"], name):
                return player
    return None


_IP_RE = re.compile(r"^(\d+)(?:\s+(\d)/3)?$")


def _parse_ip_outs(raw: str) -> Optional[int]:
    """KBO's IP notation is "N" or "N X/3" (e.g. "4 2/3") - whole innings
    plus a thirds-of-an-inning fraction, the same underlying concept as
    ESPN's "N.X" baseball notation but spelled differently (see espn.py's
    own PITCHING_OUTS_KEY comment for that equivalent MLB-side gotcha)."""
    m = _IP_RE.match((raw or "").strip())
    if not m:
        return None
    outs = int(m.group(1)) * 3
    if m.group(2):
        outs += int(m.group(2))
    return outs


# Canonical stat labels shared with espn.STAT_CATALOG["baseball"] (see
# picks.py's _PROP_SPORT_OVERRIDE) - a pick parses the same wording
# regardless of whether it's actually MLB or KBO, so grading has to
# recognize the exact same label strings.
_HITTER_COLUMNS = {
    "Hits": "H", "Runs": "R", "RBIs": "RBI", "Home Runs": "HR",
    "Walks": "BB", "Strikeouts (Batting)": "SO",
}
_PITCHER_COLUMNS = {
    "Strikeouts (Pitching)": "K", "Walks (Pitching)": "BB", "Earned Runs": "ER",
}
_COMPUTED_HITTER_STATS = {"Total Bases"}
_COMPUTED_PITCHER_STATS = {"Innings Pitched", "Pitching Outs"}

SUPPORTED_STATS = {
    False: set(_HITTER_COLUMNS) | _COMPUTED_HITTER_STATS,
    True: set(_PITCHER_COLUMNS) | _COMPUTED_PITCHER_STATS,
}


def stat_supported(stat_label: str, is_pitcher: bool) -> bool:
    return stat_label in SUPPORTED_STATS[is_pitcher]


def stat_value(row: dict, stat_label: str, is_pitcher: bool) -> Optional[float]:
    """The named stat's value for one already-found game-log row (see
    find_game_row) - None if the row doesn't carry a usable value for it
    (shouldn't normally happen once stat_supported has already confirmed
    the label is valid for this player's role, but a column can still be
    missing/blank for an unusual game line)."""
    if stat_label == "Total Bases":
        try:
            h, doubles, triples, hr = int(row["H"]), int(row["2B"]), int(row["3B"]), int(row["HR"])
        except (KeyError, ValueError):
            return None
        singles = h - doubles - triples - hr
        return float(singles + doubles * 2 + triples * 3 + hr * 4)
    if stat_label in ("Innings Pitched", "Pitching Outs"):
        outs = _parse_ip_outs(row.get("IP", ""))
        if outs is None:
            return None
        return float(outs) if stat_label == "Pitching Outs" else outs / 3
    column = (_PITCHER_COLUMNS if is_pitcher else _HITTER_COLUMNS).get(stat_label)
    if not column or column not in row:
        return None
    try:
        return float(row[column])
    except ValueError:
        return None


def _game_rows(pcode: str, is_pitcher: bool) -> list[dict]:
    cache_key = (pcode, is_pitcher)
    cached = _gamelog_cache.get(cache_key)
    if cached and time.time() - cached[1] < _GAMELOG_CACHE_SECONDS:
        return cached[0]
    url = (_PITCHER_GAMELOGS_URL if is_pitcher else _HITTER_GAMELOGS_URL).format(pcode=pcode)
    html = _get(url)
    rows = []
    for row in _ROW_RE.findall(html):
        cells = dict(re.findall(r'<td title="([^"]*)">([^<]*)</td>', row))
        if cells.get("DATE"):
            rows.append(cells)
    _gamelog_cache[cache_key] = (rows, time.time())
    return rows


def find_game_row(pcode: str, is_pitcher: bool, target_date_mmdd: str) -> Optional[dict]:
    """The player's own game-log row for one specific date ("MM.DD", no
    year - matches the site's own date column format), or None if that
    game hasn't been played/posted yet. Raises KboError only for a genuine
    fetch failure - a clean "not found yet" is a normal, expected wait
    state, not an error."""
    rows = _game_rows(pcode, is_pitcher)
    return next((r for r in rows if r.get("DATE") == target_date_mmdd), None)


def today_kst_mmdd(now: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=_KST)
    return f"{dt.month:02d}.{dt.day:02d}"


def next_kst_midnight_epoch(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=_KST)
    next_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_midnight.timestamp()

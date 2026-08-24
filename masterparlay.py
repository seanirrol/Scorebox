#!/usr/bin/env python3
"""
Auto-grades GreenFox's "MASTER PARLAYS" slips (posted as plain text, no
bracket sport tags at all - unlike every other picks message this bot
parses) against picks that are already individually tracked elsewhere as
premium picks. Pull-based (a slash command reads the channel and builds a
fresh report on demand), not push-based on_message auto-tracking - these
legs should already exist as their own tracked picks, so there's nothing
new to create here, only to look up and grade.

Each leg is re-resolved the same way its own original auto-tracker would
(scores365/espn team+event lookups), then checked against dailylog - not
each tracker's own state file - since dailylog already has exactly what's
needed (current status + human-readable detail) keyed the same way
(channel_id, module, track_key). A leg that can't be resolved to a
currently-tracked pick (unsupported wording, or genuinely not tracked) is
reported clearly as "not tracked" rather than silently dropped or guessed.

Two slip formats are recognized (GreenFox's own wording has drifted over
time, confirmed live - a whole day's worth of slips silently fell back to
an older parseable message once this second format showed up unannounced):

  🎟️ MASTER PARLAYS
  🎟️ The Daily Double (+115)
  • Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)
  • Leg 2: Atlanta Dream ML (-350 | 87% Conf)

  🎯 RECOMMENDED 1 GAME + 1 PROP DOUBLE LOCK
  Leg 1 (Game): [WNBA] Golden State Valkyries at Minnesota Lynx - Minnesota Lynx ML (FanDuel -100)
  Leg 2 (Prop): [WNBA Props] Rhyne Howard Over 14.5 Points (Alt Line) [Atlanta Dream at Los Angeles Sparks] (PrizePicks -205)

The second format has no named parlay or combo odds at all (the header
text itself becomes the "name", odds is left blank) and each leg carries
a matchup prefix and/or bracketed context and a bookmaker+odds suffix
that must be stripped down to the same bare "Team Pick"/"Player Over N
Stat" shape the resolvers below expect - see _clean_recommended_leg.

"Parlay day" windows: unlike every other tracker's Eastern-midnight
calendar day, a parlay day runs from 3:00 AM Eastern to the next 3:00 AM
Eastern (see _PARLAY_DAY_CUTOFF_HOUR) - a later cutoff to comfortably
outlast extra innings/delays before that day's slip is considered
"done" and eligible for the nightly auto-archive (see
auto_archive_if_needed). find_latest_slip only ever looks at the
*current* parlay-day window - once the cutoff passes with nothing new
posted, the previous day's results live in the archive channel instead
(PUBLISH_CHANNEL_ID), not as a silent fallback here.
"""

import asyncio
import datetime
import re
import time
from typing import Optional

import discord

import dailylog
import espn
import f5tracker
import picks
import proptracker
import scoreimage
import scores365
import settracker
import state
import tracker

# Every premium pick this feature grades against is tracked into this one
# fixed destination channel (see config.PICKS_CHANNEL_MAP) - not the
# channel GreenFox posts the parlay slip itself into.
PREMIUM_SCORES_CHANNEL_ID = 1536429372217761833

# Where GreenFox posts the "MASTER PARLAYS" slip itself (/premiumparlay is
# only usable here) - kept separate from PUBLISH_CHANNEL_ID below so this
# channel stays just the raw slip feed, not cluttered with every day's
# published report on top of it.
PARLAY_SLIP_CHANNEL_ID = 1538412823250608300

# Permanent storage for published parlay reports, tagged by the slip's own
# posted date (see slip_date_str/_archive_footer_text) - /premiumparlay's
# "View Past Dates" browsing (see find_archived_report) reads back from
# here, so a past day's results stay available even though re-resolving
# an old slip from scratch runs into 365scores' own bulk-feed data window
# closing within a day or two.
PUBLISH_CHANNEL_ID = 1540479187758874664

# See module docstring's "Parlay day windows" section. GreenFox posts a
# given day's slate the *night before* (confirmed live: as early as
# 10:00 PM through 3:00 AM Eastern), so "day D's parlays" are the ones
# posted in the window (D-1's 3am, D's 3am] - a post is tagged by
# looking FORWARD to the next 3am cutoff, not back to the last one (see
# _parlay_day_date_str). /premiumparlay's own live view then shows
# whichever window most recently *closed* (see find_latest_slip) - not
# whatever's accumulated since the last cutoff, since GreenFox hasn't
# even started posting the next window's slate until 10pm regardless.
_PARLAY_DAY_CUTOFF_HOUR = 3


def _floor_cutoff(epoch: float) -> float:
    """Epoch of the most recent 3:00 AM Eastern at or before epoch."""
    local = datetime.datetime.fromtimestamp(epoch, tz=scores365.EASTERN)
    cutoff = local.replace(hour=_PARLAY_DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if local < cutoff:
        cutoff -= datetime.timedelta(days=1)
    return cutoff.timestamp()


def _ceil_cutoff(epoch: float) -> float:
    """Epoch of the next 3:00 AM Eastern at or after epoch - the cutoff
    that a post at epoch is tagged under (see _parlay_day_date_str)."""
    local = datetime.datetime.fromtimestamp(epoch, tz=scores365.EASTERN)
    cutoff = local.replace(hour=_PARLAY_DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if local > cutoff:
        cutoff += datetime.timedelta(days=1)
    return cutoff.timestamp()


def _next_cutoff_strictly_after(epoch: float) -> float:
    """Epoch of the next 3:00 AM Eastern strictly after epoch (unlike
    _ceil_cutoff, epoch landing exactly on a cutoff still advances a full
    day) - what the auto-archive loop sleeps until next."""
    local = datetime.datetime.fromtimestamp(epoch, tz=scores365.EASTERN)
    cutoff = local.replace(hour=_PARLAY_DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if local >= cutoff:
        cutoff += datetime.timedelta(days=1)
    return cutoff.timestamp()


def _cutoff_plus_days(cutoff_epoch: float, days: int) -> float:
    """Shifts a cutoff instant by whole days, DST-safe - timedelta(days=n)
    on an aware datetime preserves the wall-clock hour across a DST
    change, same pattern scores365.next_eastern_midnight_epoch already
    relies on, so this stays exactly 3:00 AM even on a DST-transition day
    rather than drifting by an hour."""
    dt = datetime.datetime.fromtimestamp(cutoff_epoch, tz=scores365.EASTERN)
    return (dt + datetime.timedelta(days=days)).timestamp()


def _parlay_day_date_str(post_epoch: float) -> str:
    """Which parlay day a message posted at post_epoch belongs to - the
    calendar date of the *next* 3:00 AM Eastern cutoff at or after it
    (GreenFox posts a day's slate the night before, so an 11pm post is
    for the day whose cutoff is still hours ahead, not the day that
    already started hours earlier)."""
    return scores365.eastern_date_str(_ceil_cutoff(post_epoch)) or dailylog.today_str()


def _live_window(now_epoch: float) -> tuple[float, float]:
    """(start, end] of the parlay day whose posting window most recently
    closed as of now_epoch - the one /premiumparlay's live view shows.
    Deliberately NOT "since the last cutoff up to now" - GreenFox doesn't
    start posting the next window's slate until 10pm regardless of how
    long ago the last cutoff was, so that would show nothing all day."""
    end = _floor_cutoff(now_epoch)
    start = _cutoff_plus_days(end, -1)
    return start, end


def previous_parlay_day_str(now_epoch: float) -> str:
    """The parlay day one full day older than the live one - ready to
    archive since its games have now had a full extra day (past its own
    posting cutoff) to finish. Used by the nightly auto-archive job:
    when it fires right at a cutoff (e.g. 8/25 3am), this is "8/24" - the
    day whose slate closed at 8/24's own 3am cutoff."""
    _live_start, live_end = _live_window(now_epoch)
    archive_cutoff = _cutoff_plus_days(live_end, -1)
    return scores365.eastern_date_str(archive_cutoff) or dailylog.today_str()


def seconds_until_next_parlay_day_cutoff(now_epoch: float) -> float:
    return _next_cutoff_strictly_after(now_epoch) - now_epoch


# dailylog's own won/lost/push/void marks, plus two states dailylog itself
# never has: "pending" (still live/not started, same yellow as an
# in-progress score card) and "unresolved" (this leg's wording didn't
# resolve to any currently-tracked pick at all - deliberately distinct
# from every real graded status so it can't be mistaken for one).
_LEG_MARKS = {
    "won": dailylog.WINMARK, "lost": dailylog.LOSSMARK, "push": dailylog.PUSHMARK, "void": dailylog.VOIDMARK,
    "pending": "🟨", "unresolved": "❓",
}

_PARLAY_HEADER_RE = re.compile(r"^🎟️\s*(.+?)\s*\(([+-]\d+)\)\s*$")
_PARLAY_LEG_RE = re.compile(r"^[•\-]\s*Leg\s*\d+\s*:\s*(.+?)\s*\([+-]?\d+\s*\|\s*\d+%\s*Conf\.?\)\s*$", re.IGNORECASE)

# Newer "RECOMMENDED ..." format - no named parlay/combo odds at all, just
# an emoji (varies: 🎯/🔒/⚡/... - \W* accepts whatever GreenFox uses next
# without needing another update) followed by the header text itself,
# which becomes the parlay's "name" with odds left blank.
_RECOMMENDED_HEADER_RE = re.compile(r"^\W*\s*(RECOMMENDED\b.+)$", re.IGNORECASE)
# "Leg 1 (Game): [WNBA] Golden State Valkyries at Minnesota Lynx - Minnesota
# Lynx ML (FanDuel -100)" - the "(Game)"/"(Prop 2)" leg-type tag and
# "[WNBA]"/"[WNBA Props]" sport tag are both dropped, only the description
# after them is kept (see _clean_recommended_leg for the matchup-prefix/
# bracket/bookmaker-odds cleanup still needed on top of that).
_RECOMMENDED_LEG_RE = re.compile(r"^Leg\s*\d+\s*\([^)]*\)\s*:\s*\[[^\]]+\]\s*(.+)$", re.IGNORECASE)

# Strips a trailing "(FanDuel -100)"/"(PrizePicks -205)" bookmaker+odds
# parenthetical - distinct from the old format's "(-150 | 88% Conf)" suffix
# (_PARLAY_LEG_RE strips that one itself), this one starts with letters
# not digits.
_TRAILING_BOOK_ODDS_RE = re.compile(r"\s*\([A-Za-z][A-Za-z0-9 ]*\s+[+-]?\d[\d.]*\)\s*$")
# A player prop leg can carry its own bracketed matchup context after the
# stat line, e.g. "... Points (Alt Line) [Atlanta Dream at Los Angeles
# Sparks]" - purely informational, the resolver only needs the player/
# stat/line, not the matchup.
_TRAILING_BRACKET_MATCHUP_RE = re.compile(r"\s*\[[^\]]+\]\s*$")
# A game leg spells out the full matchup before its own pick, e.g. "Golden
# State Valkyries at Minnesota Lynx - Minnesota Lynx ML" - only the part
# after the dash is the actual pick text every resolver below expects.
_LEADING_MATCHUP_RE = re.compile(r"^.+?\s+(?:at|vs\.?|v\.?)\s+.+?\s-\s+(.+)$", re.IGNORECASE)


def _clean_recommended_leg(text: str) -> str:
    text = _TRAILING_BOOK_ODDS_RE.sub("", text).strip()
    text = _TRAILING_BRACKET_MATCHUP_RE.sub("", text).strip()
    m = _LEADING_MATCHUP_RE.match(text)
    if m:
        text = m.group(1).strip()
    return text

# Leg wording shapes, tried in this order - F5 ML before plain ML (a bare
# "(.+?)\s+(?:ML|Moneyline)$" would otherwise swallow "Houston Astros F5"
# whole, "F5" included, since there's only one ML/Moneyline trailer to
# anchor on at the end). Both accept "ML" and the full "Moneyline" word -
# same two spellings picks.py's own _parse_team_pick recognizes -
# confirmed live, a real slip's "New York Yankees Moneyline"/"Taylor
# Fritz Moneyline"/etc. legs all failed to resolve, reporting "Not
# currently tracked" for picks that were genuinely already tracked,
# because only the abbreviated "ML" was recognized here.
_F5_ML_RE = re.compile(r"^(.+?)\s+F5\s+(?:ML|Moneyline)$", re.IGNORECASE)
_SPREAD_RE = re.compile(r"^(.+?)\s+([+-]\d+(?:\.\d+)?)\s*(?:\(Alt Spread\))?$", re.IGNORECASE)
_ML_RE = re.compile(r"^(.+?)\s+(?:ML|Moneyline)$", re.IGNORECASE)
# "Philadelphia Phillies vs Seattle Mariners - Under 9.5 Runs" - a
# combined GAME total (both teams' score together), not a single team's
# own total (that's _SPREAD_RE/tracker's team_total path) or a player
# stat (_PLAYER_PROP_RE, which needs a stat word after the number - this
# is checked first since a bare "Runs"/"Points" trailing word would
# otherwise also satisfy that regex's own catch-all stat group).
_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?\b|\bv\.?\b|\bat\b)\s*(.+?)\s*-\s*(?:total\s*)?(Over|Under)\s+([\d.]+)(?:\s+\S.*)?\s*$",
    re.IGNORECASE,
)
_PLAYER_PROP_RE = re.compile(
    r"^(?:(.+?)\s*-\s*)?(.+?)\s+(Over|Under)\s+([\d.]+)\s+(.+?)(?:\s*\(Alt Line\))?$", re.IGNORECASE,
)

# Tennis-only markets, settracker.py-backed - "Player +5.5 Games" (a games
# handicap) and "Player to Win a Set"/"to Win at Least 1 Set"/"to Win 1+
# Set" (same wording variants picks.py's own _WIN_A_SET_WORDING accepts).
_GAMES_HANDICAP_RE = re.compile(r"^(.+?)\s+([+-]\d+(?:\.\d+)?)\s*Games\b", re.IGNORECASE)
_WIN_A_SET_RE = re.compile(r"^(.+?)\s+to\s+win\s+(?:a\s+set|at\s+least\s+1\s+sets?|1\+\s*sets?)\b", re.IGNORECASE)

# Every ESPN-backed sport this bot's player props support (see
# espn.SPORT_PATHS) - tried in turn since a bare leg line has no sport tag
# at all, stopping at the first sport where BOTH the player and the stat
# resolve (same "try WNBA if NBA fails" idea bot.py's own _auto_playerprops
# already uses, just generalized to every supported sport).
_PROP_CANDIDATE_SPORTS = ("baseball", "basketball", "wnba", "nfl", "hockey")

# GreenFox's own template occasionally leaves a team-total leg's "Player"
# placeholder unfilled (confirmed live: "[MLB] Player - Under 9.5 Runs
# (Alt Line) [Philadelphia Phillies @ Seattle Mariners]" on 2026-08-24 -
# a broken template output, not a market this bot could ever resolve as
# written since it never actually names either team, only a stray
# "Player" left over from a player-prop template). No algorithm can
# recover the intended teams from that - corrected on sight instead, a
# straight substitution applied to the cleaned leg text before it's ever
# handed to a resolver, same as if GreenFox had typed it correctly.
_LEG_TEXT_CORRECTIONS = {
    "Player - Under 9.5 Runs (Alt Line)": "Philadelphia Phillies vs Seattle Mariners - Under 9.5 Runs",
}


def parse_master_parlays(text: str) -> list[dict]:
    """[{"name": str, "odds": str, "legs": [str, ...]}, ...] - the leading
    "🎟️ MASTER PARLAYS" banner line (no "(+odds)" suffix) never matches
    _PARLAY_HEADER_RE, so it's naturally skipped rather than treated as an
    empty parlay of its own. Recognizes both slip formats (see module
    docstring) - odds is "" for a "RECOMMENDED ..." parlay, which never
    states one."""
    parlays: list[dict] = []
    current: Optional[dict] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _PARLAY_HEADER_RE.match(line)
        if header:
            current = {"name": header.group(1).strip(), "odds": header.group(2), "legs": []}
            parlays.append(current)
            continue
        recommended_header = _RECOMMENDED_HEADER_RE.match(line)
        if recommended_header:
            current = {"name": recommended_header.group(1).strip(), "odds": "", "legs": []}
            parlays.append(current)
            continue
        leg = _PARLAY_LEG_RE.match(line)
        if leg and current is not None:
            leg_text = leg.group(1).strip()
            current["legs"].append(_LEG_TEXT_CORRECTIONS.get(leg_text, leg_text))
            continue
        recommended_leg = _RECOMMENDED_LEG_RE.match(line)
        if recommended_leg and current is not None:
            leg_text = _clean_recommended_leg(recommended_leg.group(1).strip())
            current["legs"].append(_LEG_TEXT_CORRECTIONS.get(leg_text, leg_text))
    return parlays


def _dailylog_lookup(module: str, track_key_str: str) -> Optional[dict]:
    data = state.load_daily_log()
    return data.get(dailylog._key(PREMIUM_SCORES_CHANNEL_ID, module, track_key_str))


async def _resolve_team(team: str, sport: Optional[str]) -> Optional[tuple[dict, int]]:
    # allow_finished=True (with a day of lookback) - unlike a fresh
    # auto-track attempt, this is re-resolving a leg that was already
    # tracked hours ago and may well have finished by now (a graded
    # "won"/"lost" dailylog entry sitting right there for it - confirmed
    # live: the default fresh-pick bounds silently failed to find an
    # already-finished game's leg at all, reporting "not currently
    # tracked" for a pick that plainly was). Same days_ahead=0/days_back=1
    # bounds bot.py's own /tracktoday already uses for this exact reason.
    try:
        return await asyncio.to_thread(scores365.find_match_for_team, team, sport, 0, 1, True)
    except scores365.ScoresError:
        return None


async def _resolve_ml_or_spread(text: str) -> Optional[dict]:
    m = _SPREAD_RE.match(text)
    if m:
        team, line = m.group(1).strip(), float(m.group(2))
        result = await _resolve_team(team, None)
        if not result:
            return None
        game, _sport_id = result
        key = tracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], team_total=team, total_direction="spread", total_line=line)
        return _dailylog_lookup("tracker", key)

    m = _ML_RE.match(text)
    if m:
        team = m.group(1).strip()
        result = await _resolve_team(team, None)
        if not result:
            return None
        game, _sport_id = result
        key = tracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], picked_team=team)
        return _dailylog_lookup("tracker", key)
    return None


async def _resolve_total(text: str) -> Optional[dict]:
    m = _TOTAL_RE.match(text)
    if not m:
        return None
    team_a, direction, line = m.group(1).strip(), m.group(3).lower(), float(m.group(4))
    result = await _resolve_team(team_a, None)
    if not result:
        return None
    game, _sport_id = result
    key = tracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], total_direction=direction, total_line=line)
    return _dailylog_lookup("tracker", key)


async def _resolve_f5_ml(text: str) -> Optional[dict]:
    m = _F5_ML_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    result = await _resolve_team(team, "baseball")
    if not result:
        return None
    game, _sport_id = result
    key = f5tracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], picked_team=team)
    return _dailylog_lookup("f5tracker", key)


async def _resolve_games_handicap(text: str) -> Optional[dict]:
    m = _GAMES_HANDICAP_RE.match(text)
    if not m:
        return None
    player, line = m.group(1).strip(), float(m.group(2))
    result = await _resolve_team(player, "tennis")
    if not result:
        return None
    game, _sport_id = result
    key = settracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], "games_handicap", player)
    return _dailylog_lookup("settracker", key)


async def _resolve_win_a_set(text: str) -> Optional[dict]:
    m = _WIN_A_SET_RE.match(text)
    if not m:
        return None
    player = m.group(1).strip()
    result = await _resolve_team(player, "tennis")
    if not result:
        return None
    game, _sport_id = result
    key = settracker.track_key(PREMIUM_SCORES_CHANNEL_ID, game["id"], "win_a_set", player)
    return _dailylog_lookup("settracker", key)


async def _resolve_player_prop(text: str) -> Optional[dict]:
    m = _PLAYER_PROP_RE.match(text)
    if not m:
        return None
    player = (m.group(2) or "").strip()
    direction = m.group(3).lower()
    line = float(m.group(4))
    raw_stat = m.group(5).strip()
    if not player:
        return None

    for sport in _PROP_CANDIDATE_SPORTS:
        stat_label = picks._match_stat_label(sport, raw_stat)
        if not stat_label:
            continue
        stat_key = espn.STAT_CATALOG.get(sport, {}).get(stat_label)
        if not stat_key:
            continue
        try:
            entity = await asyncio.to_thread(espn.find_player, player, sport)
        except espn.EspnError:
            continue
        if not entity:
            continue
        # Same allow_finished=True reasoning as _resolve_team above.
        event_id = await asyncio.to_thread(espn.find_current_event_id, sport, entity["team_id"], 0, 1, True)
        if not event_id:
            continue
        key = proptracker.prop_key(PREMIUM_SCORES_CHANNEL_ID, event_id, entity["id"], stat_key, direction, line)
        entry = _dailylog_lookup("proptracker", key)
        if entry:
            return entry
    return None


async def resolve_leg(leg_text: str) -> dict:
    """Best-effort classification + resolution against dailylog - returns
    {"raw", "status", "label", "detail"}. status is "unresolved" (not a
    real dailylog status) when the leg's wording isn't one of the shapes
    this module knows, or it genuinely isn't currently tracked - kept
    distinct from won/lost/push/void/pending so a report can call it out
    clearly instead of guessing. label is always run through
    picks.clean_label - dailylog's own stored label can still carry
    "(Bookmaker odds)"/"(Alt Line)" annotations for anything tracked
    before that cleanup existed, and an unresolved leg's own text can
    still carry "(Alt Line)" too (only the "(odds | NN% Conf)" suffix is
    stripped by _PARLAY_LEG_RE at parse time, not every trailing paren)."""
    for resolver in (_resolve_f5_ml, _resolve_games_handicap, _resolve_win_a_set, _resolve_ml_or_spread, _resolve_total, _resolve_player_prop):
        entry = await resolver(leg_text)
        if entry:
            return {"raw": leg_text, "status": entry["status"], "label": picks.clean_label(entry["label"]), "detail": entry["detail"]}
    return {"raw": leg_text, "status": "unresolved", "label": picks.clean_label(leg_text), "detail": "Not currently tracked"}


def grade_parlay(resolved_legs: list[dict]) -> str:
    """"won"/"lost"/"pending" for the parlay as a whole (never "unresolved"
    itself, even if a leg is) - lost the moment any leg is actually graded
    lost, since a parlay can't recover from that no matter what else
    resolves. Won only once every leg has resolved won/push. Pending
    otherwise - including while a leg simply isn't resolvable yet, since
    that's a genuine unknown, not a loss; guessing "lost" for a leg the
    bot just couldn't find would be actively wrong, not conservative.
    Mirrors parlaytracker._summary_color_status's own won/lost logic."""
    if any(leg["status"] == "lost" for leg in resolved_legs):
        return "lost"
    if all(leg["status"] in ("won", "push") for leg in resolved_legs):
        return "won"
    return "pending"


_TITLE_SUFFIX = {"won": f"{dailylog.WINMARK} HIT", "lost": f"{dailylog.LOSSMARK} Busted", "pending": "⏳ Pending"}


# Every archived embed's footer starts with this exact text, followed by
# the slip's own "YYYY-MM-DD" Eastern date - a simple, greppable tag that
# survives round-tripping through Discord (footers aren't otherwise used
# for anything else here) so find_archived_dates/find_archived_report can
# read the date back out of a channel's message history later, without
# needing any separate persisted index.
_ARCHIVE_FOOTER_PREFIX = "Scorebox • "


def _archive_footer_text(date_str: str) -> str:
    return f"{_ARCHIVE_FOOTER_PREFIX}{date_str}"


def archived_date_from_embed(embed: discord.Embed) -> Optional[str]:
    footer = embed.footer.text if embed.footer else None
    if not footer or not footer.startswith(_ARCHIVE_FOOTER_PREFIX):
        return None
    return footer[len(_ARCHIVE_FOOTER_PREFIX) :]


def slip_date_str(messages: list[discord.Message]) -> str:
    """The parlay day this slip "belongs to" (see module docstring) - the
    earliest message's own posted time, not whenever Publish happens to
    get clicked, and using the 3:00 AM Eastern cutoff rather than
    midnight so a slip posted at 11pm (or even 1am the "next" day) whose
    last leg doesn't finish for hours still archives under the day it was
    posted, not the day someone finally published it."""
    earliest = min(messages, key=lambda m: m.created_at)
    return _parlay_day_date_str(earliest.created_at.timestamp())


def build_parlay_embed(name: str, odds: str, resolved_legs: list[dict], date_str: Optional[str] = None) -> discord.Embed:
    overall = grade_parlay(resolved_legs)
    color_key = {"won": "won", "lost": "lost"}.get(overall, "inprogress")
    lines = [f"{_LEG_MARKS.get(leg['status'], '❓')} {leg['label']} — {leg['detail']}" for leg in resolved_legs]
    # A "RECOMMENDED ..." parlay never states its own combo odds (see
    # module docstring) - odds is "" for those, no bare "()" left behind.
    title_prefix = f"{name} ({odds})" if odds else name
    embed = discord.Embed(
        title=f"{title_prefix} — {_TITLE_SUFFIX[overall]}",
        description="\n".join(lines),
        color=scoreimage.EMBED_COLOR[color_key],
    )
    if date_str:
        embed.set_footer(text=_archive_footer_text(date_str))
    embed.timestamp = discord.utils.utcnow()
    return embed


def combine_slip_text(messages: list[discord.Message]) -> str:
    """Joins a find_latest_slip run's content back into one blob, oldest
    first (as returned), so a slip split across multiple messages parses
    as if it were always one."""
    return "\n\n".join(m.content for m in messages)


async def resolve_parlays(source_text: str) -> list[dict]:
    """Parses source_text and resolves every leg of every parlay found -
    one dict per parlay: {"name", "odds", "legs" (resolved legs), "status"
    (grade_parlay's overall won/lost/pending)}, same order as the source
    message. Shared by build_report (turns these into embeds) and
    /premiumparlay's own preview, which needs each parlay's outcome up
    front to label its publish checklist before anything is published."""
    parlays = parse_master_parlays(source_text)
    resolved = []
    for parlay in parlays:
        resolved_legs = [await resolve_leg(leg) for leg in parlay["legs"]]
        resolved.append({
            "name": parlay["name"],
            "odds": parlay["odds"],
            "legs": resolved_legs,
            "status": grade_parlay(resolved_legs),
        })
    return resolved


async def build_report(source_text: str, date_str: Optional[str] = None, only_names: Optional[set[str]] = None) -> list[discord.Embed]:
    """Resolves every parlay in source_text into an embed, same order as
    the source message. Empty list if the message has no recognizable
    parlays at all (caller decides how to report that). date_str, when
    given, gets tagged into each embed's footer for later archive lookup
    by date (see slip_date_str/find_archived_report) - omitted for a
    plain preview that's never meant to be archived. only_names, when
    given, limits the report to parlays whose name is in the set - lets
    /premiumparlay publish only the parlays a user actually picked from
    a multi-parlay slip instead of all-or-nothing."""
    parlays = await resolve_parlays(source_text)
    if only_names is not None:
        parlays = [p for p in parlays if p["name"] in only_names]
    return [build_parlay_embed(p["name"], p["odds"], p["legs"], date_str) for p in parlays]


async def find_archived_dates(channel: discord.abc.Messageable, limit: int = 200) -> list[str]:
    """Most-recent-first distinct dates with at least one archived parlay
    report in this channel - used for /premiumparlay's own date dropdown,
    same idea as dailylog.available_dates for /summary."""
    dates: list[str] = []
    async for message in channel.history(limit=limit):
        for embed in message.embeds:
            date_str = archived_date_from_embed(embed)
            if date_str and date_str not in dates:
                dates.append(date_str)
    return dates


async def find_archived_report(channel: discord.abc.Messageable, date_str: str, limit: int = 200) -> list[discord.Embed]:
    """Every archived parlay embed tagged with this date, oldest first (as
    originally posted) - a straight re-display of what was already
    published, no re-resolution needed since a published report is
    already a final, graded snapshot."""
    matches: list[tuple[discord.Message, discord.Embed]] = []
    async for message in channel.history(limit=limit):
        for embed in message.embeds:
            if archived_date_from_embed(embed) == date_str:
                matches.append((message, embed))
    matches.reverse()
    return [embed for _message, embed in matches]


async def _find_slip_in_window(
    channel: discord.abc.Messageable, window_start: float, window_end: Optional[float], limit: int,
) -> Optional[list[discord.Message]]:
    """Every message within [window_start, window_end) (window_end=None
    means open-ended, up through the newest message) that belongs to one
    same-author run containing at least one recognizable parlay, oldest
    first - combined into one slip regardless of how far apart they land
    within the window. GreenFox's own slips routinely split across
    multiple messages (a real one landed 20 seconds apart, Discord's own
    per-message length cap), each continuing the previous one's parlay
    list rather than repeating the banner - and per explicit request,
    messages posted later in the same window (e.g. a follow-up slip 30
    minutes after the first) are meant to combine too, not just messages
    close together in time. Doesn't check the author specifically (so
    this isn't tied to GreenFox's own account id) beyond requiring every
    message in the run to share one. None if nothing qualifies within
    `limit` messages. oldest_first=False is explicit here - passing
    after= alone flips discord.py's own default to oldest-first, which
    would silently break this scan's newest-first assumption."""
    after = datetime.datetime.fromtimestamp(window_start, tz=datetime.timezone.utc)
    before = datetime.datetime.fromtimestamp(window_end, tz=datetime.timezone.utc) if window_end is not None else None
    history = [message async for message in channel.history(limit=limit, after=after, before=before, oldest_first=False)]
    start_idx = next((i for i, m in enumerate(history) if parse_master_parlays(m.content)), None)
    if start_idx is None:
        return None
    run = [history[start_idx]]
    for message in history[start_idx + 1 :]:
        prev = run[-1]
        if message.author.id != prev.author.id:
            break
        run.append(message)
    run.reverse()
    return run


async def find_latest_slip(channel: discord.abc.Messageable, limit: int = 200, now: Optional[float] = None) -> Optional[list[discord.Message]]:
    """The parlay day whose posting window most recently closed (see
    _live_window) - None once that window closed with nothing posted in
    it, even if an older slip still exists earlier in the channel's
    history. That older slip is available through the archive instead
    (see find_archived_report), not as a silent fallback here."""
    now = now if now is not None else time.time()
    window_start, window_end = _live_window(now)
    return await _find_slip_in_window(channel, window_start, window_end + 1, limit)  # +1s: window_end itself is inclusive


async def _find_slip_for_parlay_day(channel: discord.abc.Messageable, date_str: str, limit: int = 200) -> Optional[list[discord.Message]]:
    """The slip (if any) that belongs to the given parlay day - used by
    the nightly auto-archive job to grab the day that's ready to
    archive."""
    day = datetime.date.fromisoformat(date_str)
    window_end = datetime.datetime.combine(day, datetime.time(_PARLAY_DAY_CUTOFF_HOUR), tzinfo=scores365.EASTERN).timestamp()
    window_start = _cutoff_plus_days(window_end, -1)
    return await _find_slip_in_window(channel, window_start, window_end + 1, limit)  # +1s: window_end itself is inclusive


async def auto_archive_parlay_day(
    slip_channel: discord.abc.Messageable, archive_channel: discord.abc.Messageable, date_str: str,
) -> Optional[int]:
    """Archives every parlay found in date_str's slip (no curation -
    there's nobody around at 3am to click checkboxes). Returns how many
    parlays got archived, or None if there was no slip for that day at
    all. Doesn't check whether date_str is already archived - see
    auto_archive_if_needed for that."""
    messages = await _find_slip_for_parlay_day(slip_channel, date_str)
    if not messages:
        return None
    embeds = await build_report(combine_slip_text(messages), date_str)
    if not embeds:
        return None
    for i in range(0, len(embeds), 10):
        await archive_channel.send(embeds=embeds[i : i + 10])
    return len(embeds)


async def auto_archive_if_needed(
    slip_channel: discord.abc.Messageable, archive_channel: discord.abc.Messageable, date_str: str,
) -> Optional[int]:
    """Skips date_str if anything's already archived for it (a manual
    Publish earlier in the day takes precedence - no duplicate entries),
    otherwise archives the full day via auto_archive_parlay_day. Returns
    how many parlays got archived, or None if skipped / nothing to
    archive."""
    existing = await find_archived_report(archive_channel, date_str, limit=50)
    if existing:
        return None
    return await auto_archive_parlay_day(slip_channel, archive_channel, date_str)

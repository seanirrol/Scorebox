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
"""

import asyncio
import re
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
# only usable here) - published/archived reports now post back into this
# same channel too (per explicit request), rather than a separate archive
# channel. /premiumparlay's own "View Past Dates" browsing (see
# find_archived_report) reads from here as well, since re-resolving an
# old slip from scratch runs into 365scores' own bulk-feed data window
# closing within a day or two.
PARLAY_SLIP_CHANNEL_ID = 1538412823250608300
PUBLISH_CHANNEL_ID = PARLAY_SLIP_CHANNEL_ID

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
            current["legs"].append(leg.group(1).strip())
            continue
        recommended_leg = _RECOMMENDED_LEG_RE.match(line)
        if recommended_leg and current is not None:
            current["legs"].append(_clean_recommended_leg(recommended_leg.group(1).strip()))
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
    for resolver in (_resolve_f5_ml, _resolve_games_handicap, _resolve_win_a_set, _resolve_ml_or_spread, _resolve_player_prop):
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
    """The Eastern calendar date this slip "belongs to" - the earliest
    message's own posted time, not whenever Publish happens to get
    clicked. Confirmed live as the reason this matters: a slip posted at
    11pm whose last leg doesn't finish until after midnight (extra
    innings, a long match) must still archive under the day it was
    posted, not the day someone finally published it."""
    earliest = min(messages, key=lambda m: m.created_at)
    return scores365.eastern_date_str(earliest.created_at.timestamp()) or dailylog.today_str()


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


async def find_latest_slip(channel: discord.abc.Messageable, limit: int = 50) -> Optional[list[discord.Message]]:
    """Every message from the most recent Eastern calendar date (matching
    dailylog's own date convention) that has at least one same-author
    message containing a recognizable parlay, oldest first - combined
    into one slip regardless of how far apart they land within that day.
    GreenFox's own slips routinely split across multiple messages (a
    real one landed 20 seconds apart, Discord's own per-message length
    cap), each continuing the previous one's parlay list rather than
    repeating the banner - and per explicit request, messages posted
    later the same day (e.g. a follow-up slip 30 minutes after the
    first) are meant to combine too, not just messages close together in
    time. Doesn't check the author specifically (so this isn't tied to
    GreenFox's own account id) beyond requiring every message in the run
    to share one. None if nothing qualifies within `limit` messages."""
    history = [message async for message in channel.history(limit=limit)]  # newest first
    start_idx = next((i for i, m in enumerate(history) if parse_master_parlays(m.content)), None)
    if start_idx is None:
        return None
    run = [history[start_idx]]
    target_date = scores365.eastern_date(history[start_idx].created_at.timestamp())
    for message in history[start_idx + 1 :]:
        prev = run[-1]
        if message.author.id != prev.author.id or scores365.eastern_date(message.created_at.timestamp()) != target_date:
            break
        run.append(message)
    run.reverse()
    return run

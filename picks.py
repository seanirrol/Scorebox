#!/usr/bin/env python3
"""
Parses free-text sports "picks" (e.g. from a tips channel) into structured
team/player+stat info, fed into /track's and /playerprops' auto-tracking
logic in bot.py. Best-effort only - anything ambiguous or referencing an
unsupported sport/category is skipped (returns None), never guessed.

Expected line shape, one pick per line:
  "N. [Category] Description (Bookmaker) (Bookmaker odds)"
e.g.
  "1. [MLB Props] Connor Prielipp Under 5.5 Strikeouts (DraftKings -210)"
  "2. [Soccer] Monterrey ML (Bet365 -190)"
  "3. [MLB] Los Angeles Angels vs San Francisco Giants - Over 8.5 Total Runs"
  "7. [MLB] Philadelphia Phillies Moneyline (Fanatics -179)"
"""

import re
from typing import Optional

import espn
import playerstatsfootball
import scores365

# Bracket category (lowercased, "Props" suffix stripped) -> our sport key.
# Baseball has multiple league names in the wild (MLB, KBO, ...) that all map
# to the same "baseball" sport key - 365scores/ESPN aren't scoped per-league
# for team/game lookups (confirmed live: 365scores' baseball feed already
# includes KBO games alongside MLB), just the label users type differs.
_SPORT_MAP = {
    "mlb": "baseball",
    "mbl": "baseball",  # confirmed live - a real header typo'd this way twice
    "kbo": "baseball",
    "nba": "basketball",
    "wnba": "basketball",
    "nfl": "nfl",
    "nhl": "hockey",
    "soccer": "soccer",
    "tennis": "tennis",
    "rugby": "rugby",
    "volleyball": "volleyball",
    "ufc": "mma",
    "mma": "mma",
    "combat": "mma",
    "pfl": "mma",  # espn_ufc.py's scoreboard already covers PFL, not just UFC
    "boxing": "boxing",
    "dota 2": "dota2",
    "dota2": "dota2",
    "cs2": "cs2",
    "cs 2": "cs2",
    "counter-strike": "cs2",
    "counter strike": "cs2",
}

# Bare section headers can use the sport's full name ("Basketball") instead
# of the league abbreviation bracket tags always use ("NBA") - confirmed live
# a "Basketball" header was silently dropped because only "nba" was
# recognized. "Football" is deliberately left out: ambiguous between NFL and
# soccer depending on the source, so it's safer to skip than guess.
_HEADER_SPORT_MAP = {
    **_SPORT_MAP,
    "baseball": "baseball",
    "basketball": "basketball",
    "hockey": "hockey",
}

# Optional leading numbered ("1.", "2)") OR bullet ("•", or a literal "-"/
# "*" markdown list marker - Discord renders any of these as a bullet point,
# so the raw text the bot actually receives could be either) prefix before
# a "[Category]" tag - confirmed live, a real message mixed a numbered top
# list with a bulleted list below it, and the bulleted half silently never
# matched at all.
_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[•\-*]\s*)?\[([^\]]+)\]\s*(.+)$")

# A raw line is expected to carry at most one "[Category]" tag - but a
# source message occasionally squeezes two separate picks onto one raw
# line with no newline between them (confirmed live: a real message had
# "[WNBA Props] ... (PrizePicks -100) [MLB] Team A vs Team B - ..." all on
# one line - likely an edited pick re-pasted without its line break).
# _LINE_RE's own description group is greedy to end-of-line, so without
# this the second tag gets silently swallowed as literal text inside the
# first pick's team-name search, and the second pick is lost entirely.
_EMBEDDED_TAG_RE = re.compile(r"\[[^\]]+\]")


def _split_merged_bracket_lines(line: str) -> list[str]:
    """Splits a line at every "[Category]" tag beyond the first, so each
    embedded pick still gets parsed as its own line. A no-op (returns
    [line]) for the overwhelmingly common case of zero or one tag."""
    positions = [m.start() for m in _EMBEDDED_TAG_RE.finditer(line)]
    if len(positions) <= 1:
        return [line]
    splits = [line[positions[i]:positions[i + 1]].strip() for i in range(len(positions) - 1)]
    splits.append(line[positions[-1]:].strip())
    if positions[0] > 0:
        # Any text before the first tag (e.g. a numbered/bulleted prefix
        # _LINE_RE already handles) stays attached to the first segment.
        splits[0] = line[:positions[0]] + splits[0]
    return splits
# The optional "the" handles wording like "Roki Sasaki Over the 1.5 Walks
# Allowed" (confirmed live - a real pick worded this way silently never
# posted, since the number wasn't immediately after Over/Under). The
# optional "(...)" right after the number handles "(Alt Line)" placed
# BETWEEN the number and the stat name (e.g. "OVER 1.5 (Alt Line) THREE
# POINTERS") rather than at the very end where _clean_line's trailing-
# paren stripping would remove it - confirmed live, without this the
# whole "(Alt Line) Three Pointers" text got captured as the stat name,
# which then matched nothing in STAT_CATALOG and silently misparsed as a
# team pick instead of a player prop.
_PLAYER_STAT_RE = re.compile(
    r"^(.+?)\s+(Over|Under)\s+(?:the\s+)?([\d.]+)\s+(?:\([^)]*\)\s+)?(.+?)\s*(?:\(|$)", re.IGNORECASE,
)

# Combined basketball stat props ("P+R+A") are conventionally worded with
# the stat token BEFORE "Over/Under" (e.g. "Shakira Austin P+R+A Over
# 24.5") - the reverse of every other player prop this module parses
# (_PLAYER_STAT_RE above expects "Player Over N Stat"). A generic
# "stat can be on either side" regex would be ambiguous for ordinary player
# names (no way to tell where the name ends and an arbitrary stat begins) -
# safe here only because the combo-stat token itself is a small, fixed,
# unambiguous set, always immediately followed by whitespace + Over/Under.
_COMBO_STAT_BEFORE_RE = re.compile(
    r"^(.+?)\s+(P\s*\+\s*R\s*\+\s*A|PRA)\s+(Over|Under)\s+(?:the\s+)?([\d.]+)\s*(?:\(|$)",
    re.IGNORECASE,
)

# "Team A vs Team B - YRFI - Yes Runs 1st Inning (...)" - settles after just
# the 1st inning, not the whole game, so it's routed to inningtracker.py
# rather than /track's kind="track" (see _parse_yrfi_line). Only one of the
# two team names is captured - either one is enough to look the game up.
# Confirmed live: a real picks message used "Team A vs Team B: NRFI - ..."
# (colon instead of dash before the market name) and every line silently
# failed to parse - accepting either separator here covers both. Also
# accepts "@" as the matchup separator (the away-@-home convention used
# instead of "vs" for some picks) - same separator set as
# _INNING_RUN_TOTAL_RE just below - confirmed live: "Philadelphia Phillies
# @ St. Louis Cardinals - NRFI - ..." silently parsed to zero picks.
_YRFI_LINE_RE = re.compile(r"^(.+?)\s*(?:@|\bvs\.?\b|\bv\.?\b)\s*.+?[-:]\s*(YRFI|NRFI)\b", re.IGNORECASE)

# "Team A vs Team B - Over 0.5 1st Inning (...)" means exactly the same bet
# as YRFI ("at least 1 run scores in the 1st inning") worded differently -
# 0.5 is the only line where "any run" vs "no runs" are the two possible
# outcomes, so it's routed to the same inningtracker.py kind rather than
# treated as a game/team total. Only the 0.5 line is recognized - "Over 1.5
# 1st Inning" means something else (2+ runs) that inningtracker.py doesn't
# grade, so that's deliberately left unmatched and falls through to being
# skipped rather than guessed.
_INNING_RUN_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(Over|Under)\s+0\.5\s+1st\s+Inning\b",
    re.IGNORECASE,
)

# "TeamName 1st Inning Moneyline" - a 3-way "1st inning result" pick backing
# a specific team to lead after the 1st inning (see _parse_inning1_team_pick).
# The Draw side of this same market needs a matchup instead of a single team
# name, so it's handled separately below (_INNING1_DRAW_RE) - this is
# genuinely a different market from YRFI/NRFI (any runs vs none) or F5
# moneyline (which treats a tie as a push, not a separately-winnable Draw).
_INNING1_RESULT_MARKER_RE = re.compile(r"\b1st\s+inning\b", re.IGNORECASE)

# "Naomi Osaka to win 1st Set" / "Naomi Osaka 1st Set ML" / "Naomi Osaka 1st
# Set winner" / "Naomi Osaka ML 1st Set" - a tennis set-winner pick (settles
# once Set 1 finishes, not the whole match - see settracker.py). Two-way, no
# Draw side to worry about (unlike the 1st-inning-result market above), so a
# single marker regex covers every phrasing. Also covers the reversed word
# order "To Win Set 1" / "Set 1 ML/Moneyline/Winner" / "ML Set 1" - confirmed
# live, a real slate of picks used "Set 1" exclusively and none of them
# parsed at all.
_SET1_MARKER_RE = re.compile(
    r"\bto\s+win\s+1st\s+set\b|\bto\s+win\s+set\s*1\b"
    r"|\b1st\s+set\s+(?:ml|moneyline|winner)\b|\bset\s*1\s+(?:ml|moneyline|winner)\b"
    r"|\bml\s+1st\s+set\b|\bml\s+set\s*1\b",
    re.IGNORECASE,
)

# "Team A vs Team B - 1st Set Total Games Over 8.5" / "... - 1st Set Over
# 8.5 Games" - the COMBINED games played in Set 1 (both sides summed), not
# who wins it (see _SET1_MARKER_RE above for that). Checked before the
# generic game-total parser for the same reason F5's combined total is -
# without the explicit "1st Set" marker this would otherwise be graded
# against the whole match's sets-won score instead of just Set 1's games.
_SET1_TOTAL_GAMES_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*1st\s+set\s*(?:total\s+)?(?:games\s+)?(Over|Under)\s+([\d.]+)",
    re.IGNORECASE,
)
# Same market, number-before-marker word order (confirmed live: "Over 8.5
# 1st Set Total Games") - mirrors _MATCH_TOTAL_GAMES_AFTER_RE's same
# before/after split below for the whole-match version.
_SET1_TOTAL_GAMES_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-?\s*(Over|Under)\s+([\d.]+)\s*1st\s+set\s*(?:total\s+)?games\b",
    re.IGNORECASE,
)

# "Team A vs Team B - Total Games Over 23.5" (marker before the number) /
# "Team A vs Team B - Over 23.5 Total Games" or "... Over 23.5 Games"
# (marker after) - the combined games across the WHOLE match, not just Set
# 1. Requires an explicit "Games" marker somewhere - without it this would
# otherwise be indistinguishable from (and silently mis-graded as) a
# generic total against tennis's sets-won score, which could never reach a
# realistic games-total line like 23.5.
_MATCH_TOTAL_GAMES_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(?:total\s+)?games\s+(Over|Under)\s+([\d.]+)",
    re.IGNORECASE,
)
_MATCH_TOTAL_GAMES_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-?\s*(Over|Under)\s+([\d.]+)\s*(?:total\s+)?games\b",
    re.IGNORECASE,
)

# "Rebecca Sramkova vs Anna Blinkova - Rebecca Sramkova Over 11.5 Total
# Games" - a named PLAYER's own games won across the whole match, not the
# two sides combined (see _MATCH_TOTAL_GAMES_.../grade_over_under above) -
# a distinct, commonly-bet prop line. The named player between the dash and
# Over/Under is what distinguishes this from the combined-total shape above
# (which has no name there at all) - checked first and validated against
# the matchup the same way _WIN_A_SET_RE is, falling through to the
# combined-total parser if it doesn't look like either side (confirmed live
# this was previously silently swallowed as a combined match total, with
# the player's own name folded into the "team" match-lookup string instead
# of ever being used to orient which side's games actually get graded).
_PLAYER_TOTAL_GAMES_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(?:total\s+)?games\s+(Over|Under)\s+([\d.]+)",
    re.IGNORECASE,
)
_PLAYER_TOTAL_GAMES_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(Over|Under)\s+([\d.]+)\s*(?:total\s+)?games\b",
    re.IGNORECASE,
)

# Same market, no matchup at all - just "Player - Over/Under N Games"
# (confirmed live, a real slate of picks used this shape exclusively; the
# opponent isn't actually needed, find_match_for_team only needs the one
# name). Without an opposing name there's nothing to validate the player
# against, unlike _PLAYER_TOTAL_GAMES_BEFORE/AFTER_RE above - the dash
# prefix is taken as the player's name directly. Checked ahead of the
# generic player-prop parser (see the "if sport == tennis" no-matchup
# block) so "Games" doesn't get silently rejected as an unrecognized stat
# the way "Breaks" was before _TENNIS_STAT_ALIASES.
#
# The dash itself is optional - confirmed live a real "Felix
# Auger-Aliassime Under 21.5 Total Games" pick had no separating dash at
# all (unlike the dashed slate that originally motivated this parser). Safe
# to drop even for a player name with its own internal hyphen (e.g.
# "Auger-Aliassime") since group 1 is non-greedy and the Over/Under anchor
# only lines up correctly at the real name/bet boundary, not mid-name.
_PLAYER_TOTAL_GAMES_NOMATCHUP_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:-\s*)?(?:total\s+)?games\s+(Over|Under)\s+([\d.]+)", re.IGNORECASE,
)
_PLAYER_TOTAL_GAMES_NOMATCHUP_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:-\s*)?(Over|Under)\s+([\d.]+)\s*(?:total\s+)?games\b", re.IGNORECASE,
)

# "Brandon Nakashima -2.5 Games" - a games-margin HANDICAP (spread), not an
# Over/Under total - confirmed live, a real pick worded this way (no
# opponent named at all, same no-matchup shape as
# _PLAYER_TOTAL_GAMES_NOMATCHUP_*_RE above). Distinguished from every other
# Games market in this file by a signed number directly after the player's
# name instead of an "Over"/"Under" marker - checked ahead of the no-
# matchup total-games parsers below since those would otherwise never match
# a bare signed number anyway (they require the literal word Over/Under),
# but ordering it first keeps the two shapes clearly separated.
_GAMES_HANDICAP_NOMATCHUP_RE = re.compile(
    r"^(.+?)\s+([+-]\d+(?:\.\d+)?)\s*(?:total\s+)?games\b", re.IGNORECASE,
)

# "Wang Xiyu +1.5 Sets" - a sets-margin HANDICAP, same no-matchup shape as
# _GAMES_HANDICAP_NOMATCHUP_RE above but against sets won instead of games
# won - confirmed live, a real pick worded this way went completely
# unparsed (no regex matched "Sets" at all, only "Games"), silently
# vanishing from the picks count with no botlog trace whatsoever.
_SETS_HANDICAP_NOMATCHUP_RE = re.compile(
    r"^(.+?)\s+([+-]\d+(?:\.\d+)?)\s*sets\b", re.IGNORECASE,
)

# "Team A vs Team B - Venus Williams to Win a Set" (Yes) / "... - Venus
# Williams No to Win a Set" / "... - Not Venus Williams to Win a Set" (No,
# either word order) - whether the named player wins at least one set
# during the match, not who wins the match outright.
_WIN_A_SET_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+to\s+win\s+a\s+set\b",
    re.IGNORECASE,
)

# "Stefanos Tsitsipas to Win at Least 1 Set" / "Venus Williams to Win a
# Set" - the same market as _WIN_A_SET_RE, just with no opponent named at
# all (same no-matchup shape as _SETS_HANDICAP_NOMATCHUP_RE above) -
# confirmed live, this exact "to Win at Least 1 Set" wording (no matchup
# prefix, and "at least 1" instead of "a") fell through to the simple-
# name fallback, which rejects anything with a digit in it, and vanished
# with no botlog trace at all - 7 of 8 picks in one real message.
_WIN_A_SET_NOMATCHUP_RE = re.compile(
    r"^(.+?)\s+to\s+win\s+(?:a\s+set|at\s+least\s+1\s+sets?)\b", re.IGNORECASE,
)

# "Team A vs Team B - 1st Inning Draw" (or "Tie") - the Draw side of the same
# 3-way "1st inning result" market. Either team name is enough to look the
# game up - grading only cares whether the 1st inning runs end level.
_INNING1_DRAW_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*1st\s+inning\s+(?:draw|tie)\b",
    re.IGNORECASE,
)

# "F5"/"First 5 Innings" moneyline OR team total - both settle after the 5th
# inning, not the whole game, so they're routed to f5tracker.py rather than
# /track's kind="track" (see _parse_f5_pick / _parse_f5_total_pick). A
# combined-game F5 total (both sides' runs summed) still isn't handled -
# only a single team's own total - so that flavor still falls through to
# _PARTIAL_GAME_MARKERS below and gets skipped rather than mistakenly
# tracked as a full-game pick.
_F5_MARKER_RE = re.compile(
    r"\bf5\b|\bfirst\s+5\s+innings\b|\bfirst\s+five\s+innings\b|\b1st\s+5\s+innings\b", re.IGNORECASE
)

# "Kia Tigers F5 Total Over 2.5" / "Kia Tigers F5 Team Total Over 2.5" /
# "Kia Tigers First 5 Innings Over 2.5" - one team's own 1st-5th inning run
# total against a line, not compared to the other side (see
# grade_f5_team_total). "Team"/"Total" are both optional wording - only the
# F5 marker + Over/Under + number are actually required.
_F5_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:f5|first\s+5\s+innings|first\s+five\s+innings|1st\s+5\s+innings)"
    r"\s*(?:team\s*)?(?:total\s*)?(Over|Under)\s+([\d.]+)\s*$",
    re.IGNORECASE,
)

# "St. Louis Cardinals vs Chicago Cubs - F5 Total Over 4.5" - a COMBINED F5
# total (both sides' 1st-5th inning runs summed), not one team's own total
# (see _F5_TOTAL_RE above) - the matchup (two team names) is what
# distinguishes it. Checked before the generic game-total parser below, since
# confirmed live that without this, "F5 Total" got silently swallowed and
# this graded against the WHOLE game's total instead of just the first 5
# innings.
_F5_COMBINED_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*"
    r"(?:f5|first\s+5\s+innings|first\s+five\s+innings|1st\s+5\s+innings)"
    r"\s*(?:total\s*)?(Over|Under)\s+([\d.]+)",
    re.IGNORECASE,
)

# "NC Dinos vs Kia Tigers - F5 Kia Tigers +0.5" - an F5 run-line/handicap:
# the named team's own 1st-5th inning runs, adjusted by the signed line,
# compared against the other side's (see grade_f5_handicap) - distinct from
# _F5_TOTAL_RE/_F5_COMBINED_TOTAL_RE, which grade a team's (or the combined)
# runs against an Over/Under line rather than adjusting one side's score.
# The named team is validated against a matchup side the same way
# _NAMED_TEAM_TOTAL_RE is, to avoid false positives.
_F5_HANDICAP_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*"
    r"(?:f5|first\s+5\s+innings|first\s+five\s+innings|1st\s+5\s+innings)"
    r"\s+(.+?)\s+([+-]\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

# "1H"/"1st Half"/"First Half" team total OR combined total for football -
# settle once the 2nd quarter is fully complete, not the whole game, so
# they're routed to halftracker.py rather than /track's kind="track" (see
# _parse_1h_total_pick / _parse_1h_combined_total_pick). No moneyline/
# handicap flavor here (unlike F5's baseball equivalent) - not asked for.
_1H_MARKER_RE = re.compile(r"\b1h\b|\b1st\s+half\b|\bfirst\s+half\b", re.IGNORECASE)

# "Arizona Cardinals 1H Over 10.5 Total Points" / "Arizona Cardinals 1H Team
# Total Over 10.5" - one team's own Q1+Q2 point total against a line, not
# compared to the other side (see scores365.grade_1h_team_total). "Team"/
# "Total" are both optional wording, and a trailing stat word after the
# number (e.g. "Total Points") is fully optional too - only the 1H marker +
# Over/Under + number are actually required, same flexibility as
# _TOTAL_LINE_RE for the generic full-game total.
_1H_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:1h|1st\s+half|first\s+half)"
    r"\s*(?:team\s*)?(?:total\s*)?(Over|Under)\s+([\d.]+)(?:\s+\S.*)?\s*$",
    re.IGNORECASE,
)

# "Arizona Cardinals vs Carolina Panthers - 1H Total Under 17.5 Total
# Points" - a COMBINED 1st-half total (both sides' Q1+Q2 points summed), not
# one team's own total (see _1H_TOTAL_RE above) - the matchup (two team
# names) is what distinguishes it. Checked before the generic game-total
# parser below, same reasoning as F5's combined total for baseball.
_1H_COMBINED_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*"
    r"(?:1h|1st\s+half|first\s+half)"
    r"\s*(?:total\s*)?(Over|Under)\s+([\d.]+)",
    re.IGNORECASE,
)

# "Denver Broncos -3.5" - a full-game point-spread pick, no opponent named
# at all (this tipster's own wording always drops it - see
# scores365.grade_spread). Distinguished from every other shape in this
# file by a signed number directly after the team name with nothing else
# meaningful trailing it (unlike the tennis games/sets handicap variants
# above, which require a trailing "Games"/"Sets" word). Only checked for
# sport in ("nfl", "basketball") (see _parse_description) - confirmed live
# for NFL first, then WNBA (e.g. "Phoenix Mercury -2.5", "Indiana Fever
# +5.5" - both silently unparsed before this); basketball covers WNBA and
# NBA alike since _SPORT_MAP maps both to the same "basketball" sport key.
# Still scoped narrowly rather than opened to every sport - not yet
# confirmed live anywhere else.
#
# An optional trailing "Points"/"Pts"/"Runs"/"Goals" is allowed after the
# number - confirmed live, "Las Vegas Aces -1.5 Points" silently swallowed
# the whole line (including the spread number) as a literal team name,
# since that's just this sport's own scoring unit decorating a plain
# full-game spread, not a different market. Deliberately NOT a bare \w+
# (which would also match "1st"/"Half"/"Q1"-style period-market suffixes
# and misfile a half/quarter spread as a full-game one).
_TEAM_SPREAD_NOMATCHUP_RE = re.compile(r"^(.+?)\s+([+-]\d+(?:\.\d+)?)(?:\s+(?:Points|Pts|Runs|Goals))?\s*$", re.IGNORECASE)


def _parse_team_spread_nomatchup_pick(sport: str, description: str) -> Optional[dict]:
    text = _clean_line(description)
    # A matchup-shaped line (e.g. "Team A @ Team B -3.5") isn't this no-
    # opponent-named shape - left unmatched rather than misreading the
    # whole "Team A @ Team B" string as one literal team name.
    if any(sep in text for sep in (" vs. ", " vs ", " v. ", " v ", " @ ")):
        return None
    m = _TEAM_SPREAD_NOMATCHUP_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {"kind": "team_total", "sport": sport, "team": team, "direction": "spread", "line": float(m.group(2))}

# "Daniel Rodriguez vs Uros Medic - Under 2.5 Rounds" - a UFC round total
# (see grade_ufc_round_total). Requires "Rounds" explicitly since it would
# otherwise match the same shape as a generic combined game total
# (_TOTAL_LINE_RE) - checked before that generic parser for the same reason
# F5's combined total is checked before it for baseball.
_UFC_ROUND_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(?:total\s*)?(Over|Under)\s+([\d.]+)\s*rounds?\b",
    re.IGNORECASE,
)

# "Team A @ Team B - Over 9.5", "... - Over 8.5 Total Runs", or "Team A vs
# Team B Under 4" (no dash at all - confirmed live, a real soccer pick was
# worded this way) - a game total, not a single player's stat
# (_parse_player_prop requires a stat word after the number and would never
# match this shape). Checked before team-pick parsing so it doesn't get
# silently mis-tracked as a moneyline pick on Team A (confirmed this was
# actually happening for the "Total Runs" wording example already called
# out in this module's own docstring). The dash is optional since not every
# source includes one.
# The trailing stat word after the number is fully optional and unanchored
# to any specific wording ("Total Runs", "Goals", "Total Points", or
# nothing at all) - confirmed live "LA Galaxy vs FC Dallas Over 2.5 Goals"
# (no literal "Total") previously fell through this regex entirely (it only
# accepted a bare number or "Total <word>"), silently misparsed as a team
# moneyline pick on "LA Galaxy" instead of a game total.
_TOTAL_LINE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*(?:-\s*)?(Over|Under)\s+([\d.]+)(?:\s+\S.*)?\s*$",
    re.IGNORECASE,
)

# "New York Yankees vs Chicago White Sox - New York Yankees Over 3.5 Team
# Total Runs" - a single team's own FULL-GAME total (not F5-scoped - see
# _F5_COMBINED_TOTAL_RE above for that), the named team repeated between the
# matchup and the number is what distinguishes it from a combined total.
# No `$` anchor - real picks sometimes have trailing text/malformed
# parens after the number (confirmed live) that isn't worth choking on.
# The named team is validated against one of the two matchup sides
# (fuzzy-matched) rather than trusted blindly, so stray wording here
# doesn't get mistaken for a team name.
_NAMED_TEAM_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(Over|Under)\s+([\d.]+)\b",
    re.IGNORECASE,
)

# "Athletics - Over 1.5 Total Score" / "Athletics Over 1.5" - a single
# team's own full-game total with NO opponent stated at all (unlike
# _NAMED_TEAM_TOTAL_RE above, which requires a full "Team A vs Team B -
# Team Over N" matchup) - confirmed live, a real pick worded this way.
# Only reachable from inside the has_matchup==False branch (see
# _parse_description), so there's no risk of this swallowing a real
# matchup line - has_matchup already filtered those out by the time this
# runs. find_match_for_team already resolves a bare team name to its
# current game without needing the opponent named, same as a bare
# moneyline pick, so this reuses the exact same "team_total" kind/grading
# path as _NAMED_TEAM_TOTAL_RE instead of needing its own.
_BARE_TEAM_TOTAL_RE = re.compile(
    r"^(.+?)\s*-?\s*(Over|Under)\s+([\d.]+)(?:\s+\S.*)?\s*$",
    re.IGNORECASE,
)

# Maps wording that doesn't literally match a STAT_CATALOG label to the
# label it actually means - either because the bet type is genuinely
# ambiguous (a baseball "Strikeouts" prop with no other context: pitcher
# strikeouts is the overwhelmingly common bet type, so default there rather
# than guessing batting vs pitching from wording alone), or because it's
# just a common phrasing variant of an existing label (confirmed live:
# "Dustin May Over 1.5 Earned Runs Allowed" silently dropped the whole pick,
# since the catalog only has "Earned Runs" - unambiguous, since there's no
# separate batting-context "Earned Runs" to confuse it with).
_AMBIGUOUS_STAT_DEFAULTS = {
    ("baseball", "strikeouts"): "Strikeouts (Pitching)",
    # These three variants don't hit this dict at all without an explicit
    # entry - they fall through to _match_stat_label's substring-fallback
    # loop instead, which matches whichever catalog label happens to come
    # first when two labels share a base name after stripping " (...)"
    # ("Strikeouts (Batting)" is defined before "Strikeouts (Pitching)" in
    # STAT_CATALOG) - confirmed live: even "Pitcher Strikeouts", a phrase
    # that explicitly says pitcher, silently matched the batting stat and
    # voided a fully-graded pitcher prop (pitchers never appear in the
    # batting box-score group, so the lookup came back empty).
    ("baseball", "player strikeouts"): "Strikeouts (Pitching)",
    ("baseball", "total strikeouts"): "Strikeouts (Pitching)",
    ("baseball", "pitcher strikeouts"): "Strikeouts (Pitching)",
    ("baseball", "earned runs allowed"): "Earned Runs",
    ("baseball", "walks allowed"): "Walks (Pitching)",
    # Same ambiguity/substring-fallback issue as strikeouts above - bare
    # "Walks" (and these two variants) silently matched the batting "Walks"
    # stat instead of "Walks (Pitching)". Confirmed live: two real picks
    # ("J.T. Ginn Over 2.5 Walks", "Brady Singer Over 1.5 Walks" - both
    # pitchers) graded a "-" (never appeared in the batting group) and
    # voided on a fully-graded start.
    ("baseball", "walks"): "Walks (Pitching)",
    ("baseball", "player walks"): "Walks (Pitching)",
    ("baseball", "total walks"): "Walks (Pitching)",
    # "Rushing Yards" doesn't substring-match "rush yards" in either
    # direction (the "ing" breaks the contiguous substring) - confirmed
    # live: "Trey Benson - Over 33.5 Rush yards" silently dropped the
    # whole pick.
    ("nfl", "rush yards"): "Rushing Yards",
    # "Pass Yds"/"Rush Yds" (this source's own abbreviated wording, distinct
    # from the "Rush yards" variant above) share no substring with "Passing
    # Yards"/"Rushing Yards" either - confirmed live, "Tyrod Taylor Over
    # 75.5 Pass Yds" and "Emanuel Wilson Over 15.5 Rush Yds" both silently
    # fell through to a bare team-pick attempt on the player's own name
    # instead of tracking the prop.
    ("nfl", "pass yds"): "Passing Yards",
    ("nfl", "rush yds"): "Rushing Yards",
    ("nfl", "rec yds"): "Receiving Yards",
    ("nfl", "receiving yds"): "Receiving Yards",
    # "3-Pointers Made" shares no substring with any of these (confirmed
    # live: "A'ja Wilson Over 0.5 player threes" silently dropped the whole
    # pick), unlike most other stat wording variants this module already
    # fuzzy-matches via _match_stat_label's substring check.
    ("basketball", "threes"): "3-Pointers Made",
    ("basketball", "player threes"): "3-Pointers Made",
    ("basketball", "made threes"): "3-Pointers Made",
    ("basketball", "three pointers"): "3-Pointers Made",
    ("basketball", "3 pointers"): "3-Pointers Made",
    ("basketball", "3-point field goals"): "3-Pointers Made",
    ("basketball", "total 3-point field goals"): "3-Pointers Made",
    ("wnba", "threes"): "3-Pointers Made",
    ("wnba", "player threes"): "3-Pointers Made",
    ("wnba", "made threes"): "3-Pointers Made",
    ("wnba", "three pointers"): "3-Pointers Made",
    ("wnba", "3 pointers"): "3-Pointers Made",
    ("wnba", "3-point field goals"): "3-Pointers Made",
    ("wnba", "total 3-point field goals"): "3-Pointers Made",
    # "P+R+A"/"PRA" (points + rebounds + assists) shares no substring with
    # the catalog's "Points + Rebounds + Assists" label - same fuzzy-match
    # gap as the 3-Pointers aliases above. Covers the less common
    # stat-after-Over wording ("... Over 24.5 P+R+A"); the far more common
    # stat-before-Over wording ("... P+R+A Over 24.5") is handled separately
    # by _COMBO_STAT_BEFORE_RE, not through this table.
    ("basketball", "p+r+a"): "Points + Rebounds + Assists",
    ("basketball", "pra"): "Points + Rebounds + Assists",
    ("wnba", "p+r+a"): "Points + Rebounds + Assists",
    ("wnba", "pra"): "Points + Rebounds + Assists",
    # "Pts + Rebs + Asts"/"Pts+Rebs+Asts" (spelled-out-shorthand, as opposed
    # to the P+R+A/PRA initialism above) - confirmed live, a real
    # "Gabby Williams Over 14.5 Pts + Rebs + Asts" pick silently failed to
    # parse even with a correct [WNBA Props] tag, since _match_stat_label
    # only strips/lowercases (no internal whitespace collapsing), so the
    # spaced and unspaced forms need their own separate entries here.
    ("basketball", "pts + rebs + asts"): "Points + Rebounds + Assists",
    ("basketball", "pts+rebs+asts"): "Points + Rebounds + Assists",
    ("wnba", "pts + rebs + asts"): "Points + Rebounds + Assists",
    ("wnba", "pts+rebs+asts"): "Points + Rebounds + Assists",
}

# _SPORT_MAP collapses "nba"/"wnba" to the same "basketball" key since
# 365scores' team search spans both leagues under one sport type - but ESPN's
# player-prop lookup is scoped to a single league per sport key, so a WNBA
# prop needs to be routed to its own "wnba" ESPN sport instead of "basketball"
# (which only points at the NBA endpoint). Team picks aren't affected by this.
#
# "kbo" is the opposite problem: _SPORT_MAP collapses it into "baseball" too
# (365scores' game-level search does span both leagues), but ESPN has no KBO
# league at all - only MLB. Confirmed live: without this override, a KBO
# prop for a former-MLB player (e.g. Austin Dean, Sam Hilliard) silently
# matched that player's old MLB athlete record and "tracked" against the
# wrong team/game entirely - it never grades against real KBO stats, just
# times out later. Tagging it "kbo" here instead lets _auto_playerprops
# (bot.py) reject it honestly up front instead of mismatching.
_PROP_SPORT_OVERRIDE = {
    "wnba": "wnba",
    "kbo": "kbo",
}


def _strip_trailing_parens(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()


def _clean_line(line: str) -> str:
    text = line.strip()
    # Some sources copy-paste with an em/en dash instead of a plain hyphen
    # (confirmed live: "Matteo Berrettini vs Mariano Navone – Matteo
    # Berrettini ML") - every dash-anchored regex in this module expects a
    # literal "-", so normalizing here (the universal entry point every
    # parser below calls first) fixes all of them at once rather than
    # special-casing each one.
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[✅❌⭐️🔥💰]+\s*$", "", text).strip()
    while True:
        stripped = _strip_trailing_parens(text)
        if stripped == text:
            break
        text = stripped
    return text


def _match_stat_label(sport: str, raw_stat: str) -> Optional[str]:
    raw = raw_stat.strip().lower()
    default = _AMBIGUOUS_STAT_DEFAULTS.get((sport, raw))
    if default:
        return default
    catalog = espn.STAT_CATALOG.get(sport, {})
    for label in catalog:
        if label.lower() == raw:
            return label
    for label in catalog:
        base = label.lower().split(" (")[0]
        # Checked both directions - "raw in label" catches raw wording that's
        # a shorthand for a longer catalog label (e.g. "TB" in "Total Bases"),
        # "base in raw" catches the opposite: a prefixed/suffixed variant of
        # a plain catalog label (e.g. "Total Points" for catalog's "Points" -
        # confirmed live, a real "Allisha Gray Over 19.5 Total points" pick
        # silently fell through to being parsed as a team name instead, since
        # only the first direction was ever checked).
        if base == raw or raw in label.lower() or base in raw:
            return label
    return None


# "Breaks" (a player's own count of games broken on the opponent's serve)
# is the same underlying number 365scores reports as the leading X in
# "Break Points Won" ("X/Y (Z%)" - see scores365._parse_tennis_stat_value),
# just under a different name bettors commonly use - confirmed live, a real
# "Over 3.5 Breaks" pick didn't fuzzy-match "Break Points Won" via substring
# (neither word contains the other) and was silently rejected as an
# unsupported stat.
_TENNIS_STAT_ALIASES = {"breaks": "Break Points Won", "break": "Break Points Won"}


def _match_tennis_stat_label(raw_stat: str) -> Optional[str]:
    """Tennis-only equivalent of _match_stat_label - ESPN doesn't support
    tennis at all (see espn.py's module docstring), so tennis props are
    backed by scores365.TENNIS_STAT_CATALOG instead."""
    raw = raw_stat.strip().lower()
    if raw in _TENNIS_STAT_ALIASES:
        return _TENNIS_STAT_ALIASES[raw]
    catalog = scores365.TENNIS_STAT_CATALOG
    for label in catalog:
        if label.lower() == raw:
            return label
    for label in catalog:
        if raw in label.lower() or label.lower() in raw:
            return label
    return None


def _parse_tennis_player_prop(description: str) -> Optional[dict]:
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    player = re.sub(r"[\s-]+$", "", pm.group(1)).strip()
    direction = pm.group(2).lower()
    line = float(pm.group(3))
    stat_label = _match_tennis_stat_label(pm.group(4).strip())
    if not player or not stat_label:
        return None
    return {"kind": "tennis_playerprops", "player": player, "stat": stat_label, "direction": direction, "line": line}


def _match_soccer_stat_label(raw_stat: str) -> Optional[str]:
    """Soccer-only equivalent of _match_stat_label - ESPN doesn't support
    soccer at all (see espn.py's module docstring), so soccer props are
    backed by two catalogs combined: scores365.SOCCER_STAT_CATALOG (Goals/
    Assists/Yellow Cards/Red Cards) and playerstatsfootball.STAT_CATALOG
    (Shots/Shots on Target/Tackles/Fouls Committed/Fouls Drawn/Dispossessed/
    Offsides/Key Passes - see that module's docstring for why these needed
    a second source). Which catalog a label came from doesn't matter here -
    that's resolved later, at tracking time, by whichever of the two
    actually has the label (see soccerpropstracker._current_value)."""
    raw = raw_stat.strip().lower()
    labels = list(scores365.SOCCER_STAT_CATALOG) + list(playerstatsfootball.STAT_CATALOG)
    for label in labels:
        if label.lower() == raw:
            return label
    for label in labels:
        if raw in label.lower() or label.lower() in raw:
            return label
    return None


def _parse_soccer_player_prop(description: str) -> Optional[dict]:
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    player = re.sub(r"[\s-]+$", "", pm.group(1)).strip()
    direction = pm.group(2).lower()
    line = float(pm.group(3))
    stat_label = _match_soccer_stat_label(pm.group(4).strip())
    if not player or not stat_label:
        return None
    return {"kind": "soccer_playerprops", "player": player, "stat": stat_label, "direction": direction, "line": line}


# Partial-game qualifiers (settle on a sub-period, not the final score) -
# tracking the full game via /track would show a live/final score that
# doesn't reflect whether one of these actually won or lost, so they're
# skipped rather than mistakenly tracked as if they were a full-game pick.
_PARTIAL_GAME_MARKERS = (
    "first 5 innings", "first five innings", "1st 5 innings", "f5",
    "first 4 innings", "first four innings", "1st 4 innings", "f4",
    "first half", "1st half", "second half", "2nd half",
    "first quarter", "1st quarter",
)


# "Team A vs Team B - Team X ML" - a matchup followed by an explicit dash +
# named-team + ML/Moneyline trailer, used to disambiguate which side is
# actually picked (see _parse_team_pick below, which previously ignored
# this entirely and always defaulted to the first-listed team - confirmed
# live a real "Daniel Vallejo vs Juncheng Shang - Juncheng Shang ML" pick
# would have silently tracked Vallejo, the WRONG side, since Shang is the
# second-listed team here).
_MATCHUP_ML_TRAILER_RE = re.compile(r"^(.+?)\s*-\s*(.+?)\s*(?:Sets\s+)?(?:ML|Moneyline)\s*$", re.IGNORECASE)


def _parse_team_pick(description: str) -> Optional[str]:
    text = _clean_line(description)
    lowered = text.lower()
    if any(marker in lowered for marker in _PARTIAL_GAME_MARKERS):
        return None
    for sep in (" vs. ", " vs ", " v. ", " v "):
        if sep in text:
            team_a, rest = (p.strip() for p in text.split(sep, 1))
            m = _MATCHUP_ML_TRAILER_RE.match(rest)
            if m:
                team_b, named = m.group(1).strip(), m.group(2).strip()
                if scores365.names_match(named, team_a):
                    return team_a
                if scores365.names_match(named, team_b):
                    return team_b
                return None  # explicit pick doesn't match either side - don't guess
            return team_a
    # "Sets Moneyline"/"Sets ML" checked before the bare "Moneyline"/"ML"
    # cutwords - same match-winner bet, just this tipster's own wording
    # (tennis matches are decided by sets) - confirmed live. Cutting at
    # "Moneyline" alone would leave "Sets" attached to the name.
    for cutword in ("Sets Moneyline", "Sets ML", "Moneyline", "ML", " Over ", " Under "):
        idx = text.find(cutword)
        if idx > 0:
            text = text[:idx].strip()
    return text or None


# Words that show up in prop/spread-style bets, not a plain moneyline pick -
# a bare bullet line containing any of these (or a digit) is skipped rather
# than guessed at, since there's no clear separator to isolate just the name.
_PROP_REJECT_WORDS = {
    "over", "under", "winner", "set", "sets", "breaks", "total", "totals",
    "spread", "runs", "points", "goals", "rounds", "games",
}


def _bullet_strip(line: str) -> str:
    # Strips an optional leading list marker - either a bullet char or a
    # "1." / "2)" numbering (confirmed live: numbered-but-unbracketed lines
    # like "1. Aces Moneyline (Fanatics -557)" left the "1." in place, which
    # then tripped _is_simple_pick_name's digit-rejection check meant for
    # actual prop bets, not list numbering).
    return re.sub(r"^(?:\d+[.)]\s*)?[•\-\*]?\s*", "", line).strip()


def _is_simple_pick_name(text: str) -> Optional[str]:
    """A bare 'Name', 'Name ML', or 'Name Sets Moneyline' with nothing else
    attached - rejects anything with digits or prop-betting keywords, which
    signal a more complex bet we can't safely reduce to just a name.

    "Sets Moneyline" (confirmed live, a real tennis slate used this wording
    throughout) means exactly the same match-winner bet as a plain
    "Moneyline"/"ML" - just this tipster's own label, since a tennis match is
    decided by sets. Stripped as one unit together with Moneyline/ML so
    "Sets" isn't left behind to trip the _PROP_REJECT_WORDS check below."""
    stripped = re.sub(r"\b(?:Sets\s+)?(?:ML|Moneyline)\b\s*$", "", text, flags=re.IGNORECASE).strip()
    if not stripped or re.search(r"\d", stripped):
        return None
    if any(word.lower() in _PROP_REJECT_WORDS for word in stripped.split()):
        return None
    if not re.match(r"^[A-Za-z.'\- ]+$", stripped):
        return None
    return stripped


# Used only when a bare line has no sport context at all (no [Category] tag,
# no preceding header) - the stat wording itself can still safely identify
# the sport when it's unique to exactly one sport's catalog (e.g. "Earned
# Runs" only exists for baseball). A stat that exists in more than one
# sport's catalog (e.g. "Hits" in both baseball and hockey, "Assists" in
# both basketball and hockey) still can't be guessed and returns None, same
# as any other unparseable line. "wnba" is excluded from the candidate set
# since it's a literal alias of "basketball" here (identical catalog dict) -
# inferring NBA vs WNBA from stat wording alone isn't possible, so an
# inferred basketball prop defaults to the NBA player pool, same as any
# other header-less basketball pick.
_INFER_SPORT_CANDIDATES = [s for s in espn.STAT_CATALOG if s != "wnba"]

# Stat words that are only in one ESPN-supported sport's catalog, but are
# actually the dominant real-world wording for a DIFFERENT, unsupported
# sport (see espn.UNSUPPORTED_SPORTS) - "Goals" is hockey's only in this
# catalog, but overwhelmingly means soccer in a real picks channel (soccer
# isn't ESPN-prop-supported at all). Confirmed live: a real "Lionel Messi
# Goals" pick got silently misrouted to a hockey player search instead of
# being recognized as unparseable. Excluded from inference entirely rather
# than guessing wrong - a genuine bare hockey goals prop still works fine
# with an explicit "[Hockey]"/"[NHL]" tag.
_UNINFERABLE_STATS = {"goals"}


def _infer_sport_from_stat(description: str) -> Optional[str]:
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    raw_stat = pm.group(4).strip()
    if raw_stat.lower() in _UNINFERABLE_STATS:
        return None
    matches = {sport for sport in _INFER_SPORT_CANDIDATES if _match_stat_label(sport, raw_stat)}
    return matches.pop() if len(matches) == 1 else None


def _looks_like_misfiled_prop(sport: str, description: str) -> bool:
    """A player-prop-shaped line ("Name Over/Under N <word(s)>") whose
    trailing word is a real stat name for a DIFFERENT sport - checked only
    once _parse_player_prop has already failed to match it under the
    section's own (stated) sport, so this specifically catches the pick
    having been posted under the WRONG section header, not just an
    unsupported stat in general. Strong evidence it's a misfiled prop, not
    a literal team-total bet on a "team" that happens to be named after a
    person - confirmed live, a real "Yoshi Yamamoto Over 4.5 Strikeouts"
    pick (an MLB pitcher prop) posted under a "WNBA" header fell through to
    _parse_bare_team_total_pick, which silently treated "Yoshi Yamamoto" as
    a basketball team name and queued it forever searching for a
    basketball team that will never exist. Deliberately narrower than
    _infer_sport_from_stat's single-match requirement above (that's for
    guessing a sport with NO header at all, so it stays conservative) -
    here the goal is only to recognize "this doesn't belong under this
    header," which just needs the stat to be real somewhere else, not
    uniquely identify where."""
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return False
    raw_stat = pm.group(4).strip()
    return any(other != sport and _match_stat_label(other, raw_stat) for other in _INFER_SPORT_CANDIDATES)


def _parse_player_prop(sport_key: str, sport: str, description: str) -> Optional[dict]:
    if sport not in espn.SPORT_PATHS:
        return None
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    # Trailing " -" separator sometimes used before "Over/Under" (e.g. "Elly
    # De La Cruz - Over 1.5 Total Bases") stays attached to group(1) since the
    # regex only anchors on whitespace before Over/Under, not the dash itself.
    player = re.sub(r"[\s-]+$", "", pm.group(1)).strip()
    direction = pm.group(2).lower()
    line = float(pm.group(3))
    raw_stat = pm.group(4).strip()
    # Matched against the real underlying sport's catalog (e.g. "baseball"
    # for a KBO prop) - only the final tagged sport below uses the override,
    # so a stat like "Strikeouts (Pitching)" still resolves correctly even
    # for a sport ESPN itself has no dedicated catalog entry for.
    stat_label = _match_stat_label(sport, raw_stat)
    prop_sport = _PROP_SPORT_OVERRIDE.get(sport_key, sport)
    if not player or not stat_label:
        return None
    return {
        "kind": "playerprops", "sport": prop_sport, "player": player, "stat": stat_label,
        "direction": direction, "line": line,
    }


def _parse_combo_stat_prop(sport_key: str, sport: str, description: str) -> Optional[dict]:
    """Basketball-only "P+R+A"/"PRA" props, worded stat-BEFORE-Over unlike
    every other prop here - see _COMBO_STAT_BEFORE_RE."""
    if sport not in ("basketball", "wnba"):
        return None
    m = _COMBO_STAT_BEFORE_RE.match(description)
    if not m:
        return None
    player = re.sub(r"[\s-]+$", "", m.group(1)).strip()
    direction = m.group(3).lower()
    line = float(m.group(4))
    prop_sport = _PROP_SPORT_OVERRIDE.get(sport_key, sport)
    if not player:
        return None
    return {
        "kind": "playerprops", "sport": prop_sport, "player": player,
        "stat": "Points + Rebounds + Assists", "direction": direction, "line": line,
    }


def _is_yrfi_header(text: str) -> bool:
    lowered = text.lower()
    return "yrfi" in lowered and "nrfi" in lowered


def _parse_yrfi_line(text: str) -> Optional[dict]:
    cleaned = _clean_line(text)
    m = _YRFI_LINE_RE.match(cleaned)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {"kind": "inning_runs", "sport": "baseball", "team": team, "pick_type": m.group(2).upper()}


def _parse_inning_run_total(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _INNING_RUN_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    pick_type = "YRFI" if m.group(3).lower() == "over" else "NRFI"
    return {"kind": "inning_runs", "sport": "baseball", "team": team, "pick_type": pick_type}


def _parse_inning1_team_pick(description: str) -> Optional[str]:
    text = _clean_line(description)
    if not _INNING1_RESULT_MARKER_RE.search(text):
        return None
    if "moneyline" not in text.lower() and not re.search(r"\bml\b", text, re.IGNORECASE):
        return None
    text = _INNING1_RESULT_MARKER_RE.sub("", text)
    return _parse_team_pick(text)


def _parse_set1_pick(description: str) -> Optional[str]:
    text = _clean_line(description)
    if not _SET1_MARKER_RE.search(text):
        return None
    text = _SET1_MARKER_RE.sub("", text)
    # The originally-supported phrasings (e.g. "Naomi Osaka to win 1st Set")
    # never had a dash separator before the marker, but "Player - To Win Set
    # 1" does - strip whatever trailing " -" is left over, same as
    # _parse_tennis_player_prop/_parse_soccer_player_prop already do for
    # their own dash-prefixed player name.
    text = re.sub(r"[\s-]+$", "", text)
    return _parse_team_pick(text)


def _parse_set1_total_games_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _SET1_TOTAL_GAMES_RE.match(text) or _SET1_TOTAL_GAMES_AFTER_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()  # either side - just used to look the match up
    if not team:
        return None
    return {
        "kind": "tennis_set1_total_games", "team": team,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_player_total_games_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _PLAYER_TOTAL_GAMES_BEFORE_RE.match(text) or _PLAYER_TOTAL_GAMES_AFTER_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None  # doesn't look like either matchup side - let the combined-total parser try instead
    return {
        "kind": "tennis_player_total_games", "team": named,
        "direction": m.group(4).lower(), "line": float(m.group(5)),
    }


def _parse_player_total_games_nomatchup_pick(description: str) -> Optional[dict]:
    """No-matchup counterpart to _parse_player_total_games_pick - same
    market, "Player - Over/Under N Games" with no opponent named at all."""
    text = _clean_line(description)
    m = _PLAYER_TOTAL_GAMES_NOMATCHUP_BEFORE_RE.match(text) or _PLAYER_TOTAL_GAMES_NOMATCHUP_AFTER_RE.match(text)
    if not m:
        return None
    player = m.group(1).strip()
    if not player:
        return None
    return {"kind": "tennis_player_total_games", "team": player, "direction": m.group(2).lower(), "line": float(m.group(3))}


def _parse_games_handicap_nomatchup_pick(description: str) -> Optional[dict]:
    """"Player -2.5 Games" - a games-margin handicap, no opponent named at
    all (see _GAMES_HANDICAP_NOMATCHUP_RE)."""
    text = _clean_line(description)
    m = _GAMES_HANDICAP_NOMATCHUP_RE.match(text)
    if not m:
        return None
    player = m.group(1).strip()
    if not player:
        return None
    return {"kind": "tennis_games_handicap", "team": player, "line": float(m.group(2))}


def _parse_sets_handicap_nomatchup_pick(description: str) -> Optional[dict]:
    """"Player +1.5 Sets" - a sets-margin handicap, no opponent named at
    all (see _SETS_HANDICAP_NOMATCHUP_RE)."""
    text = _clean_line(description)
    m = _SETS_HANDICAP_NOMATCHUP_RE.match(text)
    if not m:
        return None
    player = m.group(1).strip()
    if not player:
        return None
    return {"kind": "tennis_sets_handicap", "team": player, "line": float(m.group(2))}


def _parse_win_a_set_nomatchup_pick(description: str) -> Optional[dict]:
    """"Player to Win a Set" / "Player to Win at Least 1 Set" - the same
    market as _parse_win_a_set_pick, just with no opponent named at all
    (see _WIN_A_SET_NOMATCHUP_RE)."""
    text = _clean_line(description)
    m = _WIN_A_SET_NOMATCHUP_RE.match(text)
    if not m:
        return None
    named = m.group(1).strip()
    direction = "yes"
    # Same leading/trailing "No"/"Not" flip as _parse_win_a_set_pick.
    stripped = re.sub(r"^(?:not|no)\s+|\s+(?:not|no)$", "", named, flags=re.IGNORECASE).strip()
    if stripped != named:
        direction = "no"
        named = stripped
    if not named:
        return None
    return {"kind": "tennis_win_a_set", "team": named, "direction": direction}


def _parse_match_total_games_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _MATCH_TOTAL_GAMES_BEFORE_RE.match(text) or _MATCH_TOTAL_GAMES_AFTER_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()  # either side - just used to look the match up
    if not team:
        return None
    return {
        "kind": "tennis_match_total_games", "team": team,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_win_a_set_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _WIN_A_SET_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    direction = "yes"
    # A leading or trailing "No"/"Not" right against the player's name flips
    # the pick - e.g. "Venus Williams No to Win a Set" or "Not Venus
    # Williams to Win a Set".
    stripped = re.sub(r"^(?:not|no)\s+|\s+(?:not|no)$", "", named, flags=re.IGNORECASE).strip()
    if stripped != named:
        direction = "no"
        named = stripped
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None  # doesn't look like either matchup side - don't guess
    return {"kind": "tennis_win_a_set", "team": named, "direction": direction}


def _parse_inning1_draw_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _INNING1_DRAW_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {"kind": "inning1_result", "sport": "baseball", "team": team, "pick": "DRAW"}


def _parse_f5_pick(description: str) -> Optional[str]:
    """The moneyline flavor of an F5 bet specifically - see
    _parse_f5_total_pick for the team-total flavor, which is tried first in
    _parse_description since it has its own more specific shape."""
    text = _clean_line(description)
    if not _F5_MARKER_RE.search(text):
        return None
    if "moneyline" not in text.lower() and not re.search(r"\bml\b", text, re.IGNORECASE):
        return None  # not the moneyline flavor - handled elsewhere or not graded at all
    text = _F5_MARKER_RE.sub("", text)
    return _parse_team_pick(text)


def _parse_f5_total_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _F5_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {
        "kind": "f5_total", "sport": sport, "team": team,
        "direction": m.group(2).lower(), "line": float(m.group(3)),
    }


def _parse_f5_combined_total_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _F5_COMBINED_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()  # either team - just used to look the game up
    if not team:
        return None
    return {
        "kind": "f5_combined_total", "sport": sport, "team": team,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_f5_handicap_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _F5_HANDICAP_RE.match(text)
    if not m:
        return None
    team_a, team_b, named_team = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named_team:
        return None
    if not (scores365.names_match(named_team, team_a) or scores365.names_match(named_team, team_b)):
        return None  # doesn't look like either matchup side - don't guess
    return {
        "kind": "f5_handicap", "sport": sport, "team": named_team, "line": float(m.group(4)),
    }


def _parse_1h_total_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _1H_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {
        "kind": "1h_total", "sport": sport, "team": team,
        "direction": m.group(2).lower(), "line": float(m.group(3)),
    }


def _parse_1h_combined_total_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _1H_COMBINED_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()  # either team - just used to look the game up
    if not team:
        return None
    return {
        "kind": "1h_combined_total", "sport": sport, "team": team,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


# "Dakota Ditcheva by KO/TKO KO/TKO Method of Victory" - method of victory
# (KO/TKO, submission, decision) has no reliable field anywhere in ESPN's
# public MMA data (see espn_ufc.py's module docstring), so it's deliberately
# unsupported. Checked explicitly in the mma branch below so this wording
# doesn't fall through to the moneyline fallback there, which would otherwise
# treat the leftover "by KO/TKO ... Method of Victory" text itself as a
# garbled fighter name and attempt (and fail) a bogus bout lookup.
_METHOD_OF_VICTORY_RE = re.compile(r"\bmethod\s+of\s+victory\b|\bby\s+(?:ko/?tko|ko|tko|submission|decision)\b", re.IGNORECASE)


def _parse_ufc_round_total_pick(description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _UFC_ROUND_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()  # either fighter - just used to look the bout up
    if not team:
        return None
    return {
        "kind": "ufc_round_total", "team": team,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


# Dota 2 / CS2 esports markets (see esports.py/esportstracker.py). Unlike
# every other sport in this module, hawk.live/GosuGamers can only resolve a
# match by BOTH team names together - there's no "find any match for this
# one team" lookup the way 365scores/ESPN support - so every one of these
# six requires an explicit "Team A vs Team B" matchup; a bare single-name
# pick (no opponent) genuinely can't be resolved and isn't supported here.

# "Team A vs Team B - Team X (-1.5) Map Handicap" / "... - Team X -1.5 Map
# Handicap" (parens optional either way).
_ESPORTS_MAP_HANDICAP_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s*\(?([+-]\d+(?:\.\d+)?)\)?\s*map\s*handicap\b",
    re.IGNORECASE,
)

# "Team A vs Team B - Total Maps Over 2.5" (marker before the number) / "...
# - Over 2.5 Total Maps" or "... Over 2.5 Maps" (marker after) - same
# before/after split as tennis's total-games markers elsewhere in this file.
_ESPORTS_TOTAL_MAPS_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(?:total\s+)?maps\s+(Over|Under)\s+([\d.]+)", re.IGNORECASE,
)
_ESPORTS_TOTAL_MAPS_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-?\s*(Over|Under)\s+([\d.]+)\s*(?:total\s+)?maps\b", re.IGNORECASE,
)

# "Team A vs Team B - Team X Map 2 Winner" - the individual-map flavor, not
# the overall series (see _parse_esports_match_winner below for that).
_ESPORTS_MAP_WINNER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+map\s*(\d+)\s+winner\b", re.IGNORECASE,
)

# "Team A vs Team B - Team X ML and Map 2 Winner" / "... Match Winner / Map
# 2 Winner" - a correlated same-game combo requiring BOTH conditions (see
# esports.grade_match_and_map_winner), distinct from either market alone.
# Checked before _ESPORTS_MAP_WINNER_RE - without this, that regex's own
# non-greedy team-name group would otherwise swallow the "ML and" part as
# if it were part of the team name and still fuzzy-match, silently
# misparsing this as a plain Map N Winner pick.
_ESPORTS_MATCH_AND_MAP_WINNER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(?:ml|moneyline|match\s+winner)\s*(?:and|\+|/|&)\s*map\s*(\d+)\s+winner\b",
    re.IGNORECASE,
)

# "Team A vs Team B - Team X to Win at Least One Map" / "... - Team X Wins
# at Least One Map" - whether the named team avoids being swept, not who
# wins the series outright.
_ESPORTS_WIN_AT_LEAST_ONE_MAP_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(?:to\s+win|wins?)\s+(?:at\s+least\s+)?(?:one|1)\s+maps?\b",
    re.IGNORECASE,
)

# "Team A vs Team B - Team X to Win 2-0" - the exact series score. Requires
# the explicit "to win" phrasing (rather than a bare trailing "N-N") since
# without it there's no reliable marker distinguishing this from arbitrary
# trailing text.
_ESPORTS_CORRECT_SCORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+to\s+win\s+(\d+)\s*-\s*(\d+)\b",
    re.IGNORECASE,
)

# "Team A vs Team B - Total Kills Over 112.5" (marker before) / "... - Over
# 112.5 Total Kills" (marker after) - combined kills across every map played
# in the series, Dota 2 only (see esports.grade_total_kills - CS2 has no
# kill data anywhere).
_ESPORTS_TOTAL_KILLS_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(?:total\s+)?kills\s+(Over|Under)\s+([\d.]+)", re.IGNORECASE,
)
_ESPORTS_TOTAL_KILLS_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-?\s*(Over|Under)\s+([\d.]+)\s*(?:total\s+)?kills\b", re.IGNORECASE,
)

# "Team A vs Team B - Team X Over 60.5 Total Kills" - one named team's own
# kill total across every map played, not the combined series total above.
# The named team between the dash and Over/Under is what distinguishes this
# from the combined-total shape (which has no name there at all) - checked
# first, same convention as tennis's _PLAYER_TOTAL_GAMES_BEFORE/AFTER_RE.
_ESPORTS_TEAM_TOTAL_KILLS_BEFORE_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(?:total\s+)?kills\s+(Over|Under)\s+([\d.]+)", re.IGNORECASE,
)
_ESPORTS_TEAM_TOTAL_KILLS_AFTER_RE = re.compile(
    r"^(.+?)\s*(?:@|\bvs\.?|\bv\.?)\s*(.+?)\s*-\s*(.+?)\s+(Over|Under)\s+([\d.]+)\s*(?:total\s+)?kills\b", re.IGNORECASE,
)


def _parse_esports_map_handicap(text: str) -> Optional[dict]:
    m = _ESPORTS_MAP_HANDICAP_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None  # doesn't look like either matchup side - don't guess
    return {"kind": "esports_map_handicap", "team_a": team_a, "team_b": team_b, "team": named, "line": float(m.group(4))}


def _parse_esports_total_maps(text: str) -> Optional[dict]:
    m = _ESPORTS_TOTAL_MAPS_BEFORE_RE.match(text) or _ESPORTS_TOTAL_MAPS_AFTER_RE.match(text)
    if not m:
        return None
    team_a, team_b = m.group(1).strip(), m.group(2).strip()
    if not team_a or not team_b:
        return None
    return {
        "kind": "esports_total_maps", "team_a": team_a, "team_b": team_b,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_esports_map_winner(text: str) -> Optional[dict]:
    m = _ESPORTS_MAP_WINNER_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None
    return {
        "kind": "esports_map_winner", "team_a": team_a, "team_b": team_b,
        "team": named, "map_number": int(m.group(4)),
    }


def _parse_esports_match_and_map_winner(text: str) -> Optional[dict]:
    m = _ESPORTS_MATCH_AND_MAP_WINNER_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None
    return {
        "kind": "esports_match_and_map_winner", "team_a": team_a, "team_b": team_b,
        "team": named, "map_number": int(m.group(4)),
    }


def _parse_esports_win_at_least_one_map(text: str) -> Optional[dict]:
    m = _ESPORTS_WIN_AT_LEAST_ONE_MAP_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    direction = "yes"
    # A leading/trailing "No"/"Not" right against the team's name flips the
    # pick to "does NOT win at least one map" (swept) - e.g. "LGD Gaming No
    # to Win at Least One Map" / "Not LGD Gaming to Win at Least One Map" -
    # same convention as tennis's win_a_set market elsewhere in this file.
    stripped = re.sub(r"^(?:not|no)\s+|\s+(?:not|no)$", "", named, flags=re.IGNORECASE).strip()
    if stripped != named:
        direction = "no"
        named = stripped
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None
    return {"kind": "esports_win_at_least_one_map", "team_a": team_a, "team_b": team_b, "team": named, "direction": direction}


def _parse_esports_correct_score(text: str) -> Optional[dict]:
    m = _ESPORTS_CORRECT_SCORE_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None
    return {
        "kind": "esports_correct_score", "team_a": team_a, "team_b": team_b, "team": named,
        "picked_maps": int(m.group(4)), "other_maps": int(m.group(5)),
    }


def _parse_esports_team_total_kills(text: str) -> Optional[dict]:
    m = _ESPORTS_TEAM_TOTAL_KILLS_BEFORE_RE.match(text) or _ESPORTS_TEAM_TOTAL_KILLS_AFTER_RE.match(text)
    if not m:
        return None
    team_a, team_b, named = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named:
        return None
    if not (scores365.names_match(named, team_a) or scores365.names_match(named, team_b)):
        return None  # doesn't look like either matchup side - let the combined-total parser try instead
    return {
        "kind": "esports_team_total_kills", "team_a": team_a, "team_b": team_b, "team": named,
        "direction": m.group(4).lower(), "line": float(m.group(5)),
    }


def _parse_esports_total_kills(text: str) -> Optional[dict]:
    m = _ESPORTS_TOTAL_KILLS_BEFORE_RE.match(text) or _ESPORTS_TOTAL_KILLS_AFTER_RE.match(text)
    if not m:
        return None
    team_a, team_b = m.group(1).strip(), m.group(2).strip()
    if not team_a or not team_b:
        return None
    return {
        "kind": "esports_total_kills", "team_a": team_a, "team_b": team_b,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_esports_match_winner(text: str) -> Optional[dict]:
    """Series (overall) moneyline - same "- Team X ML" trailer convention
    already used for every other sport's moneyline in this module. No bare
    single-team fallback (unlike _parse_team_pick) - a name with no
    opponent can't be resolved against hawk.live/GosuGamers at all."""
    for sep in (" vs. ", " vs ", " v. ", " v "):
        if sep not in text:
            continue
        team_a, rest = (p.strip() for p in text.split(sep, 1))
        m = _MATCHUP_ML_TRAILER_RE.match(rest)
        if not m:
            return None
        team_b, named = m.group(1).strip(), m.group(2).strip()
        if scores365.names_match(named, team_a):
            return {"kind": "esports_match_winner", "team_a": team_a, "team_b": team_b, "team": team_a}
        if scores365.names_match(named, team_b):
            return {"kind": "esports_match_winner", "team_a": team_a, "team_b": team_b, "team": team_b}
        return None  # explicit pick doesn't match either side - don't guess
    return None


def _parse_esports_pick(description: str, sport: str) -> Optional[dict]:
    text = _clean_line(description)
    parsers = [
        _parse_esports_map_handicap, _parse_esports_total_maps,
        _parse_esports_match_and_map_winner, _parse_esports_map_winner,
        _parse_esports_win_at_least_one_map, _parse_esports_correct_score, _parse_esports_match_winner,
    ]
    if sport == "dota2":
        # Kill totals are Dota 2-only - CS2 has no kill data anywhere (see
        # esports.live_kill_count's own docstring) - so a "Total Kills"
        # phrasing on a CS2 pick is left unrecognized rather than tracked
        # as a market that could never actually grade. team_total_kills is
        # checked first, same "named team distinguishes from the combined
        # shape" ordering used for every other sport's own team-vs-combined
        # total pair in this module.
        parsers = [_parse_esports_team_total_kills, _parse_esports_total_kills] + parsers
    for parser in parsers:
        pick = parser(text)
        if pick:
            pick["sport"] = sport
            return pick
    return None


def _parse_total_pick(sport: str, description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _TOTAL_LINE_RE.match(text)
    if not m:
        return None
    team_a, team_b = m.group(1).strip(), m.group(2).strip()
    if not team_a or not team_b:
        return None
    return {
        "kind": "total", "sport": sport, "team": team_a,
        "direction": m.group(3).lower(), "line": float(m.group(4)),
    }


def _parse_named_team_total_pick(sport: str, description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _NAMED_TEAM_TOTAL_RE.match(text)
    if not m:
        return None
    team_a, team_b, named_team = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not team_a or not team_b or not named_team:
        return None
    # The regex's own capture is greedy-enough to sometimes pull in trailing
    # wording along with the team name (e.g. "Club America Team Total" for
    # "Austin FC vs Club America - Club America Team Total OVER 1.5 Goals") -
    # names_match's fuzzy comparison still validates it against team_a/
    # team_b correctly (the real team name is a big chunk of the match), but
    # returning the raw captured text as-is downstream would send that
    # trailing junk straight into the actual team search and fail there
    # instead. Returning whichever canonical side it matched keeps the team
    # name clean regardless of what got tacked onto the capture.
    if scores365.names_match(named_team, team_a):
        team = team_a
    elif scores365.names_match(named_team, team_b):
        team = team_b
    else:
        return None  # doesn't look like either matchup side - don't guess
    return {
        "kind": "team_total", "sport": sport, "team": team,
        "direction": m.group(4).lower(), "line": float(m.group(5)),
    }


def _parse_bare_team_total_pick(sport: str, description: str) -> Optional[dict]:
    text = _clean_line(description)
    m = _BARE_TEAM_TOTAL_RE.match(text)
    if not m:
        return None
    team = m.group(1).strip()
    if not team:
        return None
    return {
        "kind": "team_total", "sport": sport, "team": team,
        "direction": m.group(2).lower(), "line": float(m.group(3)),
    }


# "Team A vs Team B - <rest>" - strips a leading matchup used purely as
# context (see _parse_description's is_prop_category+has_matchup handling)
# so <rest> can be re-parsed on its own, e.g. as a player prop. Same
# separator set as _TOTAL_LINE_RE and friends.
_MATCHUP_PREFIX_RE = re.compile(r"^.+?\s*(?:@|\bvs\.?\b|\bv\.?\b)\s*.+?\s*-\s*(.+)$", re.IGNORECASE)


def _strip_matchup_prefix(description: str) -> Optional[str]:
    m = _MATCHUP_PREFIX_RE.match(description)
    return m.group(1).strip() if m else None


def _parse_description(sport: str, sport_key: str, description: str, is_prop_category: bool) -> Optional[dict]:
    if sport in ("dota2", "cs2"):
        # None of the generic total/player-prop/track fallback logic below
        # applies to esports at all (see _parse_esports_pick's own
        # docstring) - returns unconditionally, same as mma's own
        # dedicated block at the bottom of this function.
        return _parse_esports_pick(description, sport)

    if sport == "baseball":
        inning_total = _parse_inning_run_total(description)
        if inning_total:
            return inning_total

        # Literal "YRFI"/"NRFI" wording on a matchup line (e.g. "Team A vs
        # Team B - NRFI - No Runs 1st Inning") - previously only recognized
        # under a dedicated "[YRFI/NRFI]"-tagged section (see
        # _is_yrfi_header), so the same wording under a plain "Baseball"
        # header fell through, got mistaken for a team pick, then rejected
        # by _is_simple_pick_name for containing a digit ("1st") - confirmed
        # live, a real "New York Mets vs Miami Marlins - NRFI - No Runs 1st
        # Inning" pick silently never posted.
        yrfi_inline = _parse_yrfi_line(description)
        if yrfi_inline:
            return yrfi_inline

        inning1_draw = _parse_inning1_draw_pick(description)
        if inning1_draw:
            return inning1_draw

        f5_combined = _parse_f5_combined_total_pick(description, sport)
        if f5_combined:
            return f5_combined

        f5_handicap = _parse_f5_handicap_pick(description, sport)
        if f5_handicap:
            return f5_handicap

    if sport in ("nfl", "basketball"):
        spread = _parse_team_spread_nomatchup_pick(sport, description)
        if spread:
            return spread

    if sport == "nfl":
        onehalf_combined = _parse_1h_combined_total_pick(description, sport)
        if onehalf_combined:
            return onehalf_combined

    if sport == "mma":
        ufc_round_total = _parse_ufc_round_total_pick(description)
        if ufc_round_total:
            return ufc_round_total

    if sport == "tennis":
        win_a_set = _parse_win_a_set_pick(description)
        if win_a_set:
            return win_a_set

        # Checked before Match Total Games - "1st Set Total Games" is the
        # more specific shape, same reasoning as F5's combined total vs.
        # baseball's generic total elsewhere in this function.
        set1_total_games = _parse_set1_total_games_pick(description)
        if set1_total_games:
            return set1_total_games

        # Checked before the combined-match version - a named player right
        # before Over/Under is what distinguishes "that player's own games
        # won" from "both sides' games summed" (see _PLAYER_TOTAL_GAMES_...
        # above).
        player_total_games = _parse_player_total_games_pick(description)
        if player_total_games:
            return player_total_games

        match_total_games = _parse_match_total_games_pick(description)
        if match_total_games:
            return match_total_games

    named_total = _parse_named_team_total_pick(sport, description)
    if named_total:
        return named_total

    # A description shaped like "Player Over/Under N Stat" is a player prop
    # even when the category itself is bare (e.g. "[MLB]" rather than
    # "[MLB Props]") - confirmed live that real messages mix both taggings
    # for the same bet type. A team-vs-team matchup line (e.g. "Angels vs
    # Giants - Over 8.5 Total Runs") is excluded even though it also matches
    # the Over/Under shape, since that's a game total (handled above), not a
    # single player's stat.
    has_matchup = any(sep in description for sep in (" vs. ", " vs ", " v. ", " v ", " @ "))

    # Skipped entirely for an explicitly-tagged prop category - _TOTAL_LINE_RE
    # doesn't validate its "team_b" capture at all (unlike named_total above),
    # so on a matchup-prefixed prop line (see the is_prop_category+has_matchup
    # block below) it would otherwise greedily swallow everything up to the
    # first Over/Under - including the actual player's name - as if it were
    # part of a combined game total. Confirmed live: "[MLB Props] Colorado
    # Rockies vs Arizona Diamondbacks - Corbin Carroll Over 0.5 Total Bases"
    # silently mistracked as "did Colorado Rockies+Diamondbacks combined
    # score over 0.5" - a nonsense, guaranteed-to-grade bet - instead of
    # Corbin Carroll's actual prop.
    if not is_prop_category:
        total = _parse_total_pick(sport, description)
        if total:
            return total

    if is_prop_category and has_matchup:
        # A matchup prefix ("Team A vs Team B - ") is sometimes used purely
        # for game context ahead of a player prop, not to describe a team/
        # game total - named_total above already rejects this shape (the
        # player's name doesn't match either matchup side), and the
        # has_matchup guard right below skips player-prop parsing entirely,
        # so re-parsing just the part after the matchup dash recovers the
        # intended prop instead of guessing or dropping the whole pick.
        remainder = _strip_matchup_prefix(description)
        if remainder:
            combo_prop = _parse_combo_stat_prop(sport_key, sport, remainder)
            if combo_prop:
                return combo_prop
            prop = _parse_player_prop(sport_key, sport, remainder)
            if prop:
                return prop

    if not has_matchup:
        f5_total = _parse_f5_total_pick(description, sport)
        if f5_total:
            return f5_total

        f5_team = _parse_f5_pick(description)
        if f5_team:
            return {"kind": "f5_moneyline", "sport": sport, "team": f5_team}

        if sport == "nfl":
            onehalf_total = _parse_1h_total_pick(description, sport)
            if onehalf_total:
                return onehalf_total

        if sport == "baseball":
            inning1_team = _parse_inning1_team_pick(description)
            if inning1_team:
                return {"kind": "inning1_result", "sport": sport, "team": inning1_team, "pick": inning1_team}

        if sport == "tennis":
            set1_team = _parse_set1_pick(description)
            if set1_team:
                return {"kind": "set1_moneyline", "sport": sport, "team": set1_team}

            player_total_games_nomatchup = _parse_player_total_games_nomatchup_pick(description)
            if player_total_games_nomatchup:
                return player_total_games_nomatchup

            games_handicap_nomatchup = _parse_games_handicap_nomatchup_pick(description)
            if games_handicap_nomatchup:
                return games_handicap_nomatchup

            sets_handicap_nomatchup = _parse_sets_handicap_nomatchup_pick(description)
            if sets_handicap_nomatchup:
                return sets_handicap_nomatchup

            win_a_set_nomatchup = _parse_win_a_set_nomatchup_pick(description)
            if win_a_set_nomatchup:
                return win_a_set_nomatchup

            tennis_prop = _parse_tennis_player_prop(description)
            if tennis_prop:
                return tennis_prop
            if _PLAYER_STAT_RE.match(description):
                # Player-prop-shaped but an unrecognized/unsupported stat
                # (e.g. "Winners", never tracked here - see
                # tennispropstracker.py's module docstring) - don't let this
                # fall through to _parse_player_prop (always fails for
                # tennis - ESPN doesn't support it at all) and then the
                # generic team-pick fallback below, which would otherwise
                # misparse the player's own name as if it were a team.
                return None

        if sport == "soccer":
            soccer_prop = _parse_soccer_player_prop(description)
            if soccer_prop:
                return soccer_prop
            if _PLAYER_STAT_RE.match(description):
                # Same reasoning as tennis above - e.g. "Shots"/"Passes"
                # aren't supported (see scores365.SOCCER_STAT_CATALOG).
                return None

        combo_prop = _parse_combo_stat_prop(sport_key, sport, description)
        if combo_prop:
            return combo_prop

        prop = _parse_player_prop(sport_key, sport, description)
        if prop:
            return prop

        if not is_prop_category:
            if _looks_like_misfiled_prop(sport, description):
                return None
            bare_total = _parse_bare_team_total_pick(sport, description)
            if bare_total:
                return bare_total

    # mma is exempt: this source tags every combat pick "Combat Props"
    # regardless of bet type (confirmed live - a plain fighter moneyline
    # came tagged that way, not just stat props), and ESPN has no
    # player-prop lookup for mma at all (see espn.SPORT_PATHS) so
    # _parse_player_prop above always fails here - bailing out on
    # is_prop_category would wrongly swallow every mma moneyline/round-
    # total pick before reaching mma's own authoritative fallback below,
    # which already guards against genuinely-unparseable wording
    # (Method of Victory) rather than blindly guessing. Checked outside the
    # has_matchup block (not just inside it) - a Props-tagged matchup line
    # that didn't recover as a prop above (see is_prop_category+has_matchup
    # block) shouldn't fall through to the generic team-pick guess at the
    # bottom of this function either.
    if is_prop_category and sport not in ("mma", "boxing"):
        return None  # explicitly tagged as a prop but couldn't be confidently parsed - don't guess it's a team pick

    if sport == "mma":
        if _METHOD_OF_VICTORY_RE.search(description):
            return None
        # UFC isn't a 365scores sport at all (see espn_ufc.py), so - unlike
        # every other sport here - a failed match must NOT fall through to
        # the generic track fallback below, which is backed by
        # scores365.find_match_for_team and would just search every real
        # 365scores sport pointlessly for a fighter's name. Checked outside
        # the has_matchup block above (not just inside it) since a UFC
        # moneyline pick can itself be worded with a " vs " matchup (e.g.
        # "Fighter A vs Fighter B - Fighter A ML"), which would otherwise
        # skip straight past that block to the generic fallback below.
        ufc_team = _parse_team_pick(description)
        return {"kind": "ufc_moneyline", "team": ufc_team} if ufc_team else None

    if sport == "boxing":
        # Moneyline only (see boxing.py's own module docstring) - same
        # "don't fall through to the generic 365scores-backed track
        # fallback" reasoning as mma above, since BoxingScene isn't a
        # 365scores sport either. _parse_team_pick already handles both
        # "Fighter A vs Fighter B - Fighter A ML" and a bare "Fighter A ML"
        # with no opponent named.
        boxing_fighter = _parse_team_pick(description)
        return {"kind": "boxing_moneyline", "team": boxing_fighter} if boxing_fighter else None

    team = _parse_team_pick(description)
    if not team:
        return None
    return {"kind": "track", "sport": sport, "team": team}


def _parse_with_category(category: str, description: str) -> Optional[dict]:
    if _is_yrfi_header(category):
        return _parse_yrfi_line(description)

    is_prop_category = category.lower().endswith("props")
    sport_key = category.lower().replace("props", "").strip()
    sport = _SPORT_MAP.get(sport_key)
    if not sport:
        return None
    return _parse_description(sport, sport_key, description, is_prop_category)


def parse_pick_line(line: str) -> Optional[dict]:
    """
    Returns one of:
      {"kind": "track", "sport": ..., "team": ...}
      {"kind": "playerprops", "sport": ..., "player": ..., "stat": ...}
    or None if the line isn't a pick, references an unsupported sport, or
    can't be confidently parsed. Only handles lines with their own
    "[Category]" tag - see parse_picks_message for lines that inherit a
    category from an earlier line (e.g. the second side of a matchup)."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    category, description = m.group(1).strip(), m.group(2).strip()
    return _parse_with_category(category, description)


def parse_picks_message(content: str) -> list[dict]:
    """
    Parses every line of a message, skipping anything unparseable. Handles
    two styles:

    1. Bracket-tagged, one category per line:
         "1. [Tennis] Marcos Giron (Fanatics -1985)"
         "Ugo Humbert (Bet365 -995)"  <- inherits "Tennis" from the line above

    2. Bare section header followed by a bullet list, e.g.:
         "Tennis"
         "- Marcos Giron (Fanatics -1985)"
         "- Marcos Giron 0 Set 1 Winner (Fanatics -585)"  <- skipped, prop bet
         "- Jakub Mensik ML (DraftKings -583)"

    Bullet/untagged lines only ever track a plain "Name" or "Name ML" pick -
    anything with digits or prop-betting words (Over, Winner, Breaks, etc.)
    attached is skipped rather than guessed at, since there's no clear
    separator to isolate just the name from a more complex bet.
    """
    results = []
    current_category = None
    expanded_lines = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped:
            expanded_lines.extend(_split_merged_bracket_lines(stripped))
    for raw_line in expanded_lines:
        line = raw_line.strip()
        if not line:
            continue

        m = _LINE_RE.match(line)
        if m:
            current_category, description = m.group(1).strip(), m.group(2).strip()
            pick = _parse_with_category(current_category, description)
            if pick:
                pick["section"], pick["raw"] = current_category, description
                results.append(pick)
            continue

        bare = _bullet_strip(line)
        if _is_yrfi_header(bare):
            current_category = "__yrfi__"
            continue
        if bare.lower() in _HEADER_SPORT_MAP:
            current_category = bare
            continue
        # A purely cosmetic sub-label under an already-set sport header (e.g.
        # "NFL" then "Player props" before the actual prop lines) - confirmed
        # live: "Player props" fell through to the bare-name fallback below
        # and got tracked as a team literally named "Player props". Doesn't
        # touch current_category (unlike a real "<Sport> Props" header) since
        # the sport context needs to stay intact for the prop lines under it.
        if bare.lower() in ("player props", "player prop", "props"):
            continue

        if not current_category:
            # No header/tag anywhere above this line - last resort before
            # giving up: a player-prop-shaped line whose stat wording is
            # unique to exactly one sport can still be safely inferred (see
            # _infer_sport_from_stat). Anything else genuinely has no way to
            # know the sport, so it's skipped rather than guessed. A
            # team-vs-team matchup line is excluded even though it can still
            # match the same Over/Under shape (e.g. "LA Galaxy vs FC Dallas
            # Over 2.5 Goals") - that's a game total, not a player name, and
            # inferring a sport for a bare untagged total isn't safe the way
            # it is for a player prop (confirmed live: this was silently
            # misparsed as a "player" search for the literal matchup text).
            if not any(sep in bare for sep in (" vs. ", " vs ", " v. ", " v ")):
                sport = _infer_sport_from_stat(bare)
                if sport:
                    prop = _parse_player_prop(sport, sport, bare)
                    if prop:
                        # Every other append site in this loop tags
                        # section/raw from current_category - this is the
                        # one path where there's no literal header line to
                        # take it from (that's the whole reason inference was
                        # needed), but the sport was still confidently
                        # inferred, so a display label stands in for it.
                        # Confirmed live: without this, an inferred pick
                        # tracked normally but never made it into /summary
                        # (dailylog.record_pick no-ops on a missing section).
                        prop["section"] = espn.SPORT_DISPLAY_LABELS.get(sport, sport.upper())
                        prop["raw"] = bare
                        results.append(prop)
            continue

        # A bare line whose leading word(s) match a _HEADER_SPORT_MAP key
        # plus trailing text (e.g. "MLB Home Run Predictor") is a sub-header
        # still thematically under that sport, not a pick - confirmed live,
        # this previously fell through to being parsed as a literal
        # team-name search on the header text itself instead. Deliberately
        # narrow (checked before anything else, matched only against a
        # multi-word line) so it doesn't risk misclassifying a genuine bare
        # pick - real team/player names essentially never start with a bare
        # league abbreviation as their own separate word the way a
        # descriptive sub-header does.
        bare_for_header_check = _bullet_strip(line).lower()
        header_prefix_match = next(
            (k for k in _HEADER_SPORT_MAP if bare_for_header_check.startswith(k + " ")), None
        )
        if header_prefix_match:
            current_category = header_prefix_match
            continue

        if current_category.lower().endswith("props"):
            continue  # a Props section - bullets there aren't structured enough to parse safely

        if current_category == "__yrfi__":
            pick = _parse_yrfi_line(bare)
            if pick:
                # Every other append site in this loop tags section/raw
                # from current_category - this is the one other path
                # (besides the bare-inference fallback above) where that
                # doesn't work directly, since current_category was
                # overwritten to this sentinel rather than keeping the
                # literal header text. Confirmed live: without this, a
                # pick posted under a bare (non-bracket-tagged) "YRFI/NRFI
                # Slate"-style header tracked and graded fine but never
                # made it into /summary (dailylog.record_pick no-ops on a
                # missing section) - and separately, never had a "raw"
                # line to diff a later message edit against either.
                pick["section"], pick["raw"] = "YRFI/NRFI", bare
                results.append(pick)
            continue

        sport = _HEADER_SPORT_MAP.get(current_category.lower())
        if not sport:
            continue

        # Total/F5/prop picks have their own unambiguous shape (an explicit
        # "- Over N" or "F5"/"Moneyline" marker) that's safe to try even on a
        # bare bullet - but a plain "track" result from _parse_description
        # would also fire for basically any leftover text (it just strips
        # cutwords), which is too loose for an untagged bullet line, so that
        # case still falls through to the stricter _is_simple_pick_name check.
        pick = _parse_description(sport, current_category.lower(), _clean_line(bare), is_prop_category=False)
        if pick and pick["kind"] != "track":
            pick["section"], pick["raw"] = current_category, bare
            results.append(pick)
            continue

        # A full "Team A vs Team B[ - Team X ML]" matchup line is a
        # different, stronger structural signal than the "leftover text
        # after cutword-stripping" case the comment above worries about -
        # unambiguous and safe to trust even on an untagged bullet.
        # Confirmed live a real "Daniel Vallejo vs Juncheng Shang -
        # Juncheng Shang ML" pick under a bare "Tennis" bullet header was
        # previously dropped entirely, since only a bare name/ML (no
        # matchup) was ever recognized here.
        if pick and pick["kind"] == "track" and any(sep in bare for sep in (" vs. ", " vs ", " v. ", " v ")):
            pick["section"], pick["raw"] = current_category, bare
            results.append(pick)
            continue

        if sport in ("dota2", "cs2"):
            # No generic bare-name fallback for esports at all (see this
            # module's own docstring above _ESPORTS_MAP_HANDICAP_RE) -
            # hawk.live/GosuGamers can only resolve a match by BOTH team
            # names together, so a bare team with no opponent genuinely
            # can't be tracked. Confirmed live: a real "Iron Wing to Win at
            # Least One Map" / "BoomBoys ML" pick under a bare "Dota 2"
            # header (no matchup, and _parse_esports_pick above already
            # returned None for exactly that reason) fell all the way down
            # to here and got misread as a literal team named "Iron Wing to
            # Win at Least One Map" - then routed to the generic
            # scores365-backed auto-track, which has no esports coverage
            # whatsoever, so it queued and retried forever with zero chance
            # of ever resolving.
            continue

        name = _is_simple_pick_name(_clean_line(bare))
        if not name:
            continue
        results.append({"kind": "track", "sport": sport, "team": name, "section": current_category, "raw": bare})
    return results

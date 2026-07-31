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
import scores365
import sofascore

# Bracket category (lowercased, "Props" suffix stripped) -> our sport key.
# Baseball has multiple league names in the wild (MLB, KBO, ...) that all map
# to the same "baseball" sport key - 365scores/ESPN aren't scoped per-league
# for team/game lookups (confirmed live: 365scores' baseball feed already
# includes KBO games alongside MLB), just the label users type differs.
_SPORT_MAP = {
    "mlb": "baseball",
    "kbo": "baseball",
    "nba": "basketball",
    "wnba": "basketball",
    "nfl": "nfl",
    "nhl": "hockey",
    "soccer": "soccer",
    "tennis": "tennis",
    "rugby": "rugby",
    "volleyball": "volleyball",
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

_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?\[([^\]]+)\]\s*(.+)$")
# The optional "the" handles wording like "Roki Sasaki Over the 1.5 Walks
# Allowed" (confirmed live - a real pick worded this way silently never
# posted, since the number wasn't immediately after Over/Under).
_PLAYER_STAT_RE = re.compile(r"^(.+?)\s+(Over|Under)\s+(?:the\s+)?([\d.]+)\s+(.+?)\s*(?:\(|$)", re.IGNORECASE)

# "Team A vs Team B - YRFI - Yes Runs 1st Inning (...)" - settles after just
# the 1st inning, not the whole game, so it's routed to inningtracker.py
# rather than /track's kind="track" (see _parse_yrfi_line). Only one of the
# two team names is captured - either one is enough to look the game up.
_YRFI_LINE_RE = re.compile(r"^(.+?)\s+vs\.?\s+.+?-\s*(YRFI|NRFI)\b", re.IGNORECASE)

# "Team A vs Team B - Over 0.5 1st Inning (...)" means exactly the same bet
# as YRFI ("at least 1 run scores in the 1st inning") worded differently -
# 0.5 is the only line where "any run" vs "no runs" are the two possible
# outcomes, so it's routed to the same inningtracker.py kind rather than
# treated as a game/team total. Only the 0.5 line is recognized - "Over 1.5
# 1st Inning" means something else (2+ runs) that inningtracker.py doesn't
# grade, so that's deliberately left unmatched and falls through to being
# skipped rather than guessed.
_INNING_RUN_TOTAL_RE = re.compile(
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*-\s*(Over|Under)\s+0\.5\s+1st\s+Inning\b",
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
# single marker regex covers every phrasing.
_SET1_MARKER_RE = re.compile(
    r"\bto\s+win\s+1st\s+set\b|\b1st\s+set\s+(?:ml|moneyline|winner)\b|\bml\s+1st\s+set\b",
    re.IGNORECASE,
)

# "Team A vs Team B - 1st Inning Draw" (or "Tie") - the Draw side of the same
# 3-way "1st inning result" market. Either team name is enough to look the
# game up - grading only cares whether the 1st inning runs end level.
_INNING1_DRAW_RE = re.compile(
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*-\s*1st\s+inning\s+(?:draw|tie)\b",
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
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*-\s*"
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
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*-\s*"
    r"(?:f5|first\s+5\s+innings|first\s+five\s+innings|1st\s+5\s+innings)"
    r"\s+(.+?)\s+([+-]\d+(?:\.\d+)?)\b",
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
_TOTAL_LINE_RE = re.compile(
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*(?:-\s*)?(Over|Under)\s+([\d.]+)(?:\s+Total\s+\w+)?\s*$",
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
    r"^(.+?)\s*(?:@|vs\.?|v\.?)\s*(.+?)\s*-\s*(.+?)\s+(Over|Under)\s+([\d.]+)\b",
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
    ("baseball", "earned runs allowed"): "Earned Runs",
    ("baseball", "walks allowed"): "Walks (Pitching)",
}

# _SPORT_MAP collapses "nba"/"wnba" to the same "basketball" key since
# 365scores' team search spans both leagues under one sport type - but ESPN's
# player-prop lookup is scoped to a single league per sport key, so a WNBA
# prop needs to be routed to its own "wnba" ESPN sport instead of "basketball"
# (which only points at the NBA endpoint). Team picks aren't affected by this.
_PROP_SPORT_OVERRIDE = {
    "wnba": "wnba",
}


def _strip_trailing_parens(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()


def _clean_line(line: str) -> str:
    text = line.strip()
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
        if base == raw or raw in label.lower():
            return label
    return None


def _match_sofascore_stat_label(raw_stat: str) -> Optional[str]:
    """Tennis-only equivalent of _match_stat_label - ESPN doesn't support
    tennis at all (see espn.py's module docstring), so tennis props are
    backed by sofascore.STAT_CATALOG instead."""
    raw = raw_stat.strip().lower()
    catalog = sofascore.STAT_CATALOG.get("tennis", {})
    for label in catalog:
        if label.lower() == raw:
            return label
    for label in catalog:
        if raw in label.lower():
            return label
    return None


def _parse_tennis_player_prop(description: str) -> Optional[dict]:
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    player = re.sub(r"[\s-]+$", "", pm.group(1)).strip()
    direction = pm.group(2).lower()
    line = float(pm.group(3))
    stat_label = _match_sofascore_stat_label(pm.group(4).strip())
    if not player or not stat_label:
        return None
    return {"kind": "tennis_playerprops", "player": player, "stat": stat_label, "direction": direction, "line": line}


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


def _parse_team_pick(description: str) -> Optional[str]:
    text = _clean_line(description)
    lowered = text.lower()
    if any(marker in lowered for marker in _PARTIAL_GAME_MARKERS):
        return None
    for sep in (" vs. ", " vs ", " v. ", " v "):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    for cutword in ("Moneyline", "ML", " Over ", " Under "):
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
    """A bare 'Name' or 'Name ML' with nothing else attached - rejects
    anything with digits or prop-betting keywords, which signal a more
    complex bet we can't safely reduce to just a name."""
    stripped = re.sub(r"\b(?:ML|Moneyline)\b\s*$", "", text, flags=re.IGNORECASE).strip()
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


def _infer_sport_from_stat(description: str) -> Optional[str]:
    pm = _PLAYER_STAT_RE.match(description)
    if not pm:
        return None
    raw_stat = pm.group(4).strip()
    matches = {sport for sport in _INFER_SPORT_CANDIDATES if _match_stat_label(sport, raw_stat)}
    return matches.pop() if len(matches) == 1 else None


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
    prop_sport = _PROP_SPORT_OVERRIDE.get(sport_key, sport)
    stat_label = _match_stat_label(prop_sport, raw_stat)
    if not player or not stat_label:
        return None
    return {
        "kind": "playerprops", "sport": prop_sport, "player": player, "stat": stat_label,
        "direction": direction, "line": line,
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
    return _parse_team_pick(text)


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
    if not (scores365.names_match(named_team, team_a) or scores365.names_match(named_team, team_b)):
        return None  # doesn't look like either matchup side - don't guess
    return {
        "kind": "team_total", "sport": sport, "team": named_team,
        "direction": m.group(4).lower(), "line": float(m.group(5)),
    }


def _parse_description(sport: str, sport_key: str, description: str, is_prop_category: bool) -> Optional[dict]:
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

    named_total = _parse_named_team_total_pick(sport, description)
    if named_total:
        return named_total

    total = _parse_total_pick(sport, description)
    if total:
        return total

    # A description shaped like "Player Over/Under N Stat" is a player prop
    # even when the category itself is bare (e.g. "[MLB]" rather than
    # "[MLB Props]") - confirmed live that real messages mix both taggings
    # for the same bet type. A team-vs-team matchup line (e.g. "Angels vs
    # Giants - Over 8.5 Total Runs") is excluded even though it also matches
    # the Over/Under shape, since that's a game total (handled above), not a
    # single player's stat.
    has_matchup = any(sep in description for sep in (" vs. ", " vs ", " v. ", " v "))
    if not has_matchup:
        f5_total = _parse_f5_total_pick(description, sport)
        if f5_total:
            return f5_total

        f5_team = _parse_f5_pick(description)
        if f5_team:
            return {"kind": "f5_moneyline", "sport": sport, "team": f5_team}

        if sport == "baseball":
            inning1_team = _parse_inning1_team_pick(description)
            if inning1_team:
                return {"kind": "inning1_result", "sport": sport, "team": inning1_team, "pick": inning1_team}

        if sport == "tennis":
            set1_team = _parse_set1_pick(description)
            if set1_team:
                return {"kind": "set1_moneyline", "sport": sport, "team": set1_team}

            tennis_prop = _parse_tennis_player_prop(description)
            if tennis_prop:
                return tennis_prop

        prop = _parse_player_prop(sport_key, sport, description)
        if prop:
            return prop
        if is_prop_category:
            return None  # explicitly tagged as a prop but couldn't be confidently parsed - don't guess it's a team pick

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
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _LINE_RE.match(line)
        if m:
            current_category, description = m.group(1).strip(), m.group(2).strip()
            pick = _parse_with_category(current_category, description)
            if pick:
                results.append(pick)
            continue

        bare = _bullet_strip(line)
        if _is_yrfi_header(bare):
            current_category = "__yrfi__"
            continue
        if bare.lower() in _HEADER_SPORT_MAP:
            current_category = bare
            continue

        if not current_category:
            # No header/tag anywhere above this line - last resort before
            # giving up: a player-prop-shaped line whose stat wording is
            # unique to exactly one sport can still be safely inferred (see
            # _infer_sport_from_stat). Anything else genuinely has no way to
            # know the sport, so it's skipped rather than guessed.
            sport = _infer_sport_from_stat(bare)
            if sport:
                prop = _parse_player_prop(sport, sport, bare)
                if prop:
                    results.append(prop)
            continue

        if current_category.lower().endswith("props"):
            continue  # a Props section - bullets there aren't structured enough to parse safely

        if current_category == "__yrfi__":
            pick = _parse_yrfi_line(bare)
            if pick:
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
            results.append(pick)
            continue

        name = _is_simple_pick_name(_clean_line(bare))
        if not name:
            continue
        results.append({"kind": "track", "sport": sport, "team": name})
    return results

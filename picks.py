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

# Bracket category (lowercased, "Props" suffix stripped) -> our sport key.
_SPORT_MAP = {
    "mlb": "baseball",
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
_PLAYER_STAT_RE = re.compile(r"^(.+?)\s+(Over|Under)\s+([\d.]+)\s+(.+?)\s*(?:\(|$)", re.IGNORECASE)

# "Team A vs Team B - YRFI - Yes Runs 1st Inning (...)" - settles after just
# the 1st inning, not the whole game, so it's routed to inningtracker.py
# rather than /track's kind="track" (see _parse_yrfi_line). Only one of the
# two team names is captured - either one is enough to look the game up.
_YRFI_LINE_RE = re.compile(r"^(.+?)\s+vs\.?\s+.+?-\s*(YRFI|NRFI)\b", re.IGNORECASE)

# For a baseball "Strikeouts" prop with no other context, pitcher strikeouts
# is the overwhelmingly common bet type - default there rather than guessing
# batting vs pitching from wording alone (both exist in our stat catalog).
_AMBIGUOUS_STAT_DEFAULTS = {
    ("baseball", "strikeouts"): "Strikeouts (Pitching)",
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


def _parse_with_category(category: str, description: str) -> Optional[dict]:
    if _is_yrfi_header(category):
        return _parse_yrfi_line(description)

    is_prop_category = category.lower().endswith("props")
    sport_key = category.lower().replace("props", "").strip()
    sport = _SPORT_MAP.get(sport_key)
    if not sport:
        return None

    # A description shaped like "Player Over/Under N Stat" is a player prop
    # even when the category itself is bare (e.g. "[MLB]" rather than
    # "[MLB Props]") - confirmed live that real messages mix both taggings
    # for the same bet type. A team-vs-team matchup line (e.g. "Angels vs
    # Giants - Over 8.5 Total Runs") is excluded even though it also matches
    # the Over/Under shape, since that's a game total, not a single player's
    # stat.
    has_matchup = any(sep in description for sep in (" vs. ", " vs ", " v. ", " v "))
    if not has_matchup:
        prop = _parse_player_prop(sport_key, sport, description)
        if prop:
            return prop
        if is_prop_category:
            return None  # explicitly tagged as a prop but couldn't be confidently parsed - don't guess it's a team pick

    team = _parse_team_pick(description)
    if not team:
        return None
    return {"kind": "track", "sport": sport, "team": team}


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

        if not current_category or current_category.lower().endswith("props"):
            continue  # no context yet, or a Props section - bullets there aren't structured enough to parse safely

        if current_category == "__yrfi__":
            pick = _parse_yrfi_line(bare)
            if pick:
                results.append(pick)
            continue

        sport = _HEADER_SPORT_MAP.get(current_category.lower())
        if not sport:
            continue

        name = _is_simple_pick_name(_clean_line(bare))
        if not name:
            continue
        results.append({"kind": "track", "sport": sport, "team": name})
    return results

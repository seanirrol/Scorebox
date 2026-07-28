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
    "nfl": "nfl",
    "nhl": "hockey",
    "soccer": "soccer",
    "tennis": "tennis",
    "rugby": "rugby",
    "volleyball": "volleyball",
}

_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?\[([^\]]+)\]\s*(.+)$")
_PLAYER_STAT_RE = re.compile(r"^(.+?)\s+(?:Over|Under)\s+[\d.]+\s+(.+?)\s*(?:\(|$)", re.IGNORECASE)

# For a baseball "Strikeouts" prop with no other context, pitcher strikeouts
# is the overwhelmingly common bet type - default there rather than guessing
# batting vs pitching from wording alone (both exist in our stat catalog).
_AMBIGUOUS_STAT_DEFAULTS = {
    ("baseball", "strikeouts"): "Strikeouts (Pitching)",
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


def _parse_team_pick(description: str) -> Optional[str]:
    text = _clean_line(description)
    for sep in (" vs. ", " vs ", " v. ", " v "):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    for cutword in ("Moneyline", "ML", " Over ", " Under "):
        idx = text.find(cutword)
        if idx > 0:
            text = text[:idx].strip()
    return text or None


def _parse_with_category(category: str, description: str) -> Optional[dict]:
    is_prop = category.lower().endswith("props")
    sport_key = category.lower().replace("props", "").strip()
    sport = _SPORT_MAP.get(sport_key)
    if not sport:
        return None

    if is_prop:
        if sport not in espn.SPORT_PATHS:
            return None
        pm = _PLAYER_STAT_RE.match(description)
        if not pm:
            return None
        player, raw_stat = pm.group(1).strip(), pm.group(2).strip()
        stat_label = _match_stat_label(sport, raw_stat)
        if not player or not stat_label:
            return None
        return {"kind": "playerprops", "sport": sport, "player": player, "stat": stat_label}

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
    Parses every line of a message, skipping anything unparseable. A line
    with no "[Category]" tag of its own inherits the most recent tagged
    category above it - picks are often formatted with the tag only on the
    first side of a matchup, e.g.:
      "1. [Tennis] Marcos Giron (Fanatics -1985)"
      "Ugo Humbert (Bet365 -995)"
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
        elif current_category:
            description = line
        else:
            continue
        pick = _parse_with_category(current_category, description)
        if pick:
            results.append(pick)
    return results

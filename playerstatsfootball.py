#!/usr/bin/env python3
"""
playerstats.football client - a second soccer player-prop data source,
additive to scores365.SOCCER_STAT_CATALOG (Goals/Assists/Yellow Cards/Red
Cards, unchanged, still backed by 365scores). This module exists purely to
cover stats 365scores/ESPN don't expose per player at all: Shots, Shots on
Target, Tackles, Fouls Committed, Fouls Drawn, Dispossessed, Offsides, Key
Passes - confirmed live against real finished matches across two different
leagues (MLS, Liga MX). Passes Completed specifically is NOT available here
either (only "Key Passes", a different, much smaller stat) - no source found
anywhere exposes it.

No auth, no anti-bot blocking encountered (unlike Sofascore, which 403s this
bot's VPS specifically - confirmed live, see tennispropstracker.py) - plain
`requests` works fine.

Match lookup has no clean search API on this site, so it's done by guessing
the fixture URL directly from the two team names:
  https://playerstats.football/fixture/{home-slug}/{away-slug}[/{date}]
Most team slugs are a plain kebab-case of the common name, but not all (e.g.
New York Red Bulls -> "new-york-rb", not "new-york-red-bulls") - a team
whose slug doesn't match the guess just isn't found, same "best effort,
skip rather than guess wrong" treatment as everywhere else in this
codebase, not a crash. find_fixture tries both team orders, each with and
without a date suffix (up to 4 attempts) - date-qualified first when a
kickoff is known, since a team pairing with multiple meetings (e.g. a
two-leg Leagues Cup tie) silently 200s on the bare, dateless URL too,
resolving to a DIFFERENT (wrong) meeting rather than 404ing - confirmed
live. Every successful fetch is additionally cross-checked against the
expected kickoff via the page's own startDate before being accepted, as a
second safety net against exactly that.
"""

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import requests

import scores365

BASE_URL = "https://playerstats.football"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 8

_LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

# Our display label -> this site's own stat name (confirmed live against the
# schema.org JSON-LD block real fixture pages embed). Deliberately excludes
# Goals/Assists/Yellow Cards/Red Cards - those already work fine via
# scores365.SOCCER_STAT_CATALOG, no reason to move them.
STAT_CATALOG: dict[str, str] = {
    "Shots": "Shots",
    "Shots on Target": "Shots on Target (SoT)",
    "Tackles": "Tackles",
    "Fouls Committed": "Fouls Committed",
    "Fouls Drawn": "Fouls Drawn",
    "Dispossessed": "Dispossessed",
    "Offsides": "Offsides",
    "Key Passes": "Key Passes",
}


class PlayerStatsFootballError(Exception):
    pass


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def fetch_path(path: str) -> Optional[str]:
    """Fetches an already-resolved fixture path directly - one request, no
    guessing. Used every poll cycle once find_fixture has already done the
    (more expensive) one-time resolution below and its result is persisted."""
    try:
        resp = requests.get(f"{BASE_URL}{path}", headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    return resp.text if resp.status_code == 200 else None


def find_fixture(home_name: str, away_name: str, kickoff_epoch: Optional[float]) -> Optional[str]:
    """Resolves the two team names to a fixture PATH (e.g.
    "/fixture/cincinnati/pachuca/2026-08-04"), not HTML - meant to be called
    once, with the result persisted and passed to fetch_path on every later
    poll instead of re-resolving from scratch each time. Returns None if no
    slug/order/date combination resolved to the right page.

    Date-qualified URLs are tried FIRST when a kickoff is known, not last -
    confirmed live that a team pairing with multiple meetings (e.g. a
    two-leg Leagues Cup tie) silently 200s on the bare, dateless URL too,
    resolving to a DIFFERENT (wrong) meeting rather than 404ing - trying the
    bare URL first would have silently returned the wrong match's data. As
    a second safety net on top of that reordering, every successful fetch's
    own startDate is cross-checked against the expected kickoff (within a
    day, to absorb timezone/rounding) before being accepted at all."""
    home_slug, away_slug = _slugify(home_name), _slugify(away_name)
    if not home_slug or not away_slug:
        return None
    date_str = (
        datetime.fromtimestamp(kickoff_epoch, tz=timezone.utc).strftime("%Y-%m-%d") if kickoff_epoch else None
    )
    candidates = []
    if date_str:
        candidates += [f"/fixture/{home_slug}/{away_slug}/{date_str}", f"/fixture/{away_slug}/{home_slug}/{date_str}"]
    candidates += [f"/fixture/{home_slug}/{away_slug}", f"/fixture/{away_slug}/{home_slug}"]
    for path in candidates:
        html = fetch_path(path)
        if html and _matches_kickoff(html, kickoff_epoch):
            return path
    return None


def _matches_kickoff(html: str, kickoff_epoch: Optional[float]) -> bool:
    """No expected kickoff to check against (e.g. a still-hibernating pick
    whose 365scores kickoff time wasn't threaded through) - accept
    unconditionally. Otherwise the fetched page's own startDate must be
    within a day of it."""
    if kickoff_epoch is None:
        return True
    m = _LD_JSON_RE.search(html)
    if not m:
        return False
    start_date = re.search(r'"startDate":\s*"([^"]+)"', m.group(1))
    if not start_date:
        return False
    try:
        page_epoch = datetime.fromisoformat(start_date.group(1)).timestamp()
    except ValueError:
        return False
    return abs(page_epoch - kickoff_epoch) < 86400


def parse_match(html: str) -> Optional[dict]:
    """Returns {"players": {player_name: {stat_name: raw_value_str}}} pulled
    from the fixture page's own schema.org JSON-LD block, or None if the
    page didn't have one (shouldn't happen for a real fixture page, but
    fetch/parse errors are treated as "no data" rather than raised)."""
    m = _LD_JSON_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    try:
        data = json.loads(raw)
    except ValueError:
        # Confirmed live: a not-yet-played fixture's JSON-LD emits an
        # unquoted "title" value (e.g. `"title": Team A vs Team B - ...`,
        # missing its opening/closing quotes entirely), breaking strict
        # parsing. title/description/url aren't used here anyway, so
        # truncate the object right before "title" and re-close it rather
        # than losing the whole (otherwise well-formed) homeTeam/awayTeam
        # data over one broken trailing field.
        cut = raw.find('"title"')
        if cut == -1:
            return None
        try:
            data = json.loads(raw[:cut].rstrip().rstrip(",") + "}")
        except ValueError:
            return None
    players: dict[str, dict[str, str]] = {}
    for side in ("homeTeam", "awayTeam"):
        team = data.get(side) or {}
        for athlete in team.get("athlete") or []:
            name = athlete.get("name")
            if not name:
                continue
            stats = {
                prop["name"]: prop["value"]
                for prop in (athlete.get("additionalProperty") or [])
                if prop.get("name") and "value" in prop
            }
            players[name] = stats
    return {"players": players}


_SUB_NOTE_RE = re.compile(r'text-gray-400[^>]*>\(([^)]+)\)')


def was_substituted(html: str, player_name: str) -> bool:
    """True if player_name appears as a substitution note ("(R. Lewandowski)"
    next to whoever came on for them) anywhere in the raw fixture page -
    confirmed live this site's structured JSON-LD block (what parse_match
    reads) only includes players still on the field at full time, silently
    omitting anyone substituted out during the match even though they
    genuinely played. get_player_stat returning None for a finished match
    is ambiguous between "never played at all" and "played, subbed off,
    just not in this site's structured summary" - this disambiguates the
    second case so it can be voided with an honest reason instead of
    looking like a plain DNP. Doesn't recover the actual stat value (that
    requires HTML the raw JSON-LD doesn't expose - reading the underlying
    site's own UI directly is the only reliable way right now), just
    identifies when that's actually what happened."""
    for name in _SUB_NOTE_RE.findall(html):
        if scores365.names_match(name, player_name):
            return True
    return False


def get_player_stat(match: dict, player_name: str, stat_label: str) -> Optional[float]:
    """Looks up one player's value for one stat (our display label, e.g.
    "Shots on Target" - translated to this site's own field name via
    STAT_CATALOG). Returns None if the player isn't in this match's athlete
    list at all (unclear whether they even played - not safe to assume 0).
    A player who IS listed but has no entry for this specific stat gets 0 -
    confirmed live this site only lists stats a player actually accrued
    (e.g. a player with only a "Shots" entry and nothing else genuinely had
    zero fouls/cards/tackles/etc, not "unknown")."""
    site_key = STAT_CATALOG.get(stat_label)
    if not site_key:
        return None
    for name, stats in match.get("players", {}).items():
        if scores365.names_match(name, player_name):
            raw = stats.get(site_key, "0")
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None

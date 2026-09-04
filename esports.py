#!/usr/bin/env python3
"""
Dota 2 / CS2 / LoL / Mobile Legends esports live match data.

Ported from the same approach already proven in the Torn-BetSync project
(see src/hawklive.js and src/gosugamers.js) - Dota 2 goes through hawk.live
first (its own games-won/current-game-number/decided-results tracking was
confirmed there to be more accurate and timely than GosuGamers'), falling
back to GosuGamers when hawk.live has nothing for a given match; CS2/LoL/
Mobile Legends go through GosuGamers only (hawk.live doesn't carry any of
them). Both sites are scraped via curl rather than `requests` for the same
TLS-fingerprint-block reason already documented in scoreimage.py's own logo
fetch.

Unlike scores365.py/espn.py, there's no stable "refetch by id" endpoint on
either site distinct from a fresh team-name search - every lookup here
(including a tracker's own repeated polling) re-resolves the match by team
name each time, exactly mirroring hawklive.js/gosugamers.js's own
architecture rather than trying to invent a persistent id this bot's own
trackers could hold onto between polls.
"""

import json
import re
import subprocess
import time
from typing import Optional

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CURL_TIMEOUT_SECONDS = 8


class EsportsError(Exception):
    pass


def _curl_text(url: str) -> str:
    try:
        result = subprocess.run(
            ["curl", "-s", "-A", USER_AGENT, url],
            capture_output=True, timeout=CURL_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
        raise EsportsError(str(e)) from e
    text = result.stdout.decode("utf-8", errors="replace")
    if not text:
        raise EsportsError(f"Empty response from {url}")
    return text


# --- sport identification ---------------------------------------------------

def _normalize_sport(sport: str) -> Optional[str]:
    s = (sport or "").strip().lower()
    if s in ("dota 2", "dota2"):
        return "dota2"
    if s in ("counter-strike", "counter strike", "cs2", "cs:2", "cs 2", "counterstrike"):
        return "cs2"
    if s in ("lol", "league of legends", "league-of-legends"):
        return "lol"
    if s in ("mobilelegends", "mobile legends", "mobile-legends", "mlbb"):
        return "mobilelegends"
    return None


def is_supported_sport(sport: str) -> bool:
    return _normalize_sport(sport) is not None


# --- team-name fuzzy matching -----------------------------------------------

def _normalize_words(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return [w for w in cleaned.split(" ") if w]


def _word_match(x: str, y: str) -> bool:
    return x == y or (len(x) >= 2 and len(y) >= 2 and (x.startswith(y) or y.startswith(x)))


# Word-by-word prefix comparison rather than one big substring check - a
# sponsor-abbreviated team name ("1w Team" vs hawk.live's own "1win Team")
# shares no contiguous substring once whitespace is stripped, even though
# "1w" is plainly a prefix of "1win". Comparing per-word instead catches
# that without loosening the match enough to conflate two different teams
# that just happen to share a generic word. Ported from hawklive.js.
def names_match(a: Optional[str], b: Optional[str]) -> bool:
    words_a, words_b = _normalize_words(a), _normalize_words(b)
    if not words_a or not words_b:
        return False
    small, big = (words_a, words_b) if len(words_a) <= len(words_b) else (words_b, words_a)
    return all(any(_word_match(w, bw) for bw in big) for w in small)


def _normalize_compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _loose_contains_match(a: str, b: str) -> bool:
    na, nb = _normalize_compact(a), _normalize_compact(b)
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


# --- hawk.live (Dota 2 primary) ---------------------------------------------

_HAWK_HOME_URL = "https://hawk.live/"
_HAWK_HOME_CACHE_SECONDS = 15
# A Cloudflare bot-challenge page still returns HTTP 200 with an HTML body -
# curl itself sees that as success. Real pages here are consistently 90KB+;
# a challenge/error page is nowhere close. Ported from hawklive.js.
_HAWK_MIN_HOME_LENGTH = 50_000
_HAWK_DETAIL_CACHE_SECONDS = 20

_hawk_home_cache: Optional[dict] = None  # {"live": [...], "results": [...], "fetched_at": ts}
_hawk_detail_cache: dict = {}  # series id -> (detail, fetched_at)


# Inertia.js server-renders each page's props as one HTML-entity-escaped
# JSON blob in a root element's data-page="..." attribute.
def _extract_data_page(html: str) -> Optional[dict]:
    marker = 'data-page="'
    start = html.find(marker)
    if start == -1:
        return None
    from_ = start + len(marker)
    end = html.find('">', from_)
    if end == -1:
        return None
    decoded = (
        html[from_:end]
        .replace("&quot;", '"').replace("&#039;", "'")
        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    )
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return None


def _hawk_home_data() -> dict:
    global _hawk_home_cache
    if _hawk_home_cache and time.time() - _hawk_home_cache["fetched_at"] < _HAWK_HOME_CACHE_SECONDS:
        return _hawk_home_cache
    html = _curl_text(_HAWK_HOME_URL)
    if len(html) < _HAWK_MIN_HOME_LENGTH:
        raise EsportsError("hawk.live response too small - likely a bot-challenge page")
    page = _extract_data_page(html) or {}
    props = page.get("props") or {}
    data = {
        "live": props.get("seriesList") or [],
        "results": props.get("latestSeriesResults") or [],
        "fetched_at": time.time(),
    }
    _hawk_home_cache = data
    return data


def _hawk_teams_match(series: dict, team_a: str, team_b: str) -> bool:
    t1 = (series.get("team1") or {}).get("name")
    t2 = (series.get("team2") or {}).get("name")
    return (names_match(t1, team_a) and names_match(t2, team_b)) or (names_match(t1, team_b) and names_match(t2, team_a))


def _hawk_resolve(team_a: str, team_b: str) -> Optional[tuple[dict, bool]]:
    """Returns (series, is_finished) or None."""
    try:
        data = _hawk_home_data()
    except EsportsError:
        return None
    for m in data["live"]:
        if _hawk_teams_match(m, team_a, team_b):
            return m, False
    for m in data["results"]:
        if _hawk_teams_match(m, team_a, team_b):
            return m, True
    return None


def _hawk_series_decided(series: dict) -> bool:
    best_of = series.get("bestOf") or 1
    majority = best_of // 2 + 1
    t1, t2 = series.get("team1Score", 0), series.get("team2Score", 0)
    return t1 >= majority or t2 >= majority or t1 + t2 >= best_of


def _hawk_match_page_url(series: dict) -> str:
    champ_slug = (series.get("championship") or {}).get("slug")
    return f"https://hawk.live/dota-2/matches/{champ_slug}/{series.get('slug')}"


def _hawk_series_games(series: dict) -> list[dict]:
    """Per-series detail page's individual map list - each entry carries
    `number`, `isTeam1Radiant`, `isRadiantWinner` (null until that map
    ends). Cached by series id."""
    series_id = series.get("id")
    cached = _hawk_detail_cache.get(series_id)
    if cached and time.time() - cached[1] < _HAWK_DETAIL_CACHE_SECONDS:
        return cached[0]
    try:
        html = _curl_text(_hawk_match_page_url(series))
        page = _extract_data_page(html) or {}
    except EsportsError:
        return []
    games = ((page.get("props") or {}).get("seriesPageData") or {}).get("matches") or []
    _hawk_detail_cache[series_id] = (games, time.time())
    return games


def _hawk_start_epoch(series: dict) -> Optional[float]:
    start_at = series.get("startAt")
    if not start_at:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(start_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _get_series_hawk(team_a: str, team_b: str) -> Optional[dict]:
    resolved = _hawk_resolve(team_a, team_b)
    if not resolved:
        return None
    series, provider_finished = resolved

    a_is_team1 = names_match((series.get("team1") or {}).get("name"), team_a)
    home_score = series.get("team1Score", 0) if a_is_team1 else series.get("team2Score", 0)
    away_score = series.get("team2Score", 0) if a_is_team1 else series.get("team1Score", 0)

    decided = provider_finished or _hawk_series_decided(series)
    start_epoch = _hawk_start_epoch(series)
    if decided:
        status = "finished"
    elif start_epoch and start_epoch > time.time():
        status = "notstarted"
    else:
        status = "inprogress"

    home_team = series.get("team1") if a_is_team1 else series.get("team2")
    away_team = series.get("team2") if a_is_team1 else series.get("team1")

    return {
        "sport": "dota2",
        "provider": "hawklive",
        "home_team": (home_team or {}).get("name"),
        "away_team": (away_team or {}).get("name"),
        "home_logo_url": (home_team or {}).get("logoUrl"),
        "away_logo_url": (away_team or {}).get("logoUrl"),
        "home_score": home_score,
        "away_score": away_score,
        "best_of": series.get("bestOf") or 1,
        "status": status,
        "current_game_number": max(series.get("currentMatchNumber") or 1, home_score + away_score + 1),
        "start_epoch": start_epoch,
        "tournament": (series.get("championship") or {}).get("name"),
        "page_url": _hawk_match_page_url(series),
        "_ref": {"provider": "hawklive", "series": series, "home_is_team1": a_is_team1},
    }


def _hawk_map_winners(series_data: dict) -> list[Optional[str]]:
    ref = series_data["_ref"]
    games = _hawk_series_games(ref["series"])
    home_is_team1 = ref["home_is_team1"]
    winners: list[Optional[str]] = []
    for g in sorted(games, key=lambda g: g.get("number") or 0):
        is_radiant_winner = g.get("isRadiantWinner")
        if is_radiant_winner is None:
            winners.append(None)
            continue
        team1_won = is_radiant_winner == g.get("isTeam1Radiant")
        winners.append(("home" if home_is_team1 else "away") if team1_won else ("away" if home_is_team1 else "home"))
    return winners


def _hawk_game_kills(game: dict) -> Optional[tuple[int, int]]:
    """(team1, team2) kill count from one map's last recorded state, still
    oriented to hawk.live's own team1/team2 (not yet home/away) - radiant/
    dire flipped according to isTeam1Radiant. None if that map has no state
    data recorded yet. Shared by _hawk_live_kill_count (whichever map is
    live right now), _hawk_total_kills (summed across every map) and
    _hawk_map_kills (one specific map by number)."""
    states = game.get("states") or []
    if not states:
        return None
    last_state = states[-1]
    radiant_kills, dire_kills = last_state.get("radiantScore"), last_state.get("direScore")
    if radiant_kills is None or dire_kills is None:
        return None
    return (radiant_kills, dire_kills) if game.get("isTeam1Radiant") else (dire_kills, radiant_kills)


def _hawk_live_kill_count(series_data: dict) -> Optional[tuple[int, int]]:
    """(home, away) kill count for whichever map is currently being played
    - the one entry in the series' own game list with no winner recorded
    yet. Purely a supplementary live display, not used for grading
    anything (every market here settles on maps won, not kills)."""
    ref = series_data["_ref"]
    games = _hawk_series_games(ref["series"])
    current = next((g for g in games if g.get("isRadiantWinner") is None), None)
    if not current:
        return None
    kills = _hawk_game_kills(current)
    if kills is None:
        return None
    home_is_team1 = ref["home_is_team1"]
    return kills if home_is_team1 else (kills[1], kills[0])


def _hawk_map_kills(series_data: dict, map_number: int) -> Optional[tuple[int, int]]:
    """(home, away) kill count for one specific map (1-indexed) in the
    series - backs grade_map_kills_handicap, a spread on a single map's
    kill count rather than the series-combined total (_hawk_total_kills) or
    whichever map happens to be live right now (_hawk_live_kill_count).
    None if that map hasn't been played, or has no state data recorded yet."""
    ref = series_data["_ref"]
    games = _hawk_series_games(ref["series"])
    game = next((g for g in games if g.get("number") == map_number), None)
    if not game:
        return None
    kills = _hawk_game_kills(game)
    if kills is None:
        return None
    home_is_team1 = ref["home_is_team1"]
    return kills if home_is_team1 else (kills[1], kills[0])


def _hawk_total_kills(series_data: dict) -> Optional[tuple[int, int]]:
    """(home, away) kill count summed across every map played so far in the
    series - a finished map's last recorded state holds its final tally,
    the in-progress map's (if any) holds its live-running one, so this is
    itself live-updating right up until the series ends. Distinct from
    _hawk_live_kill_count, which only reports the current map's own count.
    None if no map has any state data yet."""
    ref = series_data["_ref"]
    games = _hawk_series_games(ref["series"])
    if not games:
        return None
    home_is_team1 = ref["home_is_team1"]
    team1_total = team2_total = 0
    found_any = False
    for g in games:
        kills = _hawk_game_kills(g)
        if kills is None:
            continue
        found_any = True
        team1_total += kills[0]
        team2_total += kills[1]
    if not found_any:
        return None
    return (team1_total, team2_total) if home_is_team1 else (team2_total, team1_total)


# --- GosuGamers (CS2/LoL/Mobile Legends primary, Dota 2 fallback) -----------

_GOSU_GAME_SLUGS = {"dota2": "dota2", "cs2": "counterstrike", "lol": "lol", "mobilelegends": "mobile-legends"}
_GOSU_SLUG_TO_SPORT = {slug: sport for sport, slug in _GOSU_GAME_SLUGS.items()}
_GOSU_LIST_CACHE_SECONDS = 30
_GOSU_LIST_PAGES = 3
_GOSU_DETAIL_CACHE_SECONDS = 15
# Generous enough to cover a match sitting off the list for a while before a
# poll catches up, nowhere near enough to span two genuinely different
# matches between the same two teams (realistically hours-to-days apart even
# within one bracket). Ported from gosugamers.js's own LAST_KNOWN_MATCH_WINDOW_MS.
_GOSU_LAST_KNOWN_WINDOW_SECONDS = 6 * 3600

_gosu_list_cache: dict = {}  # game_slug -> (matches, fetched_at)
_gosu_detail_cache: dict = {}  # matchId -> (detail, fetched_at)
# gosugamers.net's own matches list drops a match entirely the moment it's no
# longer live/upcoming, even though its detail page still has the fully
# correct final result - confirmed live in the Torn-BetSync project (see
# gosugamers.js's own lastKnownMatch comment). Without this, a bet could get
# stuck with no result the moment its match finishes and drops off the list.
_gosu_last_known_match: dict = {}  # "game_slug|teamA|teamB" (sorted) -> (match, remembered_at)


# Next.js can split one continuous React Server Component payload across
# MULTIPLE self.__next_f.push([1,"..."]) <script> tags once it's long
# enough - confirmed live: a match with a long enough matchGames history
# had its own match object torn mid-token exactly at the boundary between
# two pushes, with literal `</script><script>...` HTML sitting in the
# middle of what should have been one continuous JSON string. Each push's
# own argument is a complete, well-formed JS string literal on its own (an
# unescaped double quote always really does close it) - joining every
# push's raw argument, in document order, reconstructs the original
# unbroken stream before any key search runs, rather than treating the raw
# downloaded HTML itself as one continuous string the way a page that
# happens to fit in a single push would allow.
_NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _gosu_joined_flight_text(html: str) -> str:
    parts = [m.group(1) for m in _NEXT_F_PUSH_RE.finditer(html)]
    return "".join(parts) if parts else html


def _extract_json_after_key(html: str, key: str, from_index: int = 0) -> Optional[dict]:
    """gosugamers.net (Next.js App Router) streams its page data as React
    Server Component "flight" payloads - the object needed is embedded as a
    JS-string-escaped JSON blob inside that stream, with unrelated HTML/JS
    following it (no clean end-of-string marker). Grabs everything from the
    key to the end of the document, un-escapes it, and lets json.loads
    itself report exactly where the valid object ends via its own error
    position - the slice up to there is guaranteed-valid JSON. Ported from
    gosugamers.js's extractJsonAfterKey."""
    marker = f'\\"{key}\\":'
    start = html.find(marker, from_index)
    if start == -1:
        return None
    i = start + len(marker)
    if i >= len(html) or html[i] not in "{[":
        return None
    raw = html[i:]
    out = []
    k = 0
    while k < len(raw):
        if raw[k] == "\\" and k + 1 < len(raw) and raw[k + 1] == "\\":
            out.append("\\")
            k += 2
        elif raw[k] == "\\" and k + 1 < len(raw) and raw[k + 1] == '"':
            out.append('"')
            k += 2
        else:
            out.append(raw[k])
            k += 1
    text = "".join(out)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            return json.loads(text[: e.pos])
        except (json.JSONDecodeError, ValueError):
            return None


def _gosu_matches_for_slug(game_slug: str) -> list[dict]:
    cached = _gosu_list_cache.get(game_slug)
    if cached and time.time() - cached[1] < _GOSU_LIST_CACHE_SECONDS:
        return cached[0]
    matches: list[dict] = []
    for page in range(1, _GOSU_LIST_PAGES + 1):
        url = f"https://www.gosugamers.net/{game_slug}/matches" + (f"?page={page}" if page > 1 else "")
        try:
            html = _curl_text(url)
        except EsportsError:
            break
        data = _extract_json_after_key(_gosu_joined_flight_text(html), "defaultMatches")
        page_matches = (data or {}).get("matches") or []
        if not page_matches:
            break
        matches.extend(page_matches)
        total_pages = (data or {}).get("totalPages")
        if isinstance(total_pages, int) and page >= total_pages:
            break
    _gosu_list_cache[game_slug] = (matches, time.time())
    return matches


def _gosu_find_match(matches: list[dict], team_a: str, team_b: str, expected_epoch: Optional[float]) -> Optional[dict]:
    # names_match (word-prefix based) rather than the plain compact-
    # substring _loose_contains_match - confirmed live a real "Yakult's
    # Brothers" pick failed to resolve against GosuGamers' own "Yakult
    # Brothers" (no apostrophe-s), since the extra "s" broke substring
    # containment in both directions even though every actual word still
    # matched. names_match tokenizes on punctuation (including apostrophes)
    # instead of just stripping it, so "Yakult's"/"Yakult" both reduce to
    # the same "yakult" token.
    def is_a(name):
        return names_match(name, team_a)

    def is_b(name):
        return names_match(name, team_b)

    candidates = [
        m for m in matches
        if ((is_a(m.get("team1Name")) or is_a(m.get("team1Tag"))) and (is_b(m.get("team2Name")) or is_b(m.get("team2Tag"))))
        or ((is_a(m.get("team2Name")) or is_a(m.get("team2Tag"))) and (is_b(m.get("team1Name")) or is_b(m.get("team1Tag"))))
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # No live/finished status on the list itself (only the detail page has
    # that) - closest kickoff to the expected/known time is the best
    # available signal for a back-to-back series between the same two teams.
    target = expected_epoch * 1000 if expected_epoch else time.time() * 1000
    return min(candidates, key=lambda m: abs((m.get("startingAt") or 0) - target))


def _gosu_match_page_url(game_slug: str, match: dict) -> str:
    return (
        f"https://www.gosugamers.net/{game_slug}/tournaments/"
        f"{match['parentId']}-{match['parentUrlSafeName']}/matches/"
        f"{match['matchId']}-{match['team1UrlSafeName']}-vs-{match['team2UrlSafeName']}"
    )


def _gosu_match_detail(game_slug: str, match: dict) -> Optional[dict]:
    match_id = match["matchId"]
    cached = _gosu_detail_cache.get(match_id)
    if cached and time.time() - cached[1] < _GOSU_DETAIL_CACHE_SECONDS:
        return cached[0]
    try:
        html = _curl_text(_gosu_match_page_url(game_slug, match))
    except EsportsError:
        return None
    joined = _gosu_joined_flight_text(html)
    detail = None
    from_index = 0
    for _ in range(10):
        marker = '\\"match\\":'
        idx = joined.find(marker, from_index)
        if idx == -1:
            break
        candidate = _extract_json_after_key(joined, "match", from_index)
        if candidate and candidate.get("matchId") is not None:
            detail = candidate
            break
        from_index = idx + len(marker)
    _gosu_detail_cache[match_id] = (detail, time.time())
    return detail


def _gosu_last_known_key(game_slug: str, team_a: str, team_b: str) -> str:
    names = sorted([_normalize_compact(team_a), _normalize_compact(team_b)])
    return f"{game_slug}|{names[0]}|{names[1]}"


def _gosu_resolve(game_slug: str, team_a: str, team_b: str, expected_epoch: Optional[float]) -> Optional[tuple[dict, dict]]:
    matches = _gosu_matches_for_slug(game_slug)
    key = _gosu_last_known_key(game_slug, team_a, team_b)
    match = _gosu_find_match(matches, team_a, team_b, expected_epoch)
    if match:
        _gosu_last_known_match[key] = (match, time.time())
    else:
        remembered = _gosu_last_known_match.get(key)
        if remembered and time.time() - remembered[1] < _GOSU_LAST_KNOWN_WINDOW_SECONDS:
            match = remembered[0]
    if not match:
        return None
    detail = _gosu_match_detail(game_slug, match)
    if not detail:
        return None
    return match, detail


def _gosu_series_decided(detail: dict) -> bool:
    games_per_match = detail.get("gamesPerMatch") or 1
    opponents = detail.get("opponents") or []
    if len(opponents) < 2:
        return False
    a, b = opponents[0].get("score") or 0, opponents[1].get("score") or 0
    majority = games_per_match // 2 + 1
    return a >= majority or b >= majority or a + b >= games_per_match


def _get_series_gosu(game_slug: str, team_a: str, team_b: str, expected_epoch: Optional[float]) -> Optional[dict]:
    resolved = _gosu_resolve(game_slug, team_a, team_b, expected_epoch)
    if not resolved:
        return None
    match, detail = resolved

    opponents = detail.get("opponents") or []
    team1_reg = detail.get("team1RegistrationId")
    team2_reg = detail.get("team2RegistrationId")
    opp_by_reg = {o.get("registrationId"): o for o in opponents}
    team1_opp, team2_opp = opp_by_reg.get(team1_reg), opp_by_reg.get(team2_reg)
    if not team1_opp or not team2_opp:
        return None

    a_is_team1 = names_match(team1_opp.get("name"), team_a)
    home_opp = team1_opp if a_is_team1 else team2_opp
    away_opp = team2_opp if a_is_team1 else team1_opp

    decided = _gosu_series_decided(detail)
    start_epoch = (detail.get("startingAt") or 0) / 1000 or None
    if decided:
        status = "finished"
    elif start_epoch and start_epoch > time.time():
        status = "notstarted"
    else:
        status = "inprogress"

    home_score, away_score = home_opp.get("score") or 0, away_opp.get("score") or 0

    return {
        "sport": _GOSU_SLUG_TO_SPORT.get(game_slug, game_slug),
        "provider": "gosugamers",
        "home_team": home_opp.get("name"),
        "away_team": away_opp.get("name"),
        "home_logo_url": home_opp.get("opponentImage"),
        "away_logo_url": away_opp.get("opponentImage"),
        "tournament": detail.get("parentTournamentName"),
        "home_score": home_score,
        "away_score": away_score,
        "best_of": detail.get("gamesPerMatch") or 1,
        "status": status,
        "current_game_number": home_score + away_score + 1,
        "start_epoch": start_epoch,
        "page_url": _gosu_match_page_url(game_slug, match),
        "_ref": {
            "provider": "gosugamers", "game_slug": game_slug, "match": match, "detail": detail,
            "home_registration_id": home_opp.get("registrationId"), "away_registration_id": away_opp.get("registrationId"),
        },
    }


def _gosu_map_winners(series_data: dict) -> list[Optional[str]]:
    ref = series_data["_ref"]
    games = (ref["detail"].get("matchGames") or [])
    home_reg, away_reg = ref["home_registration_id"], ref["away_registration_id"]
    winners: list[Optional[str]] = []
    for g in sorted(games, key=lambda g: g.get("gameNumber") or 0):
        result = g.get("gameResult")
        if not result:
            winners.append(None)
        elif result == home_reg:
            winners.append("home")
        elif result == away_reg:
            winners.append("away")
        else:
            winners.append(None)
    return winners


# --- Strafe.com (CS2 live round score only) ---------------------------

# CS2's own live in-game progress indicator is the round score, not a kill
# count - the natural equivalent of hawk.live's live kills for Dota 2.
# Neither hawk.live nor GosuGamers expose this at all (GosuGamers only ever
# has the maps-won score, confirmed - see this module's own gosugamers
# section), so this is a third, narrowly-scoped site used purely for this
# one supplementary display, same curl-based approach as the other two.
_STRAFE_SPORT_SLUGS = {"cs2": "csgo"}
_STRAFE_LIST_CACHE_SECONDS = 15
_STRAFE_DETAIL_CACHE_SECONDS = 10

_strafe_list_cache: dict = {}  # sport_slug -> (matches, fetched_at)
_strafe_detail_cache: dict = {}  # slug -> (header, fetched_at)


def _strafe_extract_next_data(html: str) -> Optional[dict]:
    marker = '<script id="__NEXT_DATA__" type="application/json">'
    start = html.find(marker)
    if start == -1:
        return None
    content_start = start + len(marker)
    end = html.find("</script>", content_start)
    if end == -1:
        return None
    try:
        return json.loads(html[content_start:end])
    except json.JSONDecodeError:
        return None


def _strafe_live_matches(sport_slug: str) -> list[dict]:
    cached = _strafe_list_cache.get(sport_slug)
    if cached and time.time() - cached[1] < _STRAFE_LIST_CACHE_SECONDS:
        return cached[0]
    try:
        html = _curl_text(f"https://www.strafe.com/{sport_slug}/")
    except EsportsError:
        return []
    data = _strafe_extract_next_data(html) or {}
    matches = (((data.get("props") or {}).get("pageProps") or {}).get("matches")) or []
    _strafe_list_cache[sport_slug] = (matches, time.time())
    return matches


def _strafe_find_live_slug(sport_slug: str, home_team: str, away_team: str) -> Optional[str]:
    for m in _strafe_live_matches(sport_slug):
        if m.get("status") != "LIVE":
            continue
        names = [(c.get("competitor") or {}).get("name") for c in (m.get("competitors") or [])]
        if any(names_match(n, home_team) for n in names) and any(names_match(n, away_team) for n in names):
            return m.get("slug")
    return None


def _strafe_match_header(slug: str) -> Optional[dict]:
    cached = _strafe_detail_cache.get(slug)
    if cached and time.time() - cached[1] < _STRAFE_DETAIL_CACHE_SECONDS:
        return cached[0]
    try:
        html = _curl_text(f"https://www.strafe.com/match/{slug}/")
    except EsportsError:
        return None
    data = _strafe_extract_next_data(html) or {}
    header = (((data.get("props") or {}).get("pageProps") or {}).get("legacyMatch") or {}).get("header")
    _strafe_detail_cache[slug] = (header, time.time())
    return header


def _strafe_live_round_score(home_team: str, away_team: str) -> Optional[tuple[int, int]]:
    slug = _strafe_find_live_slug("csgo", home_team, away_team)
    if not slug:
        return None
    header = _strafe_match_header(slug)
    if not header:
        return None
    live = (header.get("scores") or {}).get("live")
    if not live or live.get("home") is None or live.get("away") is None:
        return None
    strafe_home_name = ((header.get("competitors") or {}).get("home") or {}).get("name")
    if names_match(strafe_home_name, home_team):
        return live["home"], live["away"]
    return live["away"], live["home"]


# --- public API ---------------------------------------------------------

def get_series(sport: str, team_a: str, team_b: str, start_time: Optional[float] = None) -> Optional[dict]:
    """Unified live/finished series lookup - team_a/team_b order is
    preserved in the returned home_team/away_team/home_score/away_score
    regardless of which side either provider itself calls "team1"/"home".
    start_time is an optional epoch-seconds hint used only to disambiguate
    a back-to-back GosuGamers series between the same two teams."""
    norm_sport = _normalize_sport(sport)
    if not norm_sport:
        return None
    if norm_sport == "dota2":
        result = _get_series_hawk(team_a, team_b)
        if result:
            return result
    return _get_series_gosu(_GOSU_GAME_SLUGS[norm_sport], team_a, team_b, start_time)


def is_finished(series_data: dict) -> bool:
    return series_data["status"] == "finished"


# Alias matching the "decided" terminology already used for series-shaped
# picks elsewhere in this bot (settracker.py/f5tracker.py).
is_decided = is_finished


def map_winners(series_data: dict) -> list[Optional[str]]:
    """Returns a list indexed by map number (index 0 = Map 1) - each entry
    "home"/"away" (oriented to series_data's own home/away) once that map is
    decided, else None. List length matches however many maps have actually
    been recorded so far by the provider - may be shorter than best_of if
    the series isn't finished yet."""
    ref = series_data["_ref"]
    if ref["provider"] == "hawklive":
        return _hawk_map_winners(series_data)
    return _gosu_map_winners(series_data)


def start_epoch(series_data: dict) -> Optional[float]:
    return series_data.get("start_epoch")


def page_url(series_data: dict) -> str:
    return series_data["page_url"]


def live_kill_count(series_data: dict) -> Optional[tuple[int, int]]:
    """(home, away) live in-map score for whichever map is currently being
    played - kills for Dota 2 (hawk.live), rounds for CS2 (Strafe.com,
    since neither hawk.live nor GosuGamers covers CS2 at all, and
    GosuGamers has no live in-map stat for either game). None for LoL/Mobile
    Legends - Strafe.com only covers CS2, and there's no other live in-map
    stat source wired up for either yet. Purely a supplementary display
    value, not used for grading anything - every market here settles on
    maps won. None while notstarted/finished, if the relevant provider has
    no live data for this map right now, or (Dota 2 only) if the series was
    only ever resolved via the GosuGamers fallback - confirmed live, a real
    Nigma Galaxy vs Vici Gaming series resolved that way crashed the whole
    /parlay-visible pick with a raw KeyError ("unexpected error: 'series'")
    instead of returning None, since _hawk_live_kill_count assumes
    hawk.live's own series shape unconditionally."""
    if series_data["status"] != "inprogress":
        return None
    if series_data["sport"] == "dota2":
        if series_data["_ref"]["provider"] != "hawklive":
            return None
        return _hawk_live_kill_count(series_data)
    if series_data["sport"] == "cs2":
        return _strafe_live_round_score(series_data["home_team"], series_data["away_team"])
    return None


def total_kills(series_data: dict) -> Optional[tuple[int, int]]:
    """(home, away) kill count summed across every map played so far in the
    series - Dota 2 only (hawk.live; CS2 exposes no kill data anywhere, see
    live_kill_count). Unlike live_kill_count this isn't gated on "inprogress"
    - it's meaningful (and needed for grading) once the series is finished
    too. None for CS2, or if a Dota 2 series was only ever resolved via the
    GosuGamers fallback (no hawk.live coverage, no per-map state data to sum)."""
    if series_data["sport"] != "dota2" or series_data["_ref"]["provider"] != "hawklive":
        return None
    return _hawk_total_kills(series_data)


def map_kills(series_data: dict, map_number: int) -> Optional[tuple[int, int]]:
    """(home, away) kill count for one specific map (1-indexed) in the
    series - Dota 2 only (hawk.live; same restriction as total_kills, see
    its own docstring). None if that map hasn't been played, has no kill
    state data yet, or the series was only ever resolved via the
    GosuGamers fallback (no per-map state data to read at all)."""
    if series_data["sport"] != "dota2" or series_data["_ref"]["provider"] != "hawklive":
        return None
    return _hawk_map_kills(series_data, map_number)


# --- grading -----------------------------------------------------------

def _oriented_scores(series_data: dict, picked_team: str) -> Optional[tuple[int, int]]:
    """(picked team's score, other side's score), or None if picked_team
    doesn't match either side."""
    home, away = series_data["home_team"], series_data["away_team"]
    if names_match(home, picked_team):
        return series_data["home_score"], series_data["away_score"]
    if names_match(away, picked_team):
        return series_data["away_score"], series_data["home_score"]
    return None


def grade_match_winner(series_data: dict, picked_team: str) -> Optional[str]:
    """Series (overall) moneyline - who wins the match outright."""
    if not is_decided(series_data):
        return None
    scores = _oriented_scores(series_data, picked_team)
    if scores is None:
        return None
    picked, other = scores
    if picked == other:
        return "push"
    return "won" if picked > other else "lost"


def grade_map_handicap(series_data: dict, picked_team: str, line: float) -> Optional[str]:
    """e.g. "PlayTime (-1.5) Map Handicap" - picked team's maps-won count,
    adjusted by line, versus the other side's raw maps-won count. Same
    shape as f5tracker.py's grade_f5_handicap elsewhere in this bot."""
    if not is_decided(series_data):
        return None
    scores = _oriented_scores(series_data, picked_team)
    if scores is None:
        return None
    picked, other = scores
    adjusted = picked + line
    if adjusted == other:
        return "push"
    return "won" if adjusted > other else "lost"


def grade_total_maps(series_data: dict, direction: str, line: float) -> Optional[str]:
    if not is_decided(series_data):
        return None
    total = series_data["home_score"] + series_data["away_score"]
    if total == line:
        return "push"
    if direction == "over":
        return "won" if total > line else "lost"
    return "won" if total < line else "lost"


def grade_total_kills(series_data: dict, direction: str, line: float) -> Optional[str]:
    """Match-combined kill total summed across every map played in the
    series - Dota 2 only (see total_kills). Graded only once the series
    itself is decided, not the moment the running total mathematically
    clears the line - unlike a single map's live score, kills keep
    accumulating for as long as more maps get played, so an early "Over"
    can't be locked in the way tracker.py's own early-win Over tagging
    does elsewhere in this bot. Returns None (never resolves) if kill data
    was never available for this series at all - see total_kills."""
    if not is_decided(series_data):
        return None
    kills = total_kills(series_data)
    if kills is None:
        return None
    total = kills[0] + kills[1]
    if total == line:
        return "push"
    return "won" if (total > line) == (direction == "over") else "lost"


def grade_team_total_kills(series_data: dict, picked_team: str, direction: str, line: float) -> Optional[str]:
    """One named team's own kill total summed across every map played -
    Dota 2 only. Same "wait for the series to end" reasoning as
    grade_total_kills."""
    if not is_decided(series_data):
        return None
    kills = total_kills(series_data)
    if kills is None:
        return None
    home, away = series_data["home_team"], series_data["away_team"]
    if names_match(home, picked_team):
        picked_kills = kills[0]
    elif names_match(away, picked_team):
        picked_kills = kills[1]
    else:
        return None
    if picked_kills == line:
        return "push"
    return "won" if (picked_kills > line) == (direction == "over") else "lost"


def grade_map_kills_handicap(series_data: dict, map_number: int, picked_team: str, line: float) -> Optional[str]:
    """Kills-handicap spread on one specific map's kill count (e.g. "Team X
    (-4.5) Map 1 Kills Handicap") - Dota 2 only, needs hawk.live's own
    per-map state data (see map_kills). Settles the moment that specific
    map itself finishes, not the whole series - unlike grade_total_kills/
    grade_team_total_kills (series-wide combined totals, which have to wait
    for the series to end since kills keep accumulating across maps), a
    single map's final kill count is fully known as soon as that map ends.
    Voided if the series finishes before that map was ever played, same
    convention as grade_map_winner."""
    winners = map_winners(series_data)
    if map_number < 1 or map_number > len(winners) or winners[map_number - 1] is None:
        return "void" if is_decided(series_data) else None
    home, away = series_data["home_team"], series_data["away_team"]
    if names_match(home, picked_team):
        picked_is_home = True
    elif names_match(away, picked_team):
        picked_is_home = False
    else:
        return None
    kills = map_kills(series_data, map_number)
    if kills is None:
        return None
    picked_kills, other_kills = kills if picked_is_home else (kills[1], kills[0])
    adjusted = picked_kills + line
    if adjusted == other_kills:
        return "push"
    return "won" if adjusted > other_kills else "lost"


def grade_map_total_kills(series_data: dict, map_number: int, direction: str, line: float) -> Optional[str]:
    """Combined Over/Under on one specific map's own kill count (both teams
    summed) - e.g. "Map 1 Total Kills Over 50.5". Dota 2 only, needs
    hawk.live's own per-map state data (see map_kills). Settles the moment
    that specific map itself finishes, not the whole series - same
    reasoning as grade_map_kills_handicap (unlike grade_total_kills's
    series-wide combined total, which has to wait for the series to end
    since kills keep accumulating across maps). Voided if the series
    finishes before that map was ever played, same convention as
    grade_map_winner/grade_map_kills_handicap."""
    winners = map_winners(series_data)
    if map_number < 1 or map_number > len(winners) or winners[map_number - 1] is None:
        return "void" if is_decided(series_data) else None
    kills = map_kills(series_data, map_number)
    if kills is None:
        return None
    total = kills[0] + kills[1]
    if total == line:
        return "push"
    return "won" if (total > line) == (direction == "over") else "lost"


def grade_win_at_least_one_map(series_data: dict, picked_team: str, direction: str = "yes") -> Optional[str]:
    """"Wins at Least One Map" (direction="yes") - the underdog isn't swept
    0-N. direction="no" is the inverse - "does NOT win at least one map",
    i.e. the picked team gets swept.

    Both outcomes are mathematically locked in the moment the picked team's
    map count reaches 1 - it can't be un-won. That's real, permanent
    settlement info the moment it happens, same as an Over line elsewhere in
    this bot - but unlike an Over line, this market only ever needs one
    more map to know it, so grading it here the instant it locks in would
    end tracking (and bump the card to "Final") while the series is often
    still ongoing (confirmed live: a still-live best-of-3 got bumped Final
    at 1-0). So this waits for the series to actually finish either way -
    esportstracker.build_embed applies the early "can't lose/win this leg
    anymore" cosmetic tag instead, the same way it already does for an
    early-cleared Over line, while polling stays live until the real
    result."""
    if not is_decided(series_data):
        return None
    scores = _oriented_scores(series_data, picked_team)
    if scores is None:
        return None
    picked, _other = scores
    if direction == "no":
        return "lost" if picked >= 1 else "won"
    return "won" if picked >= 1 else "lost"


def grade_correct_score(series_data: dict, picked_team: str, picked_maps: int, other_maps: int) -> Optional[str]:
    """Exact series score (e.g. "Team X to win 2-0") - picked_maps/
    other_maps are the picked team's own maps-won and the opponent's,
    exactly as a bettor would phrase it, not raw home/away."""
    if not is_decided(series_data):
        return None
    scores = _oriented_scores(series_data, picked_team)
    if scores is None:
        return None
    picked, other = scores
    return "won" if picked == picked_maps and other == other_maps else "lost"


def grade_map_winner(series_data: dict, map_number: int, picked_team: str) -> Optional[str]:
    """Individual Map N winner - e.g. "Map 2 Winner". Voided rather than
    left stuck pending forever if the series ends without that map ever
    being played (e.g. "Map 3 Winner" picked on a Bo3 that finished 2-0 -
    there never was a map 3)."""
    home, away = series_data["home_team"], series_data["away_team"]
    if names_match(home, picked_team):
        picked_side = "home"
    elif names_match(away, picked_team):
        picked_side = "away"
    else:
        return None
    winners = map_winners(series_data)
    if map_number < 1 or map_number > len(winners) or winners[map_number - 1] is None:
        return "void" if is_decided(series_data) else None
    winner_side = winners[map_number - 1]
    return "won" if winner_side == picked_side else "lost"


def grade_match_and_map_winner(series_data: dict, map_number: int, picked_team: str) -> Optional[str]:
    """Correlated same-game pick requiring BOTH conditions to be true - the
    picked team wins the map AND wins the series outright (e.g. a
    sportsbook's "Match Winner / Map 2 Winner" same-game combo). A failed
    map result kills the whole pick immediately regardless of how the
    series ends (can't be undone); otherwise it can only resolve once the
    series itself is decided, since the match-winner half is never known
    before then. A voided map (never played) voids the whole pick, same
    reasoning as a voided leg elsewhere in this bot - the combo can't be
    evaluated as bet if half of it never happened."""
    map_result = grade_map_winner(series_data, map_number, picked_team)
    if map_result == "lost":
        return "lost"
    match_result = grade_match_winner(series_data, picked_team)
    if match_result is None:
        return None
    if match_result != "won":
        return "lost"
    if map_result == "won":
        return "won"
    if map_result == "void":
        return "void"
    return None

#!/usr/bin/env python3
"""
BoxingScene.com boxing fight data - moneyline only for now, no round
totals/method of victory (not asked for). Scraped via curl (same TLS-
fingerprint-avoidance reasoning as esports.py's hawk.live/GosuGamers
calls), not requests.

Both the /schedule and /results pages stream their data as Next.js App
Router React Server Component "flight" payloads - the exact same format
GosuGamers already uses (see esports.py's own module docstring), just
keyed under a repeated "event" object (each carrying a nested "fights"
list) rather than GosuGamers' single "defaultMatches" list - so this
reuses esports.py's own _gosu_joined_flight_text/_extract_json_after_key
helpers directly (confirmed live against real fetched pages) rather than
reimplementing that parsing from scratch.

BoxRec was evaluated as an alternative source and rejected - it sits
behind a Cloudflare JS challenge (confirmed live: a plain curl request
gets a "Just a moment..." challenge page, not real content) that this
bot's curl-based scraping approach can't pass.

Unlike ESPN's UFC scoreboard, there's no live round/clock data available
here at all - a fight is either "notstarted" (found on the schedule page,
not yet in results) or "finished" (found on the results page, with a
winning_fighter_id - or no winning_fighter_id at all for a draw/no-
contest, graded as a push same as espn_ufc.grade_ufc_moneyline's
equivalent). No inprogress state is ever reported.
"""

import subprocess
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import esports

_SCHEDULE_URL = "https://www.boxingscene.com/schedule"
_RESULTS_URL = "https://www.boxingscene.com/results"
_EVENT_MARKER = '\\"event\\":'
_MAX_EVENTS_PER_PAGE = 200
_LIST_CACHE_SECONDS = 60
_REQUEST_TIMEOUT = 10

# event_date/event_time are always reported in US Eastern (confirmed live -
# "EST" even for an August date, so the literal timezone string BoxingScene
# sends isn't trustworthy for DST - a real IANA zone handles that correctly
# regardless of what label the site itself uses).
_EASTERN = ZoneInfo("America/New_York")

_list_cache: dict = {}  # url -> (events, fetched_at)


class BoxingError(Exception):
    pass


def _curl_text(url: str) -> str:
    try:
        result = subprocess.run(
            ["curl", "-sL", "-A", esports.USER_AGENT, url],
            capture_output=True, timeout=_REQUEST_TIMEOUT, check=False,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
        raise BoxingError(str(e)) from e
    text = result.stdout.decode("utf-8", errors="replace")
    if not text:
        raise BoxingError(f"Empty response from {url}")
    return text


def _events_for(url: str) -> list[dict]:
    cached = _list_cache.get(url)
    if cached and time.time() - cached[1] < _LIST_CACHE_SECONDS:
        return cached[0]
    html = _curl_text(url)
    joined = esports._gosu_joined_flight_text(html)
    events = []
    from_index = 0
    for _ in range(_MAX_EVENTS_PER_PAGE):
        idx = joined.find(_EVENT_MARKER, from_index)
        if idx == -1:
            break
        candidate = esports._extract_json_after_key(joined, "event", idx)
        from_index = idx + len(_EVENT_MARKER)
        if candidate and "fights" in candidate:
            events.append(candidate)
    _list_cache[url] = (events, time.time())
    return events


def _event_start_epoch(event: dict) -> Optional[float]:
    date_str = event.get("event_date")
    if not date_str:
        return None
    time_str = event.get("event_time") or "00:00:00"
    try:
        naive = datetime.fromisoformat(f"{date_str}T{time_str}")
    except ValueError:
        return None
    return naive.replace(tzinfo=_EASTERN).timestamp()


def find_boxing_fight(fighter_name: str) -> Optional[dict]:
    """Searches results first (a decided fight is more useful to surface
    than an unrelated future one for the same fighter), then the schedule
    page for an upcoming fight. Every lookup here (including a tracker's
    own repeated polling) re-resolves by fighter name each time, same
    "no stable refetch-by-id" architecture as esports.py's own
    get_series - neither BoxingScene page exposes one.

    Returns a unified dict (independent of which page it came from):
    sport "boxing", fighter1_name/id, fighter2_name/id, event_name,
    start_epoch, status ("notstarted"/"finished"), page_url, and - once
    finished - winning_fighter_id (None means a draw/no-contest) and
    decision. Or None if no fight anywhere names this fighter."""
    for url, status in ((_RESULTS_URL, "finished"), (_SCHEDULE_URL, "notstarted")):
        try:
            events = _events_for(url)
        except BoxingError:
            continue
        for event in events:
            for fight in event.get("fights", []):
                if not (
                    esports.names_match(fight.get("fighter1_name"), fighter_name)
                    or esports.names_match(fight.get("fighter2_name"), fighter_name)
                ):
                    continue
                return {
                    "sport": "boxing",
                    "fight_id": fight.get("id"),
                    "fighter1_name": fight.get("fighter1_name"),
                    "fighter1_id": fight.get("fighter1_id"),
                    "fighter2_name": fight.get("fighter2_name"),
                    "fighter2_id": fight.get("fighter2_id"),
                    "event_name": event.get("name"),
                    "start_epoch": _event_start_epoch(event),
                    "status": status,
                    "page_url": f"https://www.boxingscene.com/events/{event.get('slug')}" if event.get("slug") else url,
                    "winning_fighter_id": fight.get("winning_fighter_id"),
                    "decision": fight.get("decision_description"),
                }
    return None


def is_finished(fight_data: dict) -> bool:
    return fight_data["status"] == "finished"


def start_epoch(fight_data: dict) -> Optional[float]:
    return fight_data.get("start_epoch")


def page_url(fight_data: dict) -> str:
    return fight_data["page_url"]


def grade_boxing_moneyline(fight_data: dict, picked_fighter: str) -> Optional[str]:
    """Grades a fight-winner pick. Returns None if not decided yet. A
    completed fight with no winning_fighter_id at all is a draw or
    no-contest - treated as a push, same "don't guess a side" reasoning
    as espn_ufc.grade_ufc_moneyline elsewhere in this bot."""
    if not is_finished(fight_data):
        return None
    winner_id = fight_data.get("winning_fighter_id")
    if esports.names_match(fight_data.get("fighter1_name"), picked_fighter):
        picked_id = fight_data.get("fighter1_id")
    elif esports.names_match(fight_data.get("fighter2_name"), picked_fighter):
        picked_id = fight_data.get("fighter2_id")
    else:
        return None
    if winner_id is None:
        return "push"
    return "won" if picked_id == winner_id else "lost"

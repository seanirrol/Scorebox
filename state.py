#!/usr/bin/env python3
"""
Tiny JSON-file persistence so /track and /playerprops survive a bot restart.

Both trackers keep their live state in memory only (an asyncio.Task per
active tracking loop) - a restart wipes that, leaving already-posted Discord
messages stale forever with nothing left to update or delete them. This
module just mirrors each tracker's "what's currently active" dict to disk on
start/stop, so bot.py can read it back on startup and either resume tracking
or clean up what can't be resumed.
"""

import json
import os
from typing import Any

_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKS_FILE = os.path.join(_DIR, "tracks_state.json")
PROPS_FILE = os.path.join(_DIR, "props_state.json")
INNINGS_FILE = os.path.join(_DIR, "innings_state.json")
F5_FILE = os.path.join(_DIR, "f5_state.json")
INNING1_FILE = os.path.join(_DIR, "inning1_state.json")
SET1_FILE = os.path.join(_DIR, "set1_state.json")
TENNIS_PROPS_FILE = os.path.join(_DIR, "tennis_props_state.json")
PENDING_DELETE_FILE = os.path.join(_DIR, "pending_delete_state.json")
UFC_FILE = os.path.join(_DIR, "ufc_state.json")
SOCCER_PROPS_FILE = os.path.join(_DIR, "soccer_props_state.json")
PARLAY_FILE = os.path.join(_DIR, "parlay_state.json")
ESPORTS_FILE = os.path.join(_DIR, "esports_state.json")
DAILY_LOG_FILE = os.path.join(_DIR, "daily_log_state.json")
PENDING_SOCCER_PROPS_FILE = os.path.join(_DIR, "pending_soccer_props_state.json")
HALF_FILE = os.path.join(_DIR, "half_state.json")
HTFT_FILE = os.path.join(_DIR, "htft_state.json")
PENDING_TRACK_FILE = os.path.join(_DIR, "pending_track_state.json")
PENDING_AUTO_FILE = os.path.join(_DIR, "pending_auto_state.json")
LAST_PERFORMANCE_POST_FILE = os.path.join(_DIR, "last_performance_post_state.json")
WINLOSSGRAPH_OVERRIDES_FILE = os.path.join(_DIR, "winlossgraph_overrides_state.json")
BOXING_FILE = os.path.join(_DIR, "boxing_state.json")
KBO_PROPS_FILE = os.path.join(_DIR, "kbo_props_state.json")
DOUBLE_CHANCE_FILE = os.path.join(_DIR, "double_chance_state.json")


def _load(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: str, data: dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_tracks() -> dict[str, Any]:
    return _load(TRACKS_FILE)


def save_tracks(data: dict[str, Any]):
    _save(TRACKS_FILE, data)


def load_props() -> dict[str, Any]:
    return _load(PROPS_FILE)


def save_props(data: dict[str, Any]):
    _save(PROPS_FILE, data)


def load_innings() -> dict[str, Any]:
    return _load(INNINGS_FILE)


def save_innings(data: dict[str, Any]):
    _save(INNINGS_FILE, data)


def load_f5() -> dict[str, Any]:
    return _load(F5_FILE)


def save_f5(data: dict[str, Any]):
    _save(F5_FILE, data)


def load_inning1() -> dict[str, Any]:
    return _load(INNING1_FILE)


def save_inning1(data: dict[str, Any]):
    _save(INNING1_FILE, data)


def load_set1() -> dict[str, Any]:
    return _load(SET1_FILE)


def save_set1(data: dict[str, Any]):
    _save(SET1_FILE, data)


def load_tennis_props() -> dict[str, Any]:
    return _load(TENNIS_PROPS_FILE)


def save_tennis_props(data: dict[str, Any]):
    _save(TENNIS_PROPS_FILE, data)


def load_pending_deletes() -> dict[str, Any]:
    return _load(PENDING_DELETE_FILE)


def save_pending_deletes(data: dict[str, Any]):
    _save(PENDING_DELETE_FILE, data)


def load_ufc() -> dict[str, Any]:
    return _load(UFC_FILE)


def save_ufc(data: dict[str, Any]):
    _save(UFC_FILE, data)


def load_boxing() -> dict[str, Any]:
    return _load(BOXING_FILE)


def save_boxing(data: dict[str, Any]):
    _save(BOXING_FILE, data)


def load_kbo_props() -> dict[str, Any]:
    return _load(KBO_PROPS_FILE)


def save_kbo_props(data: dict[str, Any]):
    _save(KBO_PROPS_FILE, data)


def load_soccer_props() -> dict[str, Any]:
    return _load(SOCCER_PROPS_FILE)


def save_soccer_props(data: dict[str, Any]):
    _save(SOCCER_PROPS_FILE, data)


def load_parlays() -> dict[str, Any]:
    return _load(PARLAY_FILE)


def save_parlays(data: dict[str, Any]):
    _save(PARLAY_FILE, data)


def load_esports() -> dict[str, Any]:
    return _load(ESPORTS_FILE)


def save_esports(data: dict[str, Any]):
    _save(ESPORTS_FILE, data)


def load_daily_log() -> dict[str, Any]:
    return _load(DAILY_LOG_FILE)


def save_daily_log(data: dict[str, Any]):
    _save(DAILY_LOG_FILE, data)


def load_pending_soccer_props() -> dict[str, Any]:
    return _load(PENDING_SOCCER_PROPS_FILE)


def save_pending_soccer_props(data: dict[str, Any]):
    _save(PENDING_SOCCER_PROPS_FILE, data)


def load_half() -> dict[str, Any]:
    return _load(HALF_FILE)


def save_half(data: dict[str, Any]):
    _save(HALF_FILE, data)


def load_htft() -> dict[str, Any]:
    return _load(HTFT_FILE)


def save_htft(data: dict[str, Any]):
    _save(HTFT_FILE, data)


def load_double_chance() -> dict[str, Any]:
    return _load(DOUBLE_CHANCE_FILE)


def save_double_chance(data: dict[str, Any]):
    _save(DOUBLE_CHANCE_FILE, data)


def load_pending_track() -> dict[str, Any]:
    return _load(PENDING_TRACK_FILE)


def save_pending_track(data: dict[str, Any]):
    _save(PENDING_TRACK_FILE, data)


def load_pending_auto() -> dict[str, Any]:
    return _load(PENDING_AUTO_FILE)


def save_pending_auto(data: dict[str, Any]):
    _save(PENDING_AUTO_FILE, data)


def load_last_performance_post() -> dict[str, Any]:
    return _load(LAST_PERFORMANCE_POST_FILE)


def save_last_performance_post(data: dict[str, Any]):
    _save(LAST_PERFORMANCE_POST_FILE, data)


def load_winlossgraph_overrides() -> dict[str, Any]:
    return _load(WINLOSSGRAPH_OVERRIDES_FILE)


def save_winlossgraph_overrides(data: dict[str, Any]):
    _save(WINLOSSGRAPH_OVERRIDES_FILE, data)

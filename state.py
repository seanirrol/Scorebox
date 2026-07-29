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

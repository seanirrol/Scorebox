#!/usr/bin/env python3
"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# How often (seconds) a tracked match's embed is refreshed.
UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "30"))

# Safety cap so a match that never reports "finished" doesn't poll forever.
MAX_TRACK_HOURS = int(os.environ.get("MAX_TRACK_HOURS", "6"))

# If set, all commands are restricted to these channels only (comma-separated,
# empty = no restriction).
_allowed_channel_ids = os.environ.get("ALLOWED_CHANNEL_ID", "").strip()
ALLOWED_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _allowed_channel_ids.split(",") if cid.strip()}
    if _allowed_channel_ids
    else None
)

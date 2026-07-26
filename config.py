#!/usr/bin/env python3
"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# How often (seconds) a tracked match's embed is refreshed.
UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "60"))

# Safety cap so a match that never reports "finished" doesn't poll forever.
MAX_TRACK_HOURS = int(os.environ.get("MAX_TRACK_HOURS", "6"))

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

# Maps each picks channel to its own target scores channel - messages posted
# in a mapped channel are auto-parsed for picks, which get tracked
# automatically (via the same logic as /track/.../playerprops) and posted
# into that specific target channel. The bot can post cross-server as long
# as it's a member of both, so this also covers e.g. a test-server picks
# channel that posts into the live server's scores channel, alongside a
# separate pair fully contained within the test server. Format (comma-
# separated "picks_id:target_id" pairs):
#   PICKS_CHANNEL_MAP=111111111111111111:222222222222222222,333...:444...
_picks_channel_map_raw = os.environ.get("PICKS_CHANNEL_MAP", "").strip()
PICKS_CHANNEL_MAP: dict[int, int] = {}
for _pair in _picks_channel_map_raw.split(","):
    _pair = _pair.strip()
    if not _pair:
        continue
    _picks_id, _, _target_id = _pair.partition(":")
    if _picks_id.strip() and _target_id.strip():
        PICKS_CHANNEL_MAP[int(_picks_id.strip())] = int(_target_id.strip())

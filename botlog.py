#!/usr/bin/env python3
"""
Posts an operational activity log to a fixed Discord channel - every
auto-track attempt (success or failure, with a reason), every manual
command invocation (who ran it, when, with what params), and every untrack
event (manual /untrack, a 🗑️-reaction delete, or an automatic stop after
repeated ESPN/365scores/Sofascore/Discord failures) - so activity can be
audited from Discord itself instead of digging through journalctl.

init(client) must be called once at startup (from bot.py's on_ready) before
event() calls actually send anything - calls made before that are silently
dropped rather than erroring, since some code paths (e.g. picks-channel
message handling) could theoretically fire before on_ready completes.
"""

import asyncio
import logging
import time
from typing import Optional

import discord

import throttle

log = logging.getLogger("scorebox.botlog")

LOG_CHANNEL_ID = 1532686196948336872

_client: Optional[discord.Client] = None


def init(client: discord.Client):
    global _client
    _client = client


def event(message: str):
    """Fire-and-forget - schedules the log post as a background task so
    callers (including tight tracker loops and command handlers) never block
    on Discord's response. Safe to call before init() (no-op) or if the log
    channel is unreachable (logged locally, never raises)."""
    if _client is None:
        return
    asyncio.create_task(_send(message))


async def _send(message: str):
    try:
        channel = _client.get_channel(LOG_CHANNEL_ID) or await _client.fetch_channel(LOG_CHANNEL_ID)
    except discord.HTTPException as e:
        log.warning("Failed to reach log channel: %s", e)
        return
    text = f"<t:{int(time.time())}:T> {message}"
    try:
        await throttle.run(LOG_CHANNEL_ID, lambda: channel.send(text))
    except discord.HTTPException as e:
        log.warning("Failed to write to log channel: %s", e)

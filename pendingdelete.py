#!/usr/bin/env python3
"""
Every tracker (tracker.py, proptracker.py, inningtracker.py, f5tracker.py,
inning1tracker.py, settracker.py, tennispropstracker.py) waits 24h after a
pick is graded before deleting its card, via a plain `asyncio.sleep`. That
sleep is in-memory only - a bot restart mid-wait killed the task without a
trace of how much of the 24h had already elapsed, so resuming just started a
fresh 24h wait from scratch. Any card unlucky enough to finish shortly before
a deploy could effectively never auto-delete if restarts kept happening
within its window.

This module persists a `delete_at` timestamp the moment a pick is graded, so
resume_all() (called once from bot.py's on_ready) can pick up exactly where
each wait left off - deleting immediately if the window already elapsed
while the bot was down, or sleeping only the remaining time otherwise.
"""

import asyncio
import logging
import time
from typing import Optional

import discord

import state
import throttle

log = logging.getLogger("scorebox.pendingdelete")

DELETE_AFTER_SECONDS = 24 * 3600


def _persist(channel_id: int, message_id: int, delete_at: float, label: str):
    data = state.load_pending_deletes()
    data[str(message_id)] = {
        "channel_id": channel_id, "message_id": message_id, "delete_at": delete_at, "label": label,
    }
    state.save_pending_deletes(data)


def list_pending() -> list[dict]:
    """Every card currently waiting out its post-result delete window,
    across all channels - backs the /pending command."""
    return list(state.load_pending_deletes().values())


def _forget(message_id: int):
    data = state.load_pending_deletes()
    data.pop(str(message_id), None)
    state.save_pending_deletes(data)


async def _delete(channel_id: int, message_id: int, delete_at: float, message: Optional[discord.Message], client: Optional[discord.Client]):
    await asyncio.sleep(max(delete_at - time.time(), 0))
    _forget(message_id)
    try:
        if message is None:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        await throttle.run(channel_id, message.delete)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("Failed to delete pending message %s: %s", message_id, e)


def start(channel_id: int, message: discord.Message, label: str, delete_at: Optional[float] = None):
    """Called by a tracker right after grading a pick, in place of its own
    sleep-then-delete. delete_at defaults to now + the standard 24h window -
    only overridden when resuming (see resume_all). label is whatever the
    tracker's own embed description said (e.g. "Kia Tigers F5 +0.5") - kept
    only for the /pending command's display, since a graded card's original
    tracker-specific state is discarded once it gets here."""
    delete_at = delete_at if delete_at is not None else time.time() + DELETE_AFTER_SECONDS
    _persist(channel_id, message.id, delete_at, label)
    asyncio.create_task(_delete(channel_id, message.id, delete_at, message, None))


async def resume_all(client: discord.Client):
    """Called once from on_ready. Re-arms every pending delete with its
    original delete_at, so restarts no longer reset the 24h window."""
    for entry in list(state.load_pending_deletes().values()):
        asyncio.create_task(_delete(entry["channel_id"], entry["message_id"], entry["delete_at"], None, client))
        log.info("Resumed pending delete for message %s in channel %s", entry["message_id"], entry["channel_id"])


async def delete_now(client: discord.Client, entry: dict) -> bool:
    """Deletes a queued card immediately instead of waiting out its timer -
    backs /pending's delete-now dropdown. The original scheduled _delete()
    task (still sleeping in the background) finds the entry already gone
    when it eventually wakes and just no-ops safely (message.delete() on an
    already-deleted message raises NotFound, which it already catches)."""
    _forget(entry["message_id"])
    try:
        channel = client.get_channel(entry["channel_id"]) or await client.fetch_channel(entry["channel_id"])
        message = await channel.fetch_message(entry["message_id"])
        await throttle.run(entry["channel_id"], message.delete)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("Failed to delete pending message %s on demand: %s", entry["message_id"], e)
        return False

#!/usr/bin/env python3
"""Manages background tasks that keep a live-score embed updated."""

import asyncio
import logging
import time

import discord

import config
import scores365

log = logging.getLogger("scorebox.tracker")

# Keyed by f"{channel_id}:{game_id}" -> asyncio.Task
_active_tracks: dict[str, asyncio.Task] = {}


def build_embed(game: dict) -> discord.Embed:
    home = (game.get("homeCompetitor") or {}).get("name", "?")
    away = (game.get("awayCompetitor") or {}).get("name", "?")
    finished = scores365.is_finished(game)

    embed = discord.Embed(
        title=f"{home} vs {away}",
        description=scores365.format_score_line(game),
        color=0x95A5A6 if finished else 0x2ECC71,
    )
    embed.add_field(name="Status", value=scores365.status_line(game), inline=True)
    competition = game.get("competitionDisplayName")
    if competition:
        embed.add_field(name="Competition", value=competition, inline=True)
    embed.set_footer(text="Scorebox • data via 365scores")
    return embed


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active_tracks


def list_tracked(channel_id: int) -> list[str]:
    prefix = f"{channel_id}:"
    return [key.split(":", 1)[1] for key in _active_tracks if key.startswith(prefix)]


def stop_tracking(channel_id: int, game_id) -> bool:
    key = track_key(channel_id, game_id)
    task = _active_tracks.pop(key, None)
    if task:
        task.cancel()
        return True
    return False


async def _track_loop(message: discord.Message, sport_id: int, game_id, channel_id: int):
    key = track_key(channel_id, game_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
            if not game:
                break

            try:
                await message.edit(embed=build_embed(game))
            except discord.HTTPException as e:
                log.warning("Failed to edit tracking message: %s", e)
                break

            if scores365.is_finished(game):
                break
    except asyncio.CancelledError:
        raise
    finally:
        _active_tracks.pop(key, None)


def start_tracking(message: discord.Message, sport_id: int, game: dict, channel_id: int):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active_tracks:
        return
    task = asyncio.create_task(_track_loop(message, sport_id, game_id, channel_id))
    _active_tracks[key] = task

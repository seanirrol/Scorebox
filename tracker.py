#!/usr/bin/env python3
"""Manages background tasks that keep a live-score embed updated."""

import asyncio
import io
import logging
import time
from typing import Optional

import discord

import config
import scoreimage
import scores365
import state

log = logging.getLogger("scorebox.tracker")

# How long a finished match's final score stays posted before auto-deleting.
POST_MATCH_DELETE_SECONDS = 24 * 3600

# Keyed by f"{channel_id}:{game_id}" -> asyncio.Task
_active_tracks: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
_message_owners: dict[int, tuple] = {}

_STATUS_COLOR = {
    "notstarted": 0x3498DB,  # blue
    "inprogress": 0xE74C3C,  # red
    "finished": 0x2ECC71,  # green
}


async def build_embed(game: dict, sport_id: Optional[int] = None) -> tuple[discord.Embed, discord.File]:
    """Returns (embed, file) - the score image must be sent/edited alongside the embed."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"))

    embed = discord.Embed(color=_STATUS_COLOR.get(status, 0x95A5A6))

    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    period_text = scores365.status_line(game, sport_id)

    # Each row is [main score, smaller sub-tier(s)...] left-to-right, main
    # score first (rendered biggest/boldest, on the left) - tennis gets
    # sets+games+points, volleyball gets sets+current-set score, everything
    # else just gets its plain score.
    main_scores = scores365.main_scores(game)
    home_cols: list[str] = [scores365.fmt_score(main_scores[0]) if main_scores else "-"]
    away_cols: list[str] = [scores365.fmt_score(main_scores[1]) if main_scores else "-"]

    set_score = scores365.current_set_score(game, sport_id)
    if set_score:
        home_cols.append(scores365.fmt_score(set_score[0]))
        away_cols.append(scores365.fmt_score(set_score[1]))

    if sport_id == scores365.SPORT_IDS["tennis"] and status == "inprogress":
        points = scores365.tennis_current_game_points(game)
        if points:
            home_cols.append(scores365.tennis_point_label(points[0]))
            away_cols.append(scores365.tennis_point_label(points[1]))

    home_name = home_competitor.get("name", "?")
    away_name = away_competitor.get("name", "?")
    home_logo_url = scores365.competitor_logo_url(home_competitor)
    away_logo_url = scores365.competitor_logo_url(away_competitor)

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, home_logo_url, away_logo_url, home_cols, away_cols, period_text, status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")

    embed.set_footer(text="Scorebox • data via 365scores")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


def register_message(message_id: int, channel_id: int, game_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active_tracks


def list_tracked(channel_id: int) -> list[str]:
    prefix = f"{channel_id}:"
    return [key.split(":", 1)[1] for key in _active_tracks if key.startswith(prefix)]


def _persist(channel_id: int, game_id, message_id: int, sport_id, owner_id: int):
    data = state.load_tracks()
    data[track_key(channel_id, game_id)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id,
    }
    state.save_tracks(data)


def _forget(channel_id: int, game_id):
    data = state.load_tracks()
    data.pop(track_key(channel_id, game_id), None)
    state.save_tracks(data)


def stop_tracking(channel_id: int, game_id) -> bool:
    key = track_key(channel_id, game_id)
    task = _active_tracks.pop(key, None)
    _forget(channel_id, game_id)
    for message_id, (c_id, g_id, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id:
            _message_owners.pop(message_id, None)
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

            embed, file = await build_embed(game, sport_id)
            try:
                await message.edit(embed=embed, attachments=[file])
            except discord.HTTPException as e:
                log.warning("Failed to edit tracking message: %s", e)
                break

            if scores365.is_finished(game):
                try:
                    await message.unpin()
                except discord.HTTPException as e:
                    log.warning("Failed to unpin finished tracking message: %s", e)
                await asyncio.sleep(POST_MATCH_DELETE_SECONDS)
                try:
                    await message.delete()
                except discord.HTTPException as e:
                    log.warning("Failed to delete finished tracking message: %s", e)
                break
    except asyncio.CancelledError:
        raise
    finally:
        _active_tracks.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id)


def start_tracking(message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active_tracks:
        return
    task = asyncio.create_task(_track_loop(message, sport_id, game_id, channel_id))
    _active_tracks[key] = task
    register_message(message.id, channel_id, game_id, owner_id)
    _persist(channel_id, game_id, message.id, sport_id, owner_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_tracks().values()):
        channel_id, game_id, message_id, sport_id = (
            entry["channel_id"], entry["game_id"], entry["message_id"], entry["sport_id"]
        )
        owner_id = entry.get("owner_id")
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, game_id)
            continue

        game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
        if not game or scores365.is_finished(game):
            _forget(channel_id, game_id)
            continue

        start_tracking(message, sport_id, game, channel_id, owner_id)
        log.info("Resumed tracking game %s in channel %s", game_id, channel_id)

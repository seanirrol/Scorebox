#!/usr/bin/env python3
"""
Manages background tasks that keep a live player-stat embed updated
(e.g. "Franck Nyembo - Points, currently 17").

Separate from tracker.py (team score tracking, backed by 365scores) because
this is backed by a different provider (Sofascore) with its own player
resolution and stat-extraction logic - see sofascore.py. Uses
scoreimage.render_player_card - team label, player photo, name, stat label,
then the value - no opponent shown, unlike /track's two-team card.
"""

import asyncio
import io
import logging
import random
import time
from typing import Optional

import discord

import config
import scoreimage
import sofascore
import state

log = logging.getLogger("scorebox.proptracker")

POST_MATCH_DELETE_SECONDS = 24 * 3600

# Tolerate this many consecutive "event not found" polls before giving up -
# guards against a transient Sofascore hiccup silently killing tracking for
# an event that's still very much alive.
MAX_CONSECUTIVE_MISSES = 3

_active_props: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, entity_id, stat_key, owner_id) - lets
# the reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_STATUS_COLOR = {
    "notstarted": 0x3498DB,  # blue
    "inprogress": 0xE74C3C,  # red
    "finished": 0x2ECC71,  # green
}


def prop_key(channel_id: int, event_id, entity_id: int, stat_key: str) -> str:
    return f"{channel_id}:{event_id}:{entity_id}:{stat_key}"


def is_tracked(channel_id: int, event_id, entity_id: int, stat_key: str) -> bool:
    return prop_key(channel_id, event_id, entity_id, stat_key) in _active_props


def register_message(message_id: int, channel_id: int, event_id, entity_id: int, stat_key: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, entity_id, stat_key, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(channel_id: int, event_id, entity_id: int, stat_key: str, message_id: int, team_id, is_tennis, sport, stat_label, player_name, owner_id: int):
    data = state.load_props()
    data[prop_key(channel_id, event_id, entity_id, stat_key)] = {
        "channel_id": channel_id, "event_id": event_id, "entity_id": entity_id, "stat_key": stat_key,
        "message_id": message_id, "team_id": team_id, "is_tennis": is_tennis, "sport": sport,
        "stat_label": stat_label, "player_name": player_name, "owner_id": owner_id,
    }
    state.save_props(data)


def _forget(channel_id: int, event_id, entity_id: int, stat_key: str):
    data = state.load_props()
    data.pop(prop_key(channel_id, event_id, entity_id, stat_key), None)
    state.save_props(data)


def stop_tracking(channel_id: int, event_id, entity_id: int, stat_key: str) -> bool:
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    task = _active_props.pop(key, None)
    _forget(channel_id, event_id, entity_id, stat_key)
    for message_id, (c_id, e_id, ent_id, s_key, _owner) in list(_message_owners.items()):
        if c_id == channel_id and e_id == event_id and ent_id == entity_id and s_key == stat_key:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _fmt_value(v) -> str:
    if v is None:
        return "-"
    return str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() else str(v)


async def build_embed(
    player_name: str,
    entity_id: int,
    is_tennis: bool,
    team_id: int,
    sport: str,
    stat_label: str,
    current_value,
    is_home,
    event: dict,
) -> tuple[discord.Embed, discord.File]:
    """
    Returns (embed, file) - mirrors tracker.build_embed's shape. `is_home`
    should come from sofascore.get_stat_value's own roster-based detection;
    team_id is only used as a fallback when that's None (e.g. lineups aren't
    posted yet for a match that hasn't started) - the player's separately
    cached team affiliation can be stale, confirmed live.
    """
    home_team = event.get("homeTeam", {})
    away_team = event.get("awayTeam", {})
    status_type = (event.get("status") or {}).get("type", "notstarted")
    if is_home is None:
        is_home = home_team.get("id") == team_id

    team_name = (home_team if is_home else away_team).get("name", "?")
    # Tennis players are their own "team" entity on Sofascore, so their photo
    # comes from the team-image endpoint (confirmed live: a real headshot,
    # not a placeholder); every other sport uses the dedicated player-image one.
    photo_url = sofascore.team_logo_url(entity_id) if is_tennis else sofascore.player_photo_url(entity_id)
    status_label = sofascore.match_status_text(event, sport)

    sport_label = sofascore.SPORT_DISPLAY_LABELS.get(sport, sport.title())
    tournament_name = (event.get("tournament") or {}).get("name")
    sport_tournament = f"{sport_label} • {tournament_name}" if tournament_name else sport_label
    matchup = f"{home_team.get('name', '?')} v {away_team.get('name', '?')}"

    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        sport_tournament, matchup, team_name, photo_url, player_name, stat_label,
        _fmt_value(current_value), status_type, status_label,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=_STATUS_COLOR.get(status_type, 0x95A5A6))
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via Sofascore")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message,
    channel_id: int,
    event_id,
    entity_id: int,
    team_id: int,
    is_tennis: bool,
    sport: str,
    stat_key: str,
    stat_label: str,
    player_name: str,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    # Random head start so many trackers started around the same moment don't
    # all land on the same wall-clock instant every cycle and pile up against
    # Discord's per-channel edit rate limit together.
    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(sofascore.get_event, event_id)
            if not event:
                consecutive_misses += 1
                log.warning(
                    "Event %s not found on Sofascore (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    break
                continue
            consecutive_misses = 0
            current_value, is_home = await asyncio.to_thread(
                sofascore.get_stat_value, event, entity_id, is_tennis, stat_key
            )

            embed, file = await build_embed(
                player_name, entity_id, is_tennis, team_id, sport, stat_label, current_value, is_home, event
            )
            try:
                await message.edit(embed=embed, attachments=[file])
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit prop tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    break
                continue

            if sofascore.is_finished(event):
                await asyncio.sleep(POST_MATCH_DELETE_SECONDS)
                try:
                    await message.delete()
                except discord.HTTPException as e:
                    log.warning("Failed to delete finished prop tracking message: %s", e)
                break
    except asyncio.CancelledError:
        raise
    finally:
        _active_props.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, event_id, entity_id, stat_key)


def start_tracking(
    message: discord.Message,
    channel_id: int,
    event_id,
    entity_id: int,
    team_id: int,
    is_tennis: bool,
    sport: str,
    stat_key: str,
    stat_label: str,
    player_name: str,
    owner_id: int,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    if key in _active_props:
        return
    task = asyncio.create_task(
        _track_loop(message, channel_id, event_id, entity_id, team_id, is_tennis, sport, stat_key, stat_label, player_name)
    )
    _active_props[key] = task
    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
    _persist(channel_id, event_id, entity_id, stat_key, message.id, team_id, is_tennis, sport, stat_label, player_name, owner_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone or already finished -
    cleans up instead.
    """
    for entry in list(state.load_props().values()):
        channel_id, event_id, entity_id, stat_key = (
            entry["channel_id"], entry["event_id"], entry["entity_id"], entry["stat_key"]
        )
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        event = await asyncio.to_thread(sofascore.get_event, event_id)
        if not event or sofascore.is_finished(event):
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        start_tracking(
            message, channel_id, event_id, entity_id, entry["team_id"], entry["is_tennis"],
            entry["sport"], stat_key, entry["stat_label"], entry["player_name"], entry.get("owner_id"),
        )
        log.info("Resumed prop tracking for %s in channel %s", entry["player_name"], channel_id)

#!/usr/bin/env python3
"""
Manages background tasks that keep a live player-stat embed updated
(e.g. "Julio Rodriguez - Hits, currently 0"). Backed by ESPN's free site API
(see espn.py) for baseball/basketball/nfl/hockey - tennis and soccer aren't
supported yet (ESPN needs bespoke per-competition handling for those).

Mirrors tracker.py's design: same hibernation (a notstarted event can't
change, so sleep instead of polling every cycle, waking once per Eastern-time
day boundary and once more right before it starts), same repost-to-the-
bottom-of-the-channel on that final pre-start wake, same 🗑️-reaction delete
and restart-safe persistence.
"""

import asyncio
import datetime
import io
import logging
import random
import time
from typing import Optional

import discord

import config
import espn
import scoreimage
import scores365
import state

log = logging.getLogger("scorebox.proptracker")

POST_MATCH_DELETE_SECONDS = 24 * 3600

# Tolerate this many consecutive "event not found"/edit-failure polls before
# giving up - guards against a transient ESPN or Discord hiccup silently
# killing tracking for something that's still very much alive.
MAX_CONSECUTIVE_MISSES = 3

TRASH_EMOJI = "🗑️"

_active_props: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, entity_id, stat_key, owner_id) - lets
# the reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_STATUS_COLOR = {
    "notstarted": 0x3498DB,  # blue
    "inprogress": 0xE74C3C,  # red
    "finished": 0x2ECC71,  # green
}


def _stat_key_str(stat_key: tuple) -> str:
    label, discriminator = stat_key
    return f"{label}:{discriminator}"


def prop_key(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> str:
    return f"{channel_id}:{event_id}:{entity_id}:{_stat_key_str(stat_key)}"


def is_tracked(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> bool:
    return prop_key(channel_id, event_id, entity_id, stat_key) in _active_props


def register_message(message_id: int, channel_id: int, event_id, entity_id: str, stat_key: tuple, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, entity_id, stat_key, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, event_id, entity_id: str, stat_key: tuple, message_id: int,
    sport: str, team_id: str, photo_url: Optional[str], stat_label: str, player_name: str, owner_id: int,
):
    data = state.load_props()
    data[prop_key(channel_id, event_id, entity_id, stat_key)] = {
        "channel_id": channel_id, "event_id": event_id, "entity_id": entity_id,
        "stat_key": list(stat_key), "message_id": message_id, "sport": sport,
        "team_id": team_id, "photo_url": photo_url, "stat_label": stat_label,
        "player_name": player_name, "owner_id": owner_id,
    }
    state.save_props(data)


def _forget(channel_id: int, event_id, entity_id: str, stat_key: tuple):
    data = state.load_props()
    data.pop(prop_key(channel_id, event_id, entity_id, stat_key), None)
    state.save_props(data)


def stop_tracking(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> bool:
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
    return "-" if v is None else str(v)


async def build_embed(
    player_name: str,
    entity_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_label: str,
    current_value,
    is_home,
    team: Optional[dict],
    event: dict,
) -> tuple[discord.Embed, discord.File]:
    """Returns (embed, file) - mirrors tracker.build_embed's shape."""
    status = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    status_type = {"pre": "notstarted", "in": "inprogress", "post": "finished"}.get(status.get("state"), "notstarted")

    competitors = (event.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])
    home_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "home"), "?")
    away_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "away"), "?")
    matchup = f"{home_name} v {away_name}"

    team_name = (team or {}).get("displayName", "?")
    status_label = espn.match_status_text(event, sport)

    sport_label = espn.SPORT_DISPLAY_LABELS.get(sport, sport.title())
    header = event.get("header", {})
    league_name = ((header.get("league") or {}).get("name")) or sport_label
    sport_tournament = f"{sport_label} • {league_name}" if league_name != sport_label else sport_label

    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        sport_tournament, matchup, team_name, photo_url, player_name, stat_label,
        _fmt_value(current_value), status_type, status_label,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=_STATUS_COLOR.get(status_type, 0x95A5A6))
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via ESPN")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message,
    channel_id: int,
    event_id,
    entity_id: str,
    team_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_key: tuple,
    stat_label: str,
    player_name: str,
    owner_id: int,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(espn.get_event, sport, event_id)

            # A notstarted event's stats can't change before it starts, so
            # hibernate instead of polling every cycle. Wakes once per
            # Eastern-time day boundary and once more just before it starts.
            hibernated = False
            while event and not espn.is_finished(event):
                status = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {})
                if status.get("type", {}).get("state") != "pre":
                    break
                start = (event.get("header", {}).get("competitions") or [{}])[0].get("date")
                if not start:
                    break
                try:
                    kickoff = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    break
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                log.info("Event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(espn.get_event, sport, event_id)

            if not event:
                consecutive_misses += 1
                log.warning(
                    "Event %s not found on ESPN (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    break
                continue
            consecutive_misses = 0

            current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity_id, stat_key)
            embed, file = await build_embed(
                player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team, event
            )

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                try:
                    new_message = await message.channel.send(embed=embed, file=file)
                except discord.HTTPException as e:
                    log.warning("Failed to repost prop tracking message near start: %s", e)
                else:
                    try:
                        await new_message.add_reaction(TRASH_EMOJI)
                    except discord.HTTPException as e:
                        log.warning("Failed to react to reposted prop tracking message: %s", e)
                    old_message = message
                    message = new_message
                    _message_owners.pop(old_message.id, None)
                    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
                    _persist(channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url, stat_label, player_name, owner_id)
                    try:
                        await old_message.delete()
                    except discord.HTTPException as e:
                        log.warning("Failed to delete old prop tracking message after repost: %s", e)
                continue

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

            if espn.is_finished(event):
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
    entity_id: str,
    team_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_key: tuple,
    stat_label: str,
    player_name: str,
    owner_id: int,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    if key in _active_props:
        return
    task = asyncio.create_task(
        _track_loop(message, channel_id, event_id, entity_id, team_id, photo_url, sport, stat_key, stat_label, player_name, owner_id)
    )
    _active_props[key] = task
    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
    _persist(channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url, stat_label, player_name, owner_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone - cleans up instead.
    """
    for entry in list(state.load_props().values()):
        stat_key = tuple(entry["stat_key"])
        channel_id, event_id, entity_id = entry["channel_id"], entry["event_id"], entry["entity_id"]
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        event = await asyncio.to_thread(espn.get_event, entry["sport"], event_id)
        if not event:
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        # Even an already-finished event still needs to be handed to
        # start_tracking/_track_loop - it re-registers the message (so the
        # 🗑️ reaction keeps working) and re-arms the 24h auto-delete timer,
        # which would otherwise be silently lost on every restart.
        start_tracking(
            message, channel_id, event_id, entity_id, entry["team_id"], entry.get("photo_url"), entry["sport"],
            stat_key, entry["stat_label"], entry["player_name"], entry.get("owner_id"),
        )
        log.info("Resumed prop tracking for %s in channel %s", entry["player_name"], channel_id)

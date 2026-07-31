#!/usr/bin/env python3
"""
Manages background tasks for tennis player prop picks (Aces, Double Faults,
Winners, Unforced Errors, Break Points Won) - backed by Sofascore, since
ESPN doesn't support tennis stats at all (see espn.py's module docstring).
Sofascore models a singles player as a 1-person "team" entity, so there's no
roster-team concept to resolve the way proptracker.py's other sports need -
the card instead shows "vs <opponent>" the same way tracker.py shows a
matchup.

Mirrors proptracker.py's design otherwise: hibernation before start (a
notstarted match's stats can't change), 🗑️-reaction delete, restart-safe
persistence, early-win tagging for Over picks, and a Won/Lost/Push result
reaction once the match finishes.
"""

import asyncio
import io
import logging
import random
import time
from typing import Optional

import discord

import botlog
import config
import pendingdelete
import scoreimage
import scores365
import sofascore
import state
import throttle

log = logging.getLogger("scorebox.tennispropstracker")
MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, entity_id, stat_key, owner_id) - lets
# the reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_STATUS_COLOR = {"notstarted": 0x3498DB, "inprogress": 0xE74C3C, "finished": 0x2ECC71}
_RESULT_TITLES = {"won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost", "push": "➖ Push"}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>"}


def prop_key(channel_id: int, event_id, entity_id, stat_key: str) -> str:
    return f"{channel_id}:{event_id}:{entity_id}:{stat_key}"


def is_tracked(channel_id: int, event_id, entity_id, stat_key: str) -> bool:
    return prop_key(channel_id, event_id, entity_id, stat_key) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_tennis_props().values()
        if prop_key(entry["channel_id"], entry["event_id"], entry["entity_id"], entry["stat_key"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, event_id, entity_id, stat_key: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, entity_id, stat_key, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, event_id, entity_id, stat_key: str, message_id: int,
    stat_label: str, player_name: str, photo_url: Optional[str], owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    data = state.load_tennis_props()
    data[prop_key(channel_id, event_id, entity_id, stat_key)] = {
        "channel_id": channel_id, "event_id": event_id, "entity_id": entity_id, "stat_key": stat_key,
        "message_id": message_id, "stat_label": stat_label, "player_name": player_name,
        "photo_url": photo_url, "owner_id": owner_id, "direction": direction, "line": line,
    }
    state.save_tennis_props(data)


def _forget(channel_id: int, event_id, entity_id, stat_key: str):
    data = state.load_tennis_props()
    data.pop(prop_key(channel_id, event_id, entity_id, stat_key), None)
    state.save_tennis_props(data)


def stop_tracking(channel_id: int, event_id, entity_id, stat_key: str) -> bool:
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    task = _active.pop(key, None)
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
    player_name: str, entity_id, photo_url: Optional[str], stat_label: str, current_value, event: dict,
    direction: Optional[str] = None, line: Optional[float] = None,
) -> tuple[discord.Embed, discord.File]:
    status_type = (event.get("status") or {}).get("type")
    render_status = status_type if status_type in ("notstarted", "inprogress", "finished") else "notstarted"

    home_name = (event.get("homeTeam") or {}).get("name", "?")
    away_name = (event.get("awayTeam") or {}).get("name", "?")
    opponent = away_name if home_name == player_name else home_name

    tournament_info = event.get("tournament") or {}
    tournament = (tournament_info.get("uniqueTournament") or {}).get("name") or tournament_info.get("name") or "Tennis"

    description_lines = [f"{player_name} vs {opponent}"]
    if direction is not None and line is not None:
        description_lines.append(f"{player_name} {direction.title()} {line:g} {stat_label}")
    if status_type == "notstarted":
        start_ts = event.get("startTimestamp")
        if start_ts:
            description_lines.append(f"<t:{int(start_ts)}:f>")
    description = "\n".join(description_lines)

    period_text = "" if status_type == "notstarted" else sofascore.match_status_text(event, "tennis")
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        f"vs {opponent}", photo_url, player_name, stat_label, _fmt_value(current_value), render_status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=_STATUS_COLOR.get(render_status, 0x95A5A6))
    if status_type == "finished" and direction is not None and line is not None:
        result = sofascore.grade_over_under(current_value, direction, line)
        if result:
            embed.title = _RESULT_TITLES[result]
    elif status_type == "inprogress" and direction == "over" and line is not None:
        # Same early-win idea as proptracker.py/tracker.py's Over tagging - a
        # counting stat only ever climbs during a match, so once the line is
        # already cleared it can't un-clear. Unders and exact ties excluded
        # for the same reason as elsewhere.
        try:
            if current_value is not None and float(current_value) > line:
                embed.title = _RESULT_TITLES["won"]
        except (TypeError, ValueError):
            pass
    embed.set_author(name=f"Tennis • {tournament}")
    embed.description = description
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via Sofascore")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, channel_id: int, event_id, entity_id, stat_key: str, stat_label: str,
    player_name: str, photo_url: Optional[str], owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(sofascore.get_event, event_id)

            # A notstarted match's stats can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
            # tracker.py/proptracker.py.
            hibernated = False
            while event and (event.get("status") or {}).get("type") == "notstarted":
                start_ts = event.get("startTimestamp")
                if not start_ts:
                    break
                seconds_until_start = start_ts - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(start_ts - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                log.info("Tennis prop event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(sofascore.get_event, event_id)

            if not event:
                consecutive_misses += 1
                log.warning(
                    "Tennis prop event %s not found on Sofascore (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (tennis prop): **{player_name}** — event `{event_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            current_value, _is_home = await asyncio.to_thread(sofascore.get_stat_value, event, entity_id, True, stat_key)
            embed, file = await build_embed(player_name, entity_id, photo_url, stat_label, current_value, event, direction, line)

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                try:
                    new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
                except discord.HTTPException as e:
                    log.warning("Failed to repost tennis prop tracking message near start: %s", e)
                else:
                    try:
                        await new_message.add_reaction(TRASH_EMOJI)
                    except discord.HTTPException as e:
                        log.warning("Failed to react to reposted tennis prop tracking message: %s", e)
                    old_message = message
                    message = new_message
                    _message_owners.pop(old_message.id, None)
                    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
                    _persist(channel_id, event_id, entity_id, stat_key, message.id, stat_label, player_name, photo_url, owner_id, direction, line)
                    try:
                        await old_message.delete()
                    except discord.HTTPException as e:
                        log.warning("Failed to delete old tennis prop tracking message after repost: %s", e)
                continue

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit tennis prop tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (tennis prop): **{player_name}** — message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue

            if sofascore.is_finished(event):
                if direction is not None and line is not None:
                    reaction = _RESULT_REACTIONS.get(sofascore.grade_over_under(current_value, direction, line))
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message)
                break
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, event_id, entity_id, stat_key)


def start_tracking(
    message: discord.Message, channel_id: int, event_id, entity_id, stat_key: str, stat_label: str,
    player_name: str, photo_url: Optional[str], owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(message, channel_id, event_id, entity_id, stat_key, stat_label, player_name, photo_url, owner_id, direction, line)
    )
    _active[key] = task
    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
    _persist(channel_id, event_id, entity_id, stat_key, message.id, stat_label, player_name, photo_url, owner_id, direction, line)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone - cleans up instead.
    """
    for entry in list(state.load_tennis_props().values()):
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
        if not event:
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        start_tracking(
            message, channel_id, event_id, entity_id, stat_key, entry["stat_label"], entry["player_name"],
            entry.get("photo_url"), entry.get("owner_id"), entry.get("direction"), entry.get("line"),
        )
        log.info("Resumed tennis prop tracking for %s in channel %s", entry["player_name"], channel_id)

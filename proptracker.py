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
import throttle

log = logging.getLogger("scorebox.proptracker")

POST_MATCH_DELETE_SECONDS = 24 * 3600

# While hibernating pre-game, how often to re-check whether the player has
# shown up in ESPN's boxscore yet (lineups typically post 1-3 hours before
# first pitch/tip-off) - keeps a "?" team name from sitting unrefreshed for
# however many hours are left until kickoff.
_LINEUP_CHECK_INTERVAL_SECONDS = 30 * 60

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


def list_tracked_details(channel_id: int) -> list[dict]:
    """Active prop-tracking entries for this channel, from persisted state."""
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active_props if k.startswith(prefix)}
    return [
        entry for entry in state.load_props().values()
        if prop_key(entry["channel_id"], entry["event_id"], entry["entity_id"], tuple(entry["stat_key"])) in active_keys
    ]


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
    direction: Optional[str] = None, line: Optional[float] = None, known_team_name: Optional[str] = None,
):
    data = state.load_props()
    data[prop_key(channel_id, event_id, entity_id, stat_key)] = {
        "channel_id": channel_id, "event_id": event_id, "entity_id": entity_id,
        "stat_key": list(stat_key), "message_id": message_id, "sport": sport,
        "team_id": team_id, "photo_url": photo_url, "stat_label": stat_label,
        "player_name": player_name, "owner_id": owner_id,
        "direction": direction, "line": line, "known_team_name": known_team_name,
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


_RESULT_TITLES = {"won": "✅ Pick Won", "lost": "❌ Pick Lost", "push": "➖ Push"}
_RESULT_REACTIONS = {"won": "✅", "lost": "❌"}


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
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
) -> tuple[discord.Embed, discord.File]:
    """
    Returns (embed, file). Sport/tournament goes in the embed author line and
    matchup + status/kickoff-time in the description - both outside the
    image, same placement as /track's author/description.

    direction/line are only set for auto-tracked picks (not manual
    /playerprops usage, which has no line to grade against) - once the event
    finishes, they're compared against the final stat value to show a
    Won/Lost/Push badge in the embed title.

    known_team_name is the player's roster team from the original ESPN
    player search (espn.find_player), independent of this specific event's
    boxscore - used as a fallback so the card shows a real team name
    immediately instead of "?" for however many hours it takes ESPN to
    publish this event's lineups (team can't change between search and event
    lookup within the same tracking session).
    """
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {}).get("type", {})
    # espn.is_finished() is the single source of truth for "finished", since
    # a naive state=="post" check also matches postponed/canceled games
    # (confirmed live) - those get their own "postponed" status instead,
    # rather than showing a false "Final" badge.
    if espn.is_finished(event):
        status_type = "finished"
    elif espn.is_postponed(event):
        status_type = "postponed"
    elif status.get("state") == "in":
        status_type = "inprogress"
    else:
        status_type = "notstarted"
    # No color/pill styling exists for "postponed" specifically - reuse the
    # notstarted (blue) look, since the pill *text* (below) already says
    # "Postponed"/"Canceled" via espn.match_status_text's detail field.
    render_status = "notstarted" if status_type == "postponed" else status_type

    competitors = comp.get("competitors", [])
    home_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "home"), "?")
    away_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "away"), "?")
    matchup = f"{home_name} v {away_name}"

    team_name = (team or {}).get("displayName") or known_team_name or "?"

    sport_label = espn.SPORT_DISPLAY_LABELS.get(sport, sport.title())
    league_name = ((event.get("header", {}).get("league") or {}).get("name")) or sport_label
    sport_tournament = f"{sport_label} • {league_name}" if league_name != sport_label else sport_label

    # The pick's own line (e.g. "Chris Sale Over 6.5 Strikeouts") stays
    # visible below the matchup for the card's whole lifetime, same as
    # moneyline cards showing "<Team> ML" - without it there's no way to
    # tell at a glance what the current value needs to beat. Live/final
    # status is drawn inside the image as a pill instead (see
    # scoreimage.render_player_card), same treatment as /track's cards.
    description_lines = [matchup]
    if direction is not None and line is not None:
        description_lines.append(f"{player_name} {direction.title()} {line:g} {stat_label}")
    if status_type == "notstarted" and comp.get("date"):
        try:
            kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
            description_lines.append(f"<t:{kickoff}:f>")
        except ValueError:
            pass
    description = "\n".join(description_lines)

    period_text = "" if status_type == "notstarted" else espn.match_status_text(event, sport)
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        team_name, photo_url, player_name, stat_label, _fmt_value(current_value), render_status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=_STATUS_COLOR.get(status_type, 0x95A5A6))
    if status_type == "finished" and direction is not None and line is not None:
        result = espn.grade_over_under(current_value, direction, line)
        if result:
            embed.title = _RESULT_TITLES[result]
    embed.set_author(name=sport_tournament)
    embed.description = description
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
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
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
            # Eastern-time day boundary and once more just before it starts -
            # but each individual sleep is capped at _LINEUP_CHECK_INTERVAL_
            # SECONDS so a card whose player hasn't appeared in ESPN's
            # boxscore yet (lineups aren't posted until a couple hours before
            # first pitch - confirmed live the boxscore's athlete lists are
            # empty/missing that far out) gets re-checked periodically and its
            # team name refreshed in place, instead of showing "?" for
            # however many hours are left until kickoff.
            hibernated = False
            team_shown = False
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
                hibernate_for = min(wake_at - time.time(), _LINEUP_CHECK_INTERVAL_SECONDS)
                deadline += hibernate_for
                hibernated = True
                log.info("Event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(espn.get_event, sport, event_id)

                if not team_shown and event:
                    current_value, is_home, team = await asyncio.to_thread(
                        espn.get_stat_value, event, entity_id, stat_key
                    )
                    if team is not None:
                        team_shown = True
                        refreshed_embed, refreshed_file = await build_embed(
                            player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team,
                            event, direction, line, known_team_name,
                        )
                        try:
                            await throttle.run(
                                channel_id, lambda: message.edit(embed=refreshed_embed, attachments=[refreshed_file])
                            )
                        except discord.HTTPException as e:
                            log.warning("Failed to refresh prop card once team resolved: %s", e)

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
                player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team, event,
                direction, line, known_team_name,
            )

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                try:
                    new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
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
                    _persist(
                        channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url,
                        stat_label, player_name, owner_id, direction, line, known_team_name,
                    )
                    try:
                        await old_message.delete()
                    except discord.HTTPException as e:
                        log.warning("Failed to delete old prop tracking message after repost: %s", e)
                continue

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
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

            if espn.is_finished(event) or espn.is_postponed(event):
                if espn.is_finished(event) and direction is not None and line is not None:
                    reaction = _RESULT_REACTIONS.get(espn.grade_over_under(current_value, direction, line))
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                # A postponed/canceled event never produces a graded result -
                # no reaction, but still cleans up after the same 24h window
                # rather than polling every cycle until MAX_TRACK_HOURS runs
                # out and leaving the stale card behind forever.
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
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    if key in _active_props:
        return
    task = asyncio.create_task(
        _track_loop(
            message, channel_id, event_id, entity_id, team_id, photo_url, sport, stat_key, stat_label,
            player_name, owner_id, direction, line, known_team_name,
        )
    )
    _active_props[key] = task
    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
    _persist(
        channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url, stat_label,
        player_name, owner_id, direction, line, known_team_name,
    )


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
            entry.get("direction"), entry.get("line"), entry.get("known_team_name"),
        )
        log.info("Resumed prop tracking for %s in channel %s", entry["player_name"], channel_id)

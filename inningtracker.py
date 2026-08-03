#!/usr/bin/env python3
"""
Manages background tasks for YRFI/NRFI ("Yes/No Runs First Inning") picks -
these settle as soon as the 1st inning is fully complete, not when the whole
game finishes, so they don't share tracker.py/proptracker.py's wait-for-the-
full-game design. Backed by ESPN's per-inning linescores (see
espn.get_first_inning_breakdown) - MLB only, first-inning scoring isn't a
bet type tracked here for other sports.

Mirrors tracker.py/proptracker.py otherwise: hibernation before kickoff
(a notstarted game's linescore can't change), 🗑️-reaction delete, and
restart-safe persistence.
"""

import asyncio
import datetime
import io
import logging
import random
import time
from typing import Optional

import discord

import botlog
import config
import espn
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.inningtracker")

MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, pick_type, owner_id) - lets the
# reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {"won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost"}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}

_PICK_LABELS = {"YRFI": "Yes Runs 1st Inning", "NRFI": "No Runs 1st Inning"}


def track_key(channel_id: int, event_id, pick_type: str) -> str:
    return f"{channel_id}:{event_id}:{pick_type}"


def is_tracked(channel_id: int, event_id, pick_type: str) -> bool:
    return track_key(channel_id, event_id, pick_type) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_innings().values()
        if track_key(entry["channel_id"], entry["event_id"], entry["pick_type"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, event_id, pick_type: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, pick_type, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(channel_id: int, event_id, pick_type: str, message_id: int, team_id: str, owner_id: int):
    data = state.load_innings()
    data[track_key(channel_id, event_id, pick_type)] = {
        "channel_id": channel_id, "event_id": event_id, "pick_type": pick_type,
        "message_id": message_id, "team_id": team_id, "owner_id": owner_id,
    }
    state.save_innings(data)


def _forget(channel_id: int, event_id, pick_type: str):
    data = state.load_innings()
    data.pop(track_key(channel_id, event_id, pick_type), None)
    state.save_innings(data)


def stop_tracking(channel_id: int, event_id, pick_type: str) -> bool:
    key = track_key(channel_id, event_id, pick_type)
    task = _active.pop(key, None)
    _forget(channel_id, event_id, pick_type)
    for message_id, (c_id, e_id, p_type, _owner) in list(_message_owners.items()):
        if c_id == channel_id and e_id == event_id and p_type == pick_type:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def build_embed(event: dict, pick_type: str) -> tuple[discord.Embed, discord.File]:
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {})
    state_name = status.get("type", {}).get("state")
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home_name = home.get("team", {}).get("displayName", "?")
    away_name = away.get("team", {}).get("displayName", "?")
    home_logo = espn.team_logo_url(home.get("team", {}))
    away_logo = espn.team_logo_url(away.get("team", {}))

    breakdown = espn.get_first_inning_breakdown(event)
    decided = breakdown is not None
    result = espn.grade_yrfi(sum(breakdown), pick_type) if decided else None
    postponed = not decided and espn.is_postponed(event)

    if postponed:
        # A postponed/canceled event never produces a graded win/loss -
        # treated as a voided leg, same as an interrupted-and-never-resumed
        # match elsewhere in this bot.
        color_status = "void"
    elif result:
        color_status = result
    elif decided:
        color_status = "finished"
    elif state_name == "in":
        color_status = "inprogress"
    else:
        color_status = "notstarted"

    league_name = ((event.get("header", {}).get("league") or {}).get("name")) or "MLB"
    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]
    embed.set_author(name=f"MLB • {league_name}" if league_name != "MLB" else "MLB")

    description_lines = [f"{away_name} v {home_name}", _PICK_LABELS[pick_type]]
    if state_name == "pre" and comp.get("date"):
        try:
            kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
            description_lines.append(f"<t:{kickoff}:f>")
        except ValueError:
            pass
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "1st Inning Final"
    elif postponed:
        period_text = espn.match_status_text(event, "baseball")
    elif state_name == "in":
        period_text = status.get("type", {}).get("detail") or "Live"
    else:
        period_text = ""

    home_cols = [str(breakdown[0])] if decided else ["-"]
    away_cols = [str(breakdown[1])] if decided else ["-"]

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, home_logo, away_logo, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via ESPN")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(message: discord.Message, channel_id: int, event_id, pick_type: str, team_id: str, owner_id: int):
    key = track_key(channel_id, event_id, pick_type)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-kickoff or
        graded) - falls back to editing in place if the repost send itself
        fails. Carries forward any reaction someone added beyond the bot's
        own service ones (e.g. tagging a card as part of a parlay) -
        otherwise silently lost every time a repost deletes the old
        message. Discord has no way to make a reaction reappear as added by
        the original user once that message is gone, so this re-adds the
        same emoji under the bot's own account instead."""
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final inning tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit inning tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final inning tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, event_id, pick_type, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old inning tracking message after final repost: %s", e)
        return carry_emojis

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(espn.get_event, "baseball", event_id)

            # A notstarted event's linescore can't change before it starts,
            # so hibernate instead of polling every cycle - same pattern as
            # tracker.py/proptracker.py. Wakes once per Eastern-time day
            # boundary and once more just before it starts.
            hibernated = False
            while event:
                comp = (event.get("header", {}).get("competitions") or [{}])[0]
                if comp.get("status", {}).get("type", {}).get("state") != "pre":
                    break
                start = comp.get("date")
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

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation ends
                # (right before kickoff) is too late: a leg with a kickoff
                # hours away would still never appear on its parlay's
                # summary card until it was basically already live anyway.
                try:
                    fresh = await message.channel.fetch_message(message.id)
                    marker_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
                except discord.HTTPException:
                    marker_emojis = []
                if marker_emojis:
                    pre_competitors = comp.get("competitors", [])
                    pre_home = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "home"), "?")
                    pre_away = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "away"), "?")
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "inningtracker", key,
                        f"{pre_away} v {pre_home} - {_PICK_LABELS[pick_type]}",
                        f"NOT STARTED - <t:{int(kickoff)}:f>", marker_emojis,
                    )

                log.info("Event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(espn.get_event, "baseball", event_id)

            if not event:
                consecutive_misses += 1
                log.warning(
                    "Event %s not found on ESPN (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(event, pick_type)
            leg_competitors = (event.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])
            leg_home = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "home"), "?")
            leg_away = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "away"), "?")
            leg_label = f"{leg_away} v {leg_home} - {_PICK_LABELS[pick_type]}"

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/proptracker.py.
                await _repost_final(embed, file)
                _persist(channel_id, event_id, pick_type, message.id, team_id, owner_id)
                continue

            breakdown = espn.get_first_inning_breakdown(event)
            if breakdown is not None or espn.is_postponed(event):
                # Bump the graded result to the bottom of the channel instead
                # of editing in place - same reasoning as the pre-kickoff bump
                # above: a live game can run long enough that the original
                # card is buried under chat by the time the 1st inning wraps.
                carry_emojis = await _repost_final(embed, file)

                if breakdown is not None:
                    result = espn.grade_yrfi(sum(breakdown), pick_type)
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                else:
                    # A postponed/canceled event never produces a graded
                    # win/loss - treated as a voided leg for parlay
                    # purposes, same as an interrupted-and-never-resumed
                    # match elsewhere in this bot.
                    result = "void"
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "inningtracker", key, leg_label, result, carry_emojis,
                )
                # A postponed/canceled event never produces a graded result -
                # no reaction, but still cleans up after the same 24h window
                # rather than polling every cycle until MAX_TRACK_HOURS runs
                # out and leaving the stale card behind forever.
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            try:
                fresh = await message.channel.fetch_message(message.id)
                marker_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
            except discord.HTTPException:
                marker_emojis = []
            if marker_emojis:
                comp = (event.get("header", {}).get("competitions") or [{}])[0]
                if comp.get("status", {}).get("type", {}).get("state") == "pre":
                    try:
                        kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
                        detail = f"NOT STARTED - <t:{kickoff}:f>"
                    except (KeyError, ValueError):
                        detail = "NOT STARTED"
                else:
                    detail = f"LIVE, {comp.get('status', {}).get('type', {}).get('detail') or 'Live'}"
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "inningtracker", key, leg_label, detail, marker_emojis,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit inning tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, event_id, pick_type)


def start_tracking(message: discord.Message, channel_id: int, event_id, pick_type: str, team_id: str, owner_id: int):
    key = track_key(channel_id, event_id, pick_type)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, channel_id, event_id, pick_type, team_id, owner_id))
    _active[key] = task
    register_message(message.id, channel_id, event_id, pick_type, owner_id)
    _persist(channel_id, event_id, pick_type, message.id, team_id, owner_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone - cleans up instead.
    """
    for entry in list(state.load_innings().values()):
        channel_id, event_id, pick_type = entry["channel_id"], entry["event_id"], entry["pick_type"]
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, event_id, pick_type)
            continue

        event = await asyncio.to_thread(espn.get_event, "baseball", event_id)
        if not event:
            _forget(channel_id, event_id, pick_type)
            continue

        start_tracking(message, channel_id, event_id, pick_type, entry["team_id"], entry.get("owner_id"))
        log.info("Resumed inning tracking for event %s in channel %s", event_id, channel_id)

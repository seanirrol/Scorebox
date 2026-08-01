#!/usr/bin/env python3
"""
Manages background tasks for UFC picks - fight moneyline (who wins) or
round total (Over/Under how many rounds the fight lasts) - backed by
espn_ufc.py. Method of victory isn't supported (see espn_ufc.py's module
docstring - no reliable field for it anywhere in ESPN's public data).

A UFC fighter is its own "competitor" within a bout ("competition"), same
model as tennis's F5/1st-set/prop tracking already use elsewhere in this
bot - no team/roster concept to resolve. Round-total picks don't need a
picked fighter at all (graded against the bout as a whole, same idea as
f5tracker.py's combined-total mode).

Mirrors settracker.py's design otherwise: hibernation before start (a
notstarted bout's result can't change), 🗑️-reaction delete, restart-safe
persistence, and a Won/Lost/Push result reaction once the bout finishes.
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
import espn_ufc
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.ufctracker")

MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, competition_id, owner_id) - lets the
# reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {"won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost", "push": "➖ Push"}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def track_key(channel_id: int, competition_id) -> str:
    return f"{channel_id}:{competition_id}"


def is_tracked(channel_id: int, competition_id) -> bool:
    return track_key(channel_id, competition_id) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_ufc().values()
        if track_key(entry["channel_id"], entry["competition_id"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, competition_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, competition_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, league_slug: str, event_id, competition_id, competition_date: str, message_id: int, owner_id: int,
    event_name: str, fighter_id=None, fighter_name: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    data = state.load_ufc()
    data[track_key(channel_id, competition_id)] = {
        "channel_id": channel_id, "league_slug": league_slug, "event_id": event_id, "competition_id": competition_id,
        "competition_date": competition_date, "message_id": message_id, "owner_id": owner_id,
        "event_name": event_name, "fighter_id": fighter_id, "fighter_name": fighter_name,
        "total_direction": total_direction, "total_line": total_line,
    }
    state.save_ufc(data)


def _forget(channel_id: int, competition_id):
    data = state.load_ufc()
    data.pop(track_key(channel_id, competition_id), None)
    state.save_ufc(data)


def stop_tracking(channel_id: int, competition_id) -> bool:
    key = track_key(channel_id, competition_id)
    task = _active.pop(key, None)
    _forget(channel_id, competition_id)
    for message_id, (c_id, comp_id, _owner) in list(_message_owners.items()):
        if c_id == channel_id and comp_id == competition_id:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def build_embed(
    competition: dict, league_slug: str, event_name: str, fighter_id=None, fighter_name: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
) -> tuple[discord.Embed, discord.File]:
    """Exactly one of two modes applies per pick: fighter_id/fighter_name is
    a moneyline pick on that fighter; total_direction/total_line (no
    fighter) grades the round total against the bout as a whole."""
    competitors = competition.get("competitors", [])
    fighter_a = next((c for c in competitors if c.get("order") == 1), competitors[0] if competitors else {})
    fighter_b = next((c for c in competitors if c.get("order") == 2), competitors[1] if len(competitors) > 1 else {})
    name_a = (fighter_a.get("athlete") or {}).get("displayName", "?")
    name_b = (fighter_b.get("athlete") or {}).get("displayName", "?")

    decided = espn_ufc.is_finished(competition)
    if decided:
        if fighter_id is not None:
            picked = fighter_a if str(fighter_a.get("id")) == str(fighter_id) else fighter_b
            result = espn_ufc.grade_ufc_moneyline(competition, picked)
        elif total_direction and total_line is not None:
            result = espn_ufc.grade_ufc_round_total(competition, total_direction, total_line)
        else:
            result = None
    else:
        result = None

    embed_color = {"won": 0x2ECC71, "lost": 0xE74C3C, "push": 0x95A5A6}.get(result, 0x3498DB)
    embed = discord.Embed(color=embed_color)
    if result:
        embed.title = _RESULT_TITLES[result]
    league_label = espn_ufc.LEAGUE_LABELS.get(league_slug, league_slug.upper())
    embed.set_author(name=f"{league_label} • {event_name}")

    if fighter_id is not None:
        description_lines = [f"{fighter_name} ML"]
    elif total_direction and total_line is not None:
        description_lines = [f"Fight {total_direction.title()} {total_line:g} Rounds"]
    else:
        description_lines = []
    if not decided:
        kickoff = espn_ufc.start_epoch(competition)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    period_text = "Final" if decided else espn_ufc.status_text(competition)
    card_status = "finished" if decided else ("inprogress" if competition.get("status", {}).get("type", {}).get("state") == "in" else "notstarted")

    if decided:
        home_cols = ["W"] if fighter_a.get("winner") else (["L"] if any(c.get("winner") for c in competitors) else ["-"])
        away_cols = ["W"] if fighter_b.get("winner") else (["L"] if any(c.get("winner") for c in competitors) else ["-"])
    else:
        home_cols = away_cols = ["-"]

    home_photo = espn_ufc.fighter_photo_url(fighter_a.get("id")) if fighter_a.get("id") else None
    away_photo = espn_ufc.fighter_photo_url(fighter_b.get("id")) if fighter_b.get("id") else None

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        name_a, name_b, home_photo, away_photo, home_cols, away_cols, period_text, card_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via ESPN")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, channel_id: int, league_slug: str, event_id, competition_id, competition_date: str, owner_id: int,
    event_name: str, fighter_id=None, fighter_name: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    key = track_key(channel_id, competition_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-fight or
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
            log.warning("Failed to repost final UFC tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit UFC tracking message as a fallback: %s", e2)
            return
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final UFC tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, competition_id, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old UFC tracking message after final repost: %s", e)

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            refreshed = await asyncio.to_thread(
                espn_ufc.refresh_ufc_fight, league_slug, event_id, competition_id, fighter_id, competition_date
            )

            # A notstarted bout's result can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
            # tracker.py/settracker.py.
            hibernated = False
            while refreshed and refreshed[0].get("status", {}).get("type", {}).get("state") == "pre":
                kickoff = espn_ufc.start_epoch(refreshed[0])
                if not kickoff:
                    break
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                log.info("UFC bout %s not starting soon; hibernating %.0fs", competition_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                refreshed = await asyncio.to_thread(
                    espn_ufc.refresh_ufc_fight, league_slug, event_id, competition_id, fighter_id, competition_date
                )

            if not refreshed:
                consecutive_misses += 1
                log.warning(
                    "UFC bout %s not found on ESPN (miss %d/%d)",
                    competition_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (UFC): bout `{competition_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0
            competition, _fighter = refreshed

            embed, file = await build_embed(competition, league_slug, event_name, fighter_id, fighter_name, total_direction, total_line)

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                await _repost_final(embed, file)
                _persist(
                    channel_id, league_slug, event_id, competition_id, competition_date, message.id, owner_id,
                    event_name, fighter_id, fighter_name, total_direction, total_line,
                )
                continue

            if espn_ufc.is_finished(competition):
                if fighter_id is not None:
                    fighter_a = next((c for c in competition.get("competitors", []) if str(c.get("id")) == str(fighter_id)), None)
                    result = espn_ufc.grade_ufc_moneyline(competition, fighter_a) if fighter_a else None
                elif total_direction and total_line is not None:
                    result = espn_ufc.grade_ufc_round_total(competition, total_direction, total_line)
                else:
                    result = None

                # Bump the graded result to the bottom of the channel instead
                # of editing in place - same reasoning as the pre-fight bump
                # above: a live card can run long enough that the original
                # card is buried under chat by the time the bout is graded.
                await _repost_final(embed, file)

                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit UFC tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (UFC): bout `{competition_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, competition_id)


def start_tracking(
    message: discord.Message, channel_id: int, league_slug: str, event_id, competition_id, competition_date: str, owner_id: int,
    event_name: str, fighter_id=None, fighter_name: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    key = track_key(channel_id, competition_id)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(
            message, channel_id, league_slug, event_id, competition_id, competition_date, owner_id, event_name,
            fighter_id, fighter_name, total_direction, total_line,
        )
    )
    _active[key] = task
    register_message(message.id, channel_id, competition_id, owner_id)
    _persist(
        channel_id, league_slug, event_id, competition_id, competition_date, message.id, owner_id,
        event_name, fighter_id, fighter_name, total_direction, total_line,
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/bout is gone - cleans up instead.
    """
    for entry in list(state.load_ufc().values()):
        channel_id, competition_id = entry["channel_id"], entry["competition_id"]
        league_slug = entry.get("league_slug", "ufc")
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, competition_id)
            continue

        refreshed = await asyncio.to_thread(
            espn_ufc.refresh_ufc_fight, league_slug, entry["event_id"], competition_id, entry.get("fighter_id"), entry["competition_date"]
        )
        if not refreshed:
            _forget(channel_id, competition_id)
            continue
        event_name = entry.get("event_name") or f"Event {entry['event_id']}"

        start_tracking(
            message, channel_id, league_slug, entry["event_id"], competition_id, entry["competition_date"], entry.get("owner_id"),
            event_name, entry.get("fighter_id"), entry.get("fighter_name"), entry.get("total_direction"), entry.get("total_line"),
        )
        log.info("Resumed UFC tracking for bout %s in channel %s", competition_id, channel_id)

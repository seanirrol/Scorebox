#!/usr/bin/env python3
"""
Manages background tasks for tennis player prop picks (Aces, Double Faults,
Break Points Won) - backed by 365scores' per-game stats endpoint (see
scores365.tennis_player_stat). ESPN doesn't support tennis at all (see
espn.py's module docstring); Sofascore was tried first but 403s from this
bot's VPS specifically (confirmed live - works fine from other networks,
even with the curl TLS-fingerprint workaround, but not from here), so
tennis props moved to 365scores entirely instead. 365scores doesn't track
Winners or Unforced Errors at all for tennis (confirmed live across 46 real
finished matches in one day's slate, none had either stat) - not available
here until another source is found for those two specifically.

A tennis player is its own "competitor" in 365scores' data model (same as
/track's moneyline, F5, and 1st-set tracking already use), so - unlike
proptracker.py's other sports - there's no separate team-affiliation lookup
needed: the picked player IS the team, found via
scores365.find_match_for_team same as everywhere else.

Mirrors settracker.py's design otherwise: hibernation before start (a
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
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.tennispropstracker")
MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, competitor_id, stat_name, owner_id) -
# lets the reaction-based delete handler in bot.py look up who can delete a
# message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "🚫 Voided (No Action)",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "void": "🚫"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def prop_key(channel_id: int, game_id, competitor_id, stat_name: str) -> str:
    return f"{channel_id}:{game_id}:{competitor_id}:{stat_name}"


def is_tracked(channel_id: int, game_id, competitor_id, stat_name: str) -> bool:
    return prop_key(channel_id, game_id, competitor_id, stat_name) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_tennis_props().values()
        if prop_key(entry["channel_id"], entry["game_id"], entry["competitor_id"], entry["stat_name"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, competitor_id, stat_name: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, competitor_id, stat_name, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, competitor_id, stat_name: str, message_id: int, sport_id,
    stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    data = state.load_tennis_props()
    data[prop_key(channel_id, game_id, competitor_id, stat_name)] = {
        "channel_id": channel_id, "game_id": game_id, "competitor_id": competitor_id, "stat_name": stat_name,
        "message_id": message_id, "sport_id": sport_id, "stat_label": stat_label, "player_name": player_name,
        "owner_id": owner_id, "direction": direction, "line": line,
    }
    state.save_tennis_props(data)


def _forget(channel_id: int, game_id, competitor_id, stat_name: str):
    data = state.load_tennis_props()
    data.pop(prop_key(channel_id, game_id, competitor_id, stat_name), None)
    state.save_tennis_props(data)


def stop_tracking(channel_id: int, game_id, competitor_id, stat_name: str) -> bool:
    key = prop_key(channel_id, game_id, competitor_id, stat_name)
    task = _active.pop(key, None)
    _forget(channel_id, game_id, competitor_id, stat_name)
    for message_id, (c_id, g_id, comp_id, s_name, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and comp_id == competitor_id and s_name == stat_name:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _fmt_value(v) -> str:
    return "-" if v is None else str(v)


async def build_embed(
    game: dict, sport_id: Optional[int], competitor_id, player_name: str, stat_label: str, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None,
) -> tuple[discord.Embed, discord.File]:
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))
    opponent = away_competitor.get("name", "?") if home_competitor.get("id") == competitor_id else home_competitor.get("name", "?")

    current_value = None
    if status != "notstarted":
        current_value = await asyncio.to_thread(scores365.tennis_player_stat, game.get("id"), competitor_id, stat_name)

    description_lines = [f"{player_name} vs {opponent}"]
    if direction is not None and line is not None:
        description_lines.append(f"{player_name} {direction.title()} {line:g} {stat_label}")
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    description = "\n".join(description_lines)

    photo_url = scores365.competitor_logo_url(home_competitor if home_competitor.get("id") == competitor_id else away_competitor)
    period_text = "" if status == "notstarted" else scores365.status_line(game, sport_id)
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        f"vs {opponent}", photo_url, player_name, stat_label, _fmt_value(current_value), status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed_color = {"won": 0x2ECC71, "lost": 0xE74C3C, "push": 0x95A5A6}
    embed = discord.Embed(color=0x3498DB)
    if status == "finished" and direction is not None and line is not None:
        result = scores365.grade_over_under(current_value, direction, line)
        if result:
            embed.title = _RESULT_TITLES[result]
            embed.color = embed_color[result]
    elif status == "inprogress" and direction == "over" and line is not None:
        # Same early-win idea as tracker.py/proptracker.py's Over tagging - a
        # counting stat only ever climbs during a match, so once the line is
        # already cleared it can't un-clear. Unders and exact ties excluded
        # for the same reason as elsewhere.
        try:
            if current_value is not None and float(current_value) > line:
                embed.title = _RESULT_TITLES["won"]
                embed.color = embed_color["won"]
        except (TypeError, ValueError):
            pass
    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))
    embed.description = description
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via 365scores")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, competitor_id, stat_name: str,
    stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    key = prop_key(channel_id, game_id, competitor_id, stat_name)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-start, graded,
        or voided) - falls back to editing in place if the repost send
        itself fails. Carries forward any reaction someone added beyond the
        bot's own service ones (e.g. tagging a card as part of a parlay) -
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
            log.warning("Failed to repost final tennis prop tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit tennis prop tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final tennis prop tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, competitor_id, stat_name, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old tennis prop tracking message after final repost: %s", e)
        return carry_emojis

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            # A notstarted match's stats can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
            # tracker.py/settracker.py.
            hibernated = False
            while game and scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                kickoff = scores365.start_epoch(game)
                if not kickoff:
                    break
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                log.info("Tennis prop game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "Tennis prop game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (tennis prop): **{player_name}** — game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, competitor_id, player_name, stat_label, stat_name, direction, line)

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                await _repost_final(embed, file)
                _persist(channel_id, game_id, competitor_id, stat_name, message.id, sport_id, stat_label, player_name, owner_id, direction, line)
                continue

            if scores365.is_finished(game):
                carry_emojis = await _repost_final(embed, file)

                if direction is not None and line is not None:
                    current_value = await asyncio.to_thread(scores365.tennis_player_stat, game_id, competitor_id, stat_name)
                    result = scores365.grade_over_under(current_value, direction, line)
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                    if result:
                        await parlaytracker.handle_leg_result(message.channel, channel_id, message, result, carry_emojis)
                pendingdelete.start(channel_id, message, embed.description or "")
                break

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
        else:
            # MAX_TRACK_HOURS ran out without the match ever finishing. If it
            # was left mid-interruption (rain delay, darkness, etc.) rather
            # than genuinely still in progress, that's stalled, not just slow
            # - tag it Voided/No Action instead of silently leaving the card
            # stuck with no result and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(game, sport_id, competitor_id, player_name, stat_label, stat_name, direction, line)
                embed.title = _RESULT_TITLES["void"]
                embed.color = 0x95A5A6
                carry_emojis = await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                await parlaytracker.handle_leg_result(message.channel, channel_id, message, "void", carry_emojis)
                botlog.event(f"➖ Voided (tennis prop, interrupted, never resumed): **{player_name}** in <#{channel_id}>")
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, competitor_id, stat_name)


def start_tracking(
    message: discord.Message, sport_id: int, game_id, channel_id: int, competitor_id, stat_name: str,
    stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    key = prop_key(channel_id, game_id, competitor_id, stat_name)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, competitor_id, stat_name, stat_label, player_name, owner_id, direction, line)
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, competitor_id, stat_name, owner_id)
    _persist(channel_id, game_id, competitor_id, stat_name, message.id, sport_id, stat_label, player_name, owner_id, direction, line)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_tennis_props().values()):
        try:
            channel_id, game_id, competitor_id, stat_name, sport_id = (
                entry["channel_id"], entry["game_id"], entry["competitor_id"], entry["stat_name"], entry["sport_id"]
            )
        except KeyError:
            # Belongs to the pre-migration Sofascore-backed schema
            # (event_id/entity_id/stat_key instead of
            # game_id/competitor_id/stat_name) - can't be resumed, just drop
            # it rather than crashing the rest of startup.
            log.warning("Dropping tennis prop entry from an old state schema: %r", entry)
            continue
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, game_id, competitor_id, stat_name)
            continue

        game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
        if not game:
            _forget(channel_id, game_id, competitor_id, stat_name)
            continue

        start_tracking(
            message, sport_id, game_id, channel_id, competitor_id, stat_name, entry["stat_label"],
            entry["player_name"], entry.get("owner_id"), entry.get("direction"), entry.get("line"),
        )
        log.info("Resumed tennis prop tracking for %s in channel %s", entry["player_name"], channel_id)

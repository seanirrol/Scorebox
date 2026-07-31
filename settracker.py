#!/usr/bin/env python3
"""
Manages background tasks for tennis "1st Set" moneyline picks (e.g. "Naomi
Osaka to win 1st Set", "1st Set ML") - settled once Set 1 is fully complete,
not when the whole match finishes. Two-way market with no push/tie case - a
completed tennis set is always decided one way or the other (by games or a
tiebreak).

Backed by 365scores' bulk game list, which already carries a per-set
`stages` breakdown for tennis directly on the game object (see
scores365.tennis_first_set_result) - no separate per-game detail call
needed, unlike baseball's innings_breakdown.

Mirrors f5tracker.py/inning1tracker.py otherwise: hibernation before
kickoff, 🗑️-reaction delete, restart-safe persistence, and a win/loss result
reaction once the pick is graded.
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
import scores365
import state
import throttle

log = logging.getLogger("scorebox.settracker")

POST_RESULT_DELETE_SECONDS = 24 * 3600
MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {"won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost"}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>"}


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_set1().values()
        if track_key(entry["channel_id"], entry["game_id"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(channel_id: int, game_id, message_id: int, sport_id, owner_id: int, team: str):
    data = state.load_set1()
    data[track_key(channel_id, game_id)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "team": team,
    }
    state.save_set1(data)


def _forget(channel_id: int, game_id):
    data = state.load_set1()
    data.pop(track_key(channel_id, game_id), None)
    state.save_set1(data)


def stop_tracking(channel_id: int, game_id) -> bool:
    key = track_key(channel_id, game_id)
    task = _active.pop(key, None)
    _forget(channel_id, game_id)
    for message_id, (c_id, g_id, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def build_embed(game: dict, sport_id: Optional[int], team: str) -> tuple[discord.Embed, discord.File]:
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"))

    breakdown = scores365.tennis_first_set_result(game)
    decided = breakdown is not None
    result = scores365.grade_tennis_set(game, breakdown[0], breakdown[1], team) if decided else None

    embed_color = {"won": 0x2ECC71, "lost": 0xE74C3C}.get(result, 0x3498DB)
    embed = discord.Embed(color=embed_color)
    if result:
        embed.title = _RESULT_TITLES[result]

    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    description_lines = [f"{team} 1st Set ML"]
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "1st Set Final"
        card_status = "finished"
    elif status == "inprogress":
        period_text = scores365.status_line(game, sport_id)
        card_status = "inprogress"
    else:
        period_text = ""
        card_status = "notstarted"

    if decided:
        # Frozen at the Set 1 score that decided the pick, even though the
        # match (and its overall sets-won score) keeps going past this point.
        home_cols = [str(breakdown[0])]
        away_cols = [str(breakdown[1])]
    else:
        # Not decided yet doesn't mean nothing to show - the live sets-won
        # score is already sitting there mid-match.
        live_scores = scores365.main_scores(game)
        home_cols = [scores365.fmt_score(live_scores[0])] if live_scores else ["-"]
        away_cols = [scores365.fmt_score(live_scores[1])] if live_scores else ["-"]

    home_name = home_competitor.get("name", "?")
    away_name = away_competitor.get("name", "?")
    home_logo_url = scores365.competitor_logo_url(home_competitor)
    away_logo_url = scores365.competitor_logo_url(away_competitor)

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, home_logo_url, away_logo_url, home_cols, away_cols, period_text, card_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via 365scores")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(message: discord.Message, sport_id: int, game_id, channel_id: int, owner_id: int, team: str):
    key = track_key(channel_id, game_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            # A notstarted match's set score can't change before it starts,
            # so hibernate instead of polling every cycle - same pattern as
            # tracker.py/f5tracker.py.
            hibernated = False
            while game and scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                kickoff = scores365.start_epoch(game)
                if not kickoff:
                    break
                seconds_until_kickoff = kickoff - time.time()
                if seconds_until_kickoff <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                log.info("1st-set game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "1st-set game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, team)

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/f5tracker.py.
                try:
                    new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
                except discord.HTTPException as e:
                    log.warning("Failed to repost 1st-set tracking message near kickoff: %s", e)
                else:
                    try:
                        await new_message.add_reaction(TRASH_EMOJI)
                    except discord.HTTPException as e:
                        log.warning("Failed to react to reposted 1st-set tracking message: %s", e)
                    old_message = message
                    message = new_message
                    _message_owners.pop(old_message.id, None)
                    register_message(message.id, channel_id, game_id, owner_id)
                    _persist(channel_id, game_id, message.id, sport_id, owner_id, team)
                    try:
                        await old_message.delete()
                    except discord.HTTPException as e:
                        log.warning("Failed to delete old 1st-set tracking message after repost: %s", e)
                continue

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit 1st-set tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    break
                continue

            breakdown = scores365.tennis_first_set_result(game)
            if breakdown is not None:
                result = scores365.grade_tennis_set(game, breakdown[0], breakdown[1], team)
                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                await asyncio.sleep(POST_RESULT_DELETE_SECONDS)
                try:
                    await message.delete()
                except discord.HTTPException as e:
                    log.warning("Failed to delete finished 1st-set tracking message: %s", e)
                break
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id)


def start_tracking(message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int, team: str):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, sport_id, game_id, channel_id, owner_id, team))
    _active[key] = task
    register_message(message.id, channel_id, game_id, owner_id)
    _persist(channel_id, game_id, message.id, sport_id, owner_id, team)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_set1().values()):
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
        if not game:
            _forget(channel_id, game_id)
            continue

        start_tracking(message, sport_id, game, channel_id, owner_id, entry["team"])
        log.info("Resumed 1st-set tracking for game %s in channel %s", game_id, channel_id)

#!/usr/bin/env python3
"""
Manages background tasks for "F5" (First 5 Innings) picks - moneyline (one
side to be ahead through 5 innings), team total (one side's own 1st-5th
inning runs vs. a line), combined total (both sides summed vs. a line, same
as tracker.py's game total but scoped to just the first 5 innings), or
handicap/run-line (a team's own 1st-5th inning runs adjusted by a +/- line
before comparing against the other side's) - all settle once the 5th inning
is fully complete, not when the whole game finishes, so they don't share
tracker.py's wait-for-the-full-game design.

Backed by 365scores' per-game detail call (see scores365.innings_breakdown),
which works across every league 365scores covers under baseball (MLB, KBO,
etc.) - unlike espn.py's inning breakdown used for YRFI/NRFI, which is
hardcoded to the MLB endpoint only.

Mirrors tracker.py/inningtracker.py otherwise: hibernation before kickoff,
🗑️-reaction delete, restart-safe persistence, and a ✅/❌ result reaction once
the pick is graded.
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
import state
import throttle

log = logging.getLogger("scorebox.f5tracker")

MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"
THROUGH_INNING = 5

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "🚫 Voided (No Action)",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "void": "🚫"}


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_f5().values()
        if track_key(entry["channel_id"], entry["game_id"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, message_id: int, sport_id, owner_id: int, picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None, handicap_line: Optional[float] = None,
):
    data = state.load_f5()
    data[track_key(channel_id, game_id)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "picked_team": picked_team,
        "total_direction": total_direction, "total_line": total_line, "handicap_line": handicap_line,
    }
    state.save_f5(data)


def _forget(channel_id: int, game_id):
    data = state.load_f5()
    data.pop(track_key(channel_id, game_id), None)
    state.save_f5(data)


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


async def build_embed(
    game: dict, sport_id: Optional[int], picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None, handicap_line: Optional[float] = None,
) -> tuple[discord.Embed, discord.File]:
    """Exactly one of four modes applies per pick: picked_team + handicap_line
    grades that team's own 1st-5th inning runs adjusted by the line against
    the other side's; picked_team alone (no handicap/total) is an F5
    moneyline; picked_team + total_direction/total_line grades that team's
    own 1st-5th inning total against a line; total_direction/total_line with
    no picked_team grades the *combined* (both sides summed) 1st-5th inning
    total against a line instead."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))
    home_name = home_competitor.get("name", "?")
    away_name = away_competitor.get("name", "?")

    breakdown = await asyncio.to_thread(scores365.innings_breakdown, game.get("id"), THROUGH_INNING)
    decided = breakdown is not None
    if decided:
        if picked_team and handicap_line is not None:
            result = scores365.grade_f5_handicap(game, breakdown[0], breakdown[1], picked_team, handicap_line)
        elif picked_team and total_direction and total_line is not None:
            result = scores365.grade_f5_team_total(
                game, breakdown[0], breakdown[1], picked_team, total_direction, total_line
            )
        elif total_direction and total_line is not None:
            result = scores365.grade_f5_combined_total(breakdown[0], breakdown[1], total_direction, total_line)
        else:
            result = scores365.grade_f5_moneyline(game, breakdown[0], breakdown[1], picked_team)
    else:
        result = None

    embed_color = {"won": 0x2ECC71, "lost": 0xE74C3C, "push": 0x95A5A6}.get(result, 0x3498DB)
    embed = discord.Embed(color=embed_color)
    if result:
        embed.title = _RESULT_TITLES[result]
    elif status == "inprogress" and total_direction == "over" and total_line is not None:
        # Same early-win idea as tracker.py/proptracker.py's Over tagging -
        # a team's (or the combined) innings-1-5 run total only climbs as
        # innings complete, so once the partial total already clears the
        # line, the pick can't become anything but a win even before all 5
        # innings are done.
        if picked_team:
            partial = await asyncio.to_thread(
                scores365.partial_f5_team_total, game.get("id"), picked_team, home_name, away_name
            )
        else:
            partial = await asyncio.to_thread(scores365.partial_f5_combined_total, game.get("id"))
        if partial is not None and partial > total_line:
            embed.title = _RESULT_TITLES["won"]

    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    if picked_team and handicap_line is not None:
        description_lines = [f"{picked_team} F5 {handicap_line:+g}"]
    elif picked_team and total_direction and total_line is not None:
        description_lines = [f"{picked_team} F5 {total_direction.title()} {total_line:g}"]
    elif total_direction and total_line is not None:
        description_lines = [f"F5 {total_direction.title()} {total_line:g}"]
    else:
        description_lines = [f"{picked_team} F5 ML"]
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "F5 Final"
        card_status = "finished"
    elif status == "inprogress":
        period_text = scores365.status_line(game, sport_id)
        card_status = "inprogress"
    else:
        period_text = ""
        card_status = "notstarted"

    if decided:
        # Frozen at whatever the score was through the 5th inning - the
        # number that actually decided the pick - even though the game (and
        # its overall score) may keep going past this point.
        home_cols = [str(breakdown[0])]
        away_cols = [str(breakdown[1])]
    else:
        # Not decided yet doesn't mean nothing to show - the live overall
        # score is already sitting there mid-game (confirmed: this was
        # showing "-" through the entire 1st-5th innings instead of the
        # actual running score).
        live_scores = scores365.main_scores(game)
        home_cols = [scores365.fmt_score(live_scores[0])] if live_scores else ["-"]
        away_cols = [scores365.fmt_score(live_scores[1])] if live_scores else ["-"]

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


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    handicap_line: Optional[float] = None,
):
    key = track_key(channel_id, game_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel for a graded/voided
        result - same repost mechanics as the pre-kickoff bump below. Falls
        back to editing in place if the repost send itself fails."""
        nonlocal message
        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final F5 tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit F5 tracking message as a fallback: %s", e2)
            return
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final F5 tracking message: %s", e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old F5 tracking message after final repost: %s", e)

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            # A notstarted game's inning-by-inning score can't change before
            # it starts, so hibernate instead of polling every cycle - same
            # pattern as tracker.py.
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
                log.info("F5 game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "F5 game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (F5): game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, picked_team, total_direction, total_line, handicap_line)

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/proptracker.py.
                try:
                    new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
                except discord.HTTPException as e:
                    log.warning("Failed to repost F5 tracking message near kickoff: %s", e)
                else:
                    try:
                        await new_message.add_reaction(TRASH_EMOJI)
                    except discord.HTTPException as e:
                        log.warning("Failed to react to reposted F5 tracking message: %s", e)
                    old_message = message
                    message = new_message
                    _message_owners.pop(old_message.id, None)
                    register_message(message.id, channel_id, game_id, owner_id)
                    _persist(
                        channel_id, game_id, message.id, sport_id, owner_id, picked_team,
                        total_direction, total_line, handicap_line,
                    )
                    try:
                        await old_message.delete()
                    except discord.HTTPException as e:
                        log.warning("Failed to delete old F5 tracking message after repost: %s", e)
                continue

            breakdown = await asyncio.to_thread(scores365.innings_breakdown, game_id, THROUGH_INNING)
            if breakdown is not None:
                if picked_team and handicap_line is not None:
                    result = scores365.grade_f5_handicap(game, breakdown[0], breakdown[1], picked_team, handicap_line)
                elif picked_team and total_direction and total_line is not None:
                    result = scores365.grade_f5_team_total(
                        game, breakdown[0], breakdown[1], picked_team, total_direction, total_line
                    )
                elif total_direction and total_line is not None:
                    result = scores365.grade_f5_combined_total(breakdown[0], breakdown[1], total_direction, total_line)
                else:
                    result = scores365.grade_f5_moneyline(game, breakdown[0], breakdown[1], picked_team)

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
                    "Failed to edit F5 tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (F5): game `{game_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without F5 ever settling. If the game
            # was left mid-interruption (rain delay, etc.) rather than
            # genuinely still in progress, that's stalled, not just slow -
            # tag it Voided/No Action instead of silently leaving the card
            # stuck with no result and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(game, sport_id, picked_team, total_direction, total_line, handicap_line)
                embed.title = _RESULT_TITLES["void"]
                embed.color = 0x95A5A6
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                botlog.event(f"➖ Voided (F5, interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    handicap_line: Optional[float] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(
            message, sport_id, game_id, channel_id, owner_id, picked_team, total_direction, total_line, handicap_line
        )
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, owner_id)
    _persist(channel_id, game_id, message.id, sport_id, owner_id, picked_team, total_direction, total_line, handicap_line)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_f5().values()):
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

        start_tracking(
            message, sport_id, game, channel_id, owner_id, entry["picked_team"],
            entry.get("total_direction"), entry.get("total_line"), entry.get("handicap_line"),
        )
        log.info("Resumed F5 tracking for game %s in channel %s", game_id, channel_id)

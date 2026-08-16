#!/usr/bin/env python3
"""
Manages background tasks for KBO player prop picks - backed by
koreabaseball.py (eng.koreabaseball.com, the league's own official site).
See koreabaseball.py's own module docstring for why this exists separately
from proptracker.py (ESPN, this bot's only other player-prop source, has no
KBO league at all).

Unlike proptracker.py's live in-game boxscore, there's no live/in-progress
stat feed here at all - a player's game log only gets a new row once that
specific game is actually final. So this tracker has no genuine "live"
phase to poll through the way proptracker.py does: a pick is either still
waiting for its game to finish and post ("notstarted"), or graded
("finished") - closer to boxingtracker.py's simplicity than proptracker.py's
richer live-tracking shape.

A KBO prop is always about the game played on the KST calendar day it was
posted (same "a pick is about the day it was posted" assumption
pendingsoccerprops.py already makes for soccer) - target_date is fixed at
track-start time and never recomputed. Polls on a much longer interval than
config.UPDATE_INTERVAL_SECONDS (meant for a genuinely live game elsewhere in
this bot) since nothing here changes until the one game of the day is over.
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
import dailylog
import espn
import koreabaseball as kbo
import parlaytracker
import pendingdelete
import scoreimage
import state
import throttle

log = logging.getLogger("scorebox.kboproptracker")

# How often to check whether today's game log row has posted yet - far
# longer than a live tracker's poll interval, since nothing changes here
# until the one game of the day is over.
_POLL_INTERVAL_SECONDS = 20 * 60

MAX_CONSECUTIVE_MISSES = 3
MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, pcode, stat_label, direction, line, target_date, owner_id)
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖", "void": "<:cashback:1533844020839841832>"}

_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via KBO" if message_id else "Scorebox • data via KBO"


def track_key(channel_id: int, pcode: str, stat_label: str, direction: str, line: float, target_date: str) -> str:
    return f"{channel_id}:{pcode}:{stat_label}:{direction}:{line:g}:{target_date}"


def is_tracked(channel_id: int, pcode: str, stat_label: str, direction: str, line: float, target_date: str) -> bool:
    return track_key(channel_id, pcode, stat_label, direction, line, target_date) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_kbo_props().values()
        if track_key(
            entry["channel_id"], entry["pcode"], entry["stat_label"],
            entry["direction"], entry["line"], entry["target_date"],
        ) in active_keys
    ]


def register_message(
    message_id: int, channel_id: int, pcode: str, stat_label: str,
    direction: str, line: float, target_date: str, owner_id: int,
):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, pcode, stat_label, direction, line, target_date, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, pcode: str, stat_label: str, direction: str, line: float, target_date: str,
    player_name: str, team_name: Optional[str], is_pitcher: bool, message_id: int, owner_id: int,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    data = state.load_kbo_props()
    data[track_key(channel_id, pcode, stat_label, direction, line, target_date)] = {
        "channel_id": channel_id, "pcode": pcode, "stat_label": stat_label,
        "direction": direction, "line": line, "target_date": target_date,
        "player_name": player_name, "team_name": team_name, "is_pitcher": is_pitcher,
        "message_id": message_id, "owner_id": owner_id,
        "section": section, "label": label, "origin_channel_id": origin_channel_id,
    }
    state.save_kbo_props(data)


def _forget(channel_id: int, pcode: str, stat_label: str, direction: str, line: float, target_date: str):
    data = state.load_kbo_props()
    data.pop(track_key(channel_id, pcode, stat_label, direction, line, target_date), None)
    state.save_kbo_props(data)


def _forget_key(key: str):
    data = state.load_kbo_props()
    data.pop(key, None)
    state.save_kbo_props(data)


def stop_tracking(channel_id: int, pcode: str, stat_label: str, direction: str, line: float, target_date: str) -> bool:
    key = track_key(channel_id, pcode, stat_label, direction, line, target_date)
    task = _active.pop(key, None)
    _forget(channel_id, pcode, stat_label, direction, line, target_date)
    dailylog.record_result(channel_id, "kboproptracker", key, "void", "Manually untracked")
    for message_id, (c_id, pc, sl, drct, ln, td, _owner) in list(_message_owners.items()):
        if c_id == channel_id and pc == pcode and sl == stat_label and drct == direction and ln == line and td == target_date:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def build_embed(
    pcode: str, player_name: str, team_name: Optional[str], is_pitcher: bool, stat_label: str,
    direction: str, line: float, row: Optional[dict], target_date: Optional[str] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """row is the player's game-log row for the tracked target_date, or
    None if that game hasn't been played/posted yet - see
    koreabaseball.find_game_row.

    target_date is only used cosmetically (see koreabaseball.game_status's
    own docstring) - purely to show "Live" instead of misleadingly still
    looking not-started once the team's game has actually started, since
    there's no live per-player stat to show either way. Grading itself
    never depends on it, only on row."""
    result = None
    if row is not None:
        value = kbo.stat_value(row, stat_label, is_pitcher)
        result = espn.grade_over_under(value, direction, line)
        if result is None:
            # The game's final and posted, but this stat came back
            # unusable for it (a hitter with no AB in a game they still
            # appeared in as a pinch-runner, etc.) - can't grade it, so
            # void rather than sit "Final" with no real result forever.
            result = "void"
    else:
        value = None

    live_status = "notstarted"
    if row is None and not force_result and team_name and target_date:
        live_status = kbo.game_status(team_name, target_date)

    if force_result:
        color_status = force_result
    elif result:
        color_status = result
    elif row is not None:
        color_status = "finished"
    elif live_status == "inprogress":
        color_status = "inprogress"
    else:
        color_status = "notstarted"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if force_result == "void":
        embed.title = _RESULT_TITLES["void"]
    elif result:
        embed.title = _RESULT_TITLES[result]
    embed.set_author(name="KBO")
    embed.description = f"{player_name} {direction.title()} {line:g} {stat_label}"

    if row is not None:
        period_text = "Final"
    elif color_status == "inprogress":
        period_text = "Live"
    else:
        period_text = ""
    value_text = "-" if value is None else (str(int(value)) if float(value).is_integer() else f"{value:g}")
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        team_name or "?", kbo.photo_url(pcode), player_name, stat_label, value_text, color_status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, channel_id: int, pcode: str, stat_label: str,
    direction: str, line: float, target_date: str, player_name: str,
    team_name: Optional[str], is_pitcher: bool, owner_id: int,
):
    key = track_key(channel_id, pcode, stat_label, direction, line, target_date)
    # Bounded by the KST calendar, not a flat hour count - the game this
    # pick is about might not even start for many hours yet (a pick posted
    # in the morning for an evening game), so a plain "N hours from now"
    # deadline could time this out before the game even happens. Two
    # rollovers out from now (not "the day after target_date") covers a
    # pick that gets tracked well after target_date already started, same
    # margin-of-safety reasoning as pendingsoccerprops.py's identical
    # two-midnight bound.
    deadline = kbo.next_kst_midnight_epoch(kbo.next_kst_midnight_epoch())

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel once graded/voided -
        falls back to editing in place if the repost send itself fails.
        Carries forward any reaction someone added beyond the bot's own
        service ones - see tracker.py's identical helper for why this
        matters."""
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final KBO prop tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit KBO prop tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final KBO prop tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, pcode, stat_label, direction, line, target_date, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old KBO prop tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        """Called on every path where this tracker gives up without ever
        reaching a real result - see tracker.py's identical helper for why
        this matters."""
        dailylog.record_result(channel_id, "kboproptracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "kboproptracker", key)
        if not group_ids:
            return
        pick_desc = f"{player_name} {direction.title()} {line:g} {stat_label}"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "kboproptracker", key, pick_desc, "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, _POLL_INTERVAL_SECONDS))
    try:
        while time.time() < deadline:
            try:
                row = await asyncio.to_thread(kbo.find_game_row, pcode, is_pitcher, target_date)
            except kbo.KboError as e:
                log.warning("KBO game log fetch failed for %s: %s", player_name, e)
                consecutive_misses += 1
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (KBO prop): **{player_name}** — couldn't reach koreabaseball.com {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Couldn't reach koreabaseball.com repeatedly")
                    break
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue
            consecutive_misses = 0

            embed, file = await build_embed(
                pcode, player_name, team_name, is_pitcher, stat_label, direction, line, row,
                target_date=target_date, message_id=message.id,
            )
            leg_label = f"{player_name} {direction.title()} {line:g} {stat_label}"

            if row is not None:
                # Bump the graded result to the bottom of the channel
                # instead of editing in place - same reasoning as every
                # other tracker's identical pre-grade-to-bottom move: the
                # card can easily be buried under chat by the time the
                # game's actually final and posted, many hours later.
                await _repost_final(embed, file)

                value = kbo.stat_value(row, stat_label, is_pitcher)
                result = espn.grade_over_under(value, direction, line)
                if result is None:
                    result = "void"
                    botlog.event(f"➖ Voided (KBO prop, no usable value in the final box score): **{player_name}** in <#{channel_id}>")
                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                dailylog.record_result(channel_id, "kboproptracker", key, result, "No usable stat value" if result == "void" else None)
                group_ids = parlaytracker.groups_for_leg(channel_id, "kboproptracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "kboproptracker", key, leg_label, result, group_ids,
                )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            dailylog.touch(channel_id, "kboproptracker", key, "NOT STARTED - waiting for today's game")
            group_ids = parlaytracker.groups_for_leg(channel_id, "kboproptracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "kboproptracker", key, leg_label,
                    "NOT STARTED - waiting for today's game", group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
            except discord.HTTPException as e:
                if e.status == 429:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit KBO prop tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (KBO prop): **{player_name}** — message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit KBO prop tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (KBO prop): **{player_name}** — message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        else:
            botlog.event(f"⚠️ Auto-stopped tracking (KBO prop): **{player_name}** — game for {target_date} never posted within the expected window, in <#{channel_id}>")
            await _void_leg_and_give_up("Game never posted within the expected window")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists - an
        # unguarded exception here used to kill the task silently, freezing
        # the card forever with no Discord/botlog signal.
        log.exception("KBO prop tracker crashed unexpectedly for %s in channel %s", player_name, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (KBO prop): **{player_name}** — crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, pcode, stat_label, direction, line, target_date)


def start_tracking(
    message: discord.Message, channel_id: int, pcode: str, stat_label: str,
    direction: str, line: float, target_date: str, player_name: str,
    team_name: Optional[str], is_pitcher: bool, owner_id: int,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    key = track_key(channel_id, pcode, stat_label, direction, line, target_date)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(
            message, channel_id, pcode, stat_label, direction, line, target_date,
            player_name, team_name, is_pitcher, owner_id,
        )
    )
    _active[key] = task
    register_message(message.id, channel_id, pcode, stat_label, direction, line, target_date, owner_id)
    _persist(
        channel_id, pcode, stat_label, direction, line, target_date,
        player_name, team_name, is_pitcher, message.id, owner_id, section, label, origin_channel_id,
    )
    dailylog.record_pick(channel_id, "kboproptracker", key, section, label or player_name, message.id, origin_channel_id, sport="KBO", tournament="KBO")


async def resume_all(client: discord.Client):
    """Called once from on_ready. Re-arms every still-tracked KBO prop so a
    restart doesn't lose it - unlike a live-event tracker there's no
    "event" to re-fetch first, just the same message to find and hand
    straight back to start_tracking."""
    for key, entry in list(state.load_kbo_props().items()):
        try:
            channel_id, pcode, stat_label = entry["channel_id"], entry["pcode"], entry["stat_label"]
            direction, line, target_date = entry["direction"], entry["line"], entry["target_date"]
        except KeyError:
            log.warning("Dropping KBO prop entry from an incompatible state schema: %r", entry)
            continue
        message = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            try:
                channel = await client.fetch_channel(channel_id)
                message = await channel.fetch_message(entry["message_id"])
                break
            except (discord.NotFound, discord.Forbidden):
                break
            except discord.HTTPException:
                if attempt < MAX_CONSECUTIVE_MISSES - 1:
                    await asyncio.sleep(5)
        if message is None:
            botlog.event(f"⚠️ Dropped on resume (KBO prop): **{entry.get('player_name', pcode)}** — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(
            message, channel_id, pcode, stat_label, direction, line, target_date,
            entry["player_name"], entry.get("team_name"), entry["is_pitcher"], entry.get("owner_id"),
        )
        log.info("Resumed KBO prop tracking for %s in channel %s", entry["player_name"], channel_id)

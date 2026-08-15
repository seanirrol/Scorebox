#!/usr/bin/env python3
"""
Manages background tasks for boxing moneyline picks - backed by boxing.py
(BoxingScene.com). Moneyline only, no round totals/method of victory -
BoxingScene's data doesn't expose method of victory reliably enough to
grade, and there's no scope yet asked for round totals here (unlike
espn_ufc.py's UFC round-total support).

A boxing fighter is its own "competitor" within a fight, same model as
espn_ufc.py's UFC bouts - no team/roster concept to resolve. Unlike UFC,
there's no live round/clock data anywhere in BoxingScene's scraped pages
(see boxing.py's own module docstring) - a fight only ever reports
"notstarted" or "finished", so the live in-between state just shows a
bare "Live" with no further detail, rather than the round/clock text
espn_ufc.py's status_text produces.

Mirrors ufctracker.py's design otherwise: hibernation before start (a
notstarted fight's result can't change), 🗑️-reaction delete, restart-safe
persistence, and a Won/Lost/Push result reaction once the fight finishes.
Every lookup here (including a tracker's own repeated polling) re-resolves
the fight by fighter name each time - boxing.py has no stable refetch-by-id
call, same architecture as esports.py's own trackers.
"""

import asyncio
import io
import logging
import random
import time
from typing import Optional

import discord

import botlog
import boxing
import config
import dailylog
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.boxingtracker")

MAX_CONSECUTIVE_MISSES = 3
MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, fight_id, fighter_id, owner_id) - lets the
# reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {"won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost", "push": "➖ Push"}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via BoxingScene" if message_id else "Scorebox • data via BoxingScene"


def track_key(channel_id: int, fight_id, fighter_id) -> str:
    return f"{channel_id}:{fight_id}:ml:{fighter_id}"


def is_tracked(channel_id: int, fight_id, fighter_id) -> bool:
    return track_key(channel_id, fight_id, fighter_id) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_boxing().values()
        if track_key(entry["channel_id"], entry["fight_id"], entry["fighter_id"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, fight_id, fighter_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, fight_id, fighter_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, fight_id, fighter_id, fighter_name: str, message_id: int, owner_id: int,
    event_name: str, section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """section/label/origin_channel_id are persisted purely so resume_all
    can hand them back to dailylog.record_pick on restart - see
    tracker.py's _persist for why this matters."""
    data = state.load_boxing()
    data[track_key(channel_id, fight_id, fighter_id)] = {
        "channel_id": channel_id, "fight_id": fight_id, "fighter_id": fighter_id, "fighter_name": fighter_name,
        "message_id": message_id, "owner_id": owner_id, "event_name": event_name,
        "section": section, "label": label, "origin_channel_id": origin_channel_id,
    }
    state.save_boxing(data)


def _forget(channel_id: int, fight_id, fighter_id):
    data = state.load_boxing()
    data.pop(track_key(channel_id, fight_id, fighter_id), None)
    state.save_boxing(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_boxing()
    data.pop(key, None)
    state.save_boxing(data)


def stop_tracking(channel_id: int, fight_id, fighter_id) -> bool:
    key = track_key(channel_id, fight_id, fighter_id)
    task = _active.pop(key, None)
    _forget(channel_id, fight_id, fighter_id)
    dailylog.record_result(channel_id, "boxingtracker", key, "void", "Manually untracked")
    for message_id, (c_id, fid, fighter, _owner) in list(_message_owners.items()):
        if c_id == channel_id and fid == fight_id and fighter == fighter_id:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def build_embed(
    fight_data: dict, fighter_id, fighter_name: str, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    decided = boxing.is_finished(fight_data)
    if decided:
        result = boxing.grade_boxing_moneyline(fight_data, fighter_name)
    else:
        result = None

    if result:
        color_status = result
    elif decided:
        color_status = "finished"
    elif fight_data.get("start_epoch") and fight_data["start_epoch"] <= time.time():
        color_status = "inprogress"
    else:
        color_status = "notstarted"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]
    embed.set_author(name=f"Boxing • {fight_data.get('event_name') or ''}")

    description_lines = [f"{fighter_name} ML"]
    if not decided and fight_data.get("start_epoch"):
        description_lines.append(f"<t:{int(fight_data['start_epoch'])}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "Final"
    elif color_status == "inprogress":
        period_text = "Live"
    else:
        period_text = ""

    name_a, name_b = fight_data.get("fighter1_name") or "?", fight_data.get("fighter2_name") or "?"
    if decided:
        winner_id = fight_data.get("winning_fighter_id")
        home_cols = ["W"] if winner_id == fight_data.get("fighter1_id") else (["L"] if winner_id is not None else ["-"])
        away_cols = ["W"] if winner_id == fight_data.get("fighter2_id") else (["L"] if winner_id is not None else ["-"])
    else:
        home_cols = away_cols = ["-"]

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        name_a, name_b, None, None, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, channel_id: int, fight_id, fighter_id, fighter_name: str, owner_id: int, event_name: str,
):
    key = track_key(channel_id, fight_id, fighter_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-fight or
        graded) - falls back to editing in place if the repost send itself
        fails. Carries forward any reaction someone added beyond the bot's
        own service ones - see ufctracker.py's identical helper for why
        this matters."""
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final boxing tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit boxing tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final boxing tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, fight_id, fighter_id, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old boxing tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        """Called on every path where this tracker gives up without ever
        reaching a real result - see tracker.py's identical helper for why
        this matters."""
        dailylog.record_result(channel_id, "boxingtracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "boxingtracker", key)
        if not group_ids:
            return
        matchup = f"{refreshed['fighter1_name']} vs {refreshed['fighter2_name']}" if refreshed else event_name
        void_label = f"{matchup} - {fighter_name} ML"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "boxingtracker", key, void_label, "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    refreshed = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            try:
                refreshed = await asyncio.to_thread(boxing.find_boxing_fight, fighter_name)
            except boxing.BoxingError as e:
                log.warning("Boxing fetch failed for %s: %s", fighter_name, e)
                refreshed = None

            # A notstarted fight's result can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
            # tracker.py/ufctracker.py.
            hibernated = False
            while refreshed and refreshed["status"] == "notstarted" and refreshed.get("start_epoch"):
                kickoff = refreshed["start_epoch"]
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True

                dailylog.touch(channel_id, "boxingtracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "boxingtracker", key)
                if group_ids:
                    pre_matchup = f"{refreshed['fighter1_name']} vs {refreshed['fighter2_name']}"
                    pre_label = f"{pre_matchup} - {fighter_name} ML"
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "boxingtracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Boxing fight %s not starting soon; hibernating %.0fs", fight_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                try:
                    refreshed = await asyncio.to_thread(boxing.find_boxing_fight, fighter_name)
                except boxing.BoxingError as e:
                    log.warning("Boxing fetch failed for %s: %s", fighter_name, e)
                    refreshed = None

            if not refreshed:
                consecutive_misses += 1
                log.warning(
                    "Boxing fight %s not found on BoxingScene (miss %d/%d)",
                    fight_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (Boxing): fight `{fight_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Fight not found on BoxingScene")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(refreshed, fighter_id, fighter_name, message_id=message.id)
            leg_matchup = f"{refreshed['fighter1_name']} vs {refreshed['fighter2_name']}"
            leg_label = f"{leg_matchup} - {fighter_name} ML"

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                await _repost_final(embed, file)
                _persist(channel_id, fight_id, fighter_id, fighter_name, message.id, owner_id, event_name)
                continue

            if boxing.is_finished(refreshed):
                result = boxing.grade_boxing_moneyline(refreshed, fighter_name)

                # Bump the graded result to the bottom of the channel instead
                # of editing in place - same reasoning as the pre-fight bump
                # above.
                await _repost_final(embed, file)

                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                if result:
                    dailylog.record_result(channel_id, "boxingtracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "boxingtracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "boxingtracker", key, leg_label, result, group_ids,
                    )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            detail = (
                f"NOT STARTED - <t:{int(refreshed['start_epoch'])}:f>"
                if refreshed["status"] == "notstarted" and refreshed.get("start_epoch")
                else "LIVE"
            )
            dailylog.touch(channel_id, "boxingtracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "boxingtracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "boxingtracker", key, leg_label, detail, group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
            except discord.HTTPException as e:
                if e.status == 429:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit boxing tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (Boxing): fight `{fight_id}` message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit boxing tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (Boxing): fight `{fight_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the fight ever settling - see
            # ufctracker.py's identical branch for why a parlay leg still
            # gets Voided even though a standalone card is left alone.
            botlog.event(f"⚠️ Auto-stopped tracking (Boxing): fight `{fight_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
            await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Boxing tracker crashed unexpectedly for fight %s in channel %s", fight_id, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (Boxing): fight `{fight_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, fight_id, fighter_id)


def start_tracking(
    message: discord.Message, channel_id: int, fight_id, fighter_id, fighter_name: str, owner_id: int, event_name: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    key = track_key(channel_id, fight_id, fighter_id)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, channel_id, fight_id, fighter_id, fighter_name, owner_id, event_name))
    _active[key] = task
    register_message(message.id, channel_id, fight_id, fighter_id, owner_id)
    if not label:
        label = f"{fighter_name} ML"
    _persist(channel_id, fight_id, fighter_id, fighter_name, message.id, owner_id, event_name, section, label, origin_channel_id)
    dailylog.record_pick(channel_id, "boxingtracker", key, section, label, message.id, origin_channel_id, sport="Boxing")


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/fight is gone - cleans up instead.
    """
    for key, entry in list(state.load_boxing().items()):
        try:
            channel_id, fight_id, fighter_id, fighter_name = (
                entry["channel_id"], entry["fight_id"], entry["fighter_id"], entry["fighter_name"]
            )
        except KeyError:
            log.warning("Dropping boxing entry from an incompatible state schema: %r", entry)
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
                # A bare HTTPException (rate limit, transient 5xx) used to
                # drop the pick with zero retry - see tracker.py's identical
                # resume_all fix for why this matters.
                if attempt < MAX_CONSECUTIVE_MISSES - 1:
                    await asyncio.sleep(5)
        if message is None:
            botlog.event(f"⚠️ Dropped on resume (Boxing): fight `{fight_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        refreshed = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            try:
                refreshed = await asyncio.to_thread(boxing.find_boxing_fight, fighter_name)
            except boxing.BoxingError:
                refreshed = None
            if refreshed:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not refreshed:
            botlog.event(f"⚠️ Dropped on resume (Boxing): fight `{fight_id}` not found on BoxingScene after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue
        event_name = entry.get("event_name") or refreshed.get("event_name") or ""

        start_tracking(
            message, channel_id, fight_id, fighter_id, fighter_name, entry.get("owner_id"), event_name,
            entry.get("section"), entry.get("label"), entry.get("origin_channel_id"),
        )
        log.info("Resumed boxing tracking for fight %s in channel %s", fight_id, channel_id)

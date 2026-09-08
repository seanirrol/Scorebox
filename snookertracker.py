#!/usr/bin/env python3
"""
Manages background tasks for snooker picks - backed by snooker.py
(scores24.live), same as tabletennistracker.py is for table tennis. Two
markets: "winner" (moneyline) and "frame_handicap" (frames won, signed
line) - see snooker.grade_moneyline/grade_frame_handicap.

No hibernation, no auto-track retry queue - same reasoning as
tabletennistracker.py's own docstring (a match this provider will ever
have is already live/imminent by the time a pick comes in).

Otherwise mirrors tabletennistracker.py exactly: 🗑️-reaction delete,
restart-safe persistence, and a Won/Lost/Push/Voided result reaction once
graded.
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
import parlaytracker
import pendingdelete
import scoreimage
import snooker
import state
import throttle

log = logging.getLogger("scorebox.snookertracker")

MAX_CONSECUTIVE_MISSES = 6  # see tracker.py's own comment
MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # see tracker.py's own comment
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, match_id, market, team, owner_id) - lets the
# reaction-based delete handler in bot.py look up who's allowed to delete a
# given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖", "void": "<:cashback:1533844020839841832>"}
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}

# Both markets are per-player - unlike, say, a combined frame total, "team"
# genuinely disambiguates two different bets on the same match.
_PER_PLAYER_MARKETS = {"winner", "frame_handicap"}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via scores24.live" if message_id else "Scorebox • data via scores24.live"


def track_key(channel_id: int, match_id, market: str, team: Optional[str] = None) -> str:
    disambiguator = team if market in _PER_PLAYER_MARKETS else None
    return f"{channel_id}:{match_id}:{market}:{disambiguator}"


def is_tracked(channel_id: int, match_id, market: str, team: Optional[str] = None) -> bool:
    return track_key(channel_id, match_id, market, team) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_snooker().values()
        if track_key(entry["channel_id"], entry["match_id"], entry["market"], entry.get("team")) in active_keys
    ]


def register_message(message_id: int, channel_id: int, match_id, market: str, team: Optional[str], owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, match_id, market, team, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, match_id, market: str, message_id: int, owner_id: int,
    team: Optional[str] = None, line: Optional[float] = None, match_slug: Optional[str] = None,
):
    data = state.load_snooker()
    data[track_key(channel_id, match_id, market, team)] = {
        "channel_id": channel_id, "match_id": match_id, "market": market, "message_id": message_id,
        "owner_id": owner_id, "team": team, "line": line, "match_slug": match_slug,
    }
    state.save_snooker(data)


def _forget(channel_id: int, match_id, market: str, team: Optional[str] = None):
    data = state.load_snooker()
    data.pop(track_key(channel_id, match_id, market, team), None)
    state.save_snooker(data)


def _forget_key(key: str):
    data = state.load_snooker()
    data.pop(key, None)
    state.save_snooker(data)


def stop_tracking(channel_id: int, match_id, market: str, team: Optional[str] = None) -> bool:
    key = track_key(channel_id, match_id, market, team)
    task = _active.pop(key, None)
    _forget(channel_id, match_id, market, team)
    dailylog.record_result(channel_id, "snookertracker", key, "void", "Manually untracked")
    for message_id, (c_id, m_id, m, t, _owner) in list(_message_owners.items()):
        if c_id == channel_id and m_id == match_id and m == market and t == team:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def pick_label(market: str, team: str, line: Optional[float] = None) -> str:
    if market == "frame_handicap":
        return f"{team} {line:+g} Frames"
    return f"{team} Winner"


def _grade(match: dict, market: str, team: str, line: Optional[float]) -> Optional[str]:
    if market == "frame_handicap":
        return snooker.grade_frame_handicap(match, team, line)
    return snooker.grade_moneyline(match, team)


async def build_embed(
    match: dict, market: str, team: str, line: Optional[float] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    decided = snooker.is_finished(match)
    result = _grade(match, market, team, line) if decided else None

    if force_result:
        color_status = force_result
    elif result:
        color_status = result
    elif snooker.is_live(match):
        color_status = "inprogress"
    elif decided:
        color_status = "finished"
    else:
        color_status = "notstarted"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]

    tournament = match.get("unique_tournament_name") or match.get("league_slug")
    author_bits = [b for b in ("Snooker", tournament) if b]
    embed.set_author(name=" • ".join(author_bits))
    embed.description = pick_label(market, team, line)

    period_text = snooker.status_line(match)

    frames = snooker.frames_won(match)
    home_cols = [str(frames[0])] if frames else ["-"]
    away_cols = [str(frames[1])] if frames else ["-"]
    current = snooker.current_frame_score(match)
    if current:
        home_cols.append(str(current[0]))
        away_cols.append(str(current[1]))

    teams = match.get("teams") or []
    home_name = teams[0].get("name", "?") if len(teams) > 0 else "?"
    away_name = teams[1].get("name", "?") if len(teams) > 1 else "?"

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, None, None, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, match: dict, channel_id: int, market: str, owner_id: int,
    team: str, line: Optional[float] = None,
):
    match_id = match["id"]
    key = track_key(channel_id, match_id, market, team)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0
    last_sent_image_bytes = None

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel once graded - same
        "something significant happened, resurface it" reasoning as every
        other tracker's own _repost_final."""
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final snooker tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit snooker tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final snooker tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, match_id, market, team, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old snooker tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        dailylog.record_result(channel_id, "snookertracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "snookertracker", key)
        if not group_ids:
            return
        teams = current_match.get("teams") or []
        if len(teams) == 2:
            matchup = f"{teams[0].get('name', '?')} vs {teams[1].get('name', '?')}"
        else:
            matchup = f"Match `{match_id}`"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "snookertracker", key,
            f"{matchup} - {pick_label(market, team, line)}", "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    current_match = match
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            updated = await asyncio.to_thread(snooker.get_live_update, current_match)
            if not updated:
                consecutive_misses += 1
                log.warning(
                    "Snooker match %s not found on scores24.live (miss %d/%d)",
                    match_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (snooker): match `{match_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Match not found on scores24.live")
                    break
                continue
            current_match = updated
            consecutive_misses = 0

            embed, file = await build_embed(current_match, market, team, line, message_id=message.id)
            teams = current_match.get("teams") or []
            leg_matchup = f"{teams[0].get('name', '?')} vs {teams[1].get('name', '?')}" if len(teams) == 2 else f"Match `{match_id}`"
            leg_pick = embed.description.splitlines()[0] if embed.description else None
            leg_label = f"{leg_matchup} - {leg_pick}" if leg_pick and leg_pick != leg_matchup else leg_matchup

            if snooker.is_finished(current_match):
                await _repost_final(embed, file)
                result = _grade(current_match, market, team, line)
                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                if result:
                    dailylog.record_result(channel_id, "snookertracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "snookertracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "snookertracker", key, leg_label, result, group_ids,
                    )
                break

            detail = f"LIVE, {snooker.status_line(current_match)}" if snooker.is_live(current_match) else "NOT STARTED"
            dailylog.touch(channel_id, "snookertracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "snookertracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "snookertracker", key, leg_label, detail, group_ids,
                )

            # Skips a redundant image re-upload (and the Discord edit call
            # entirely) when the rendered card is byte-identical to what's
            # already posted - same fix as tabletennistracker.py's own
            # identical guard.
            image_bytes = file.fp.getvalue()
            if image_bytes == last_sent_image_bytes:
                continue

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
                last_sent_image_bytes = image_bytes
            except discord.HTTPException as e:
                if e.status == 429 or e.code == 400009:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit snooker tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (snooker): match `{match_id}` message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit snooker tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (snooker): match `{match_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            botlog.event(f"⚠️ Auto-stopped tracking (snooker): match `{match_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
            await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Snooker tracker crashed unexpectedly for match %s in channel %s", match_id, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (snooker): match `{match_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, match_id, market, team)


def start_tracking(
    message: discord.Message, match: dict, channel_id: int, market: str, owner_id: int,
    team: str, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    match_id = match["id"]
    key = track_key(channel_id, match_id, market, team)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, match, channel_id, market, owner_id, team, line))
    _active[key] = task
    register_message(message.id, channel_id, match_id, market, team, owner_id)
    _persist(channel_id, match_id, market, message.id, owner_id, team, line, match.get("slug"))
    dailylog.record_pick(
        channel_id, "snookertracker", key, section, label or pick_label(market, team, line), message.id,
        origin_channel_id, sport="Snooker", tournament=match.get("unique_tournament_name"), game_date=None,
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/match is gone - cleans up instead.
    """
    for key, entry in list(state.load_snooker().items()):
        try:
            channel_id, match_id, market, message_id = (
                entry["channel_id"], entry["match_id"], entry["market"], entry["message_id"],
            )
        except KeyError:
            log.warning("Dropping snooker entry from an incompatible state schema: %r", entry)
            continue
        owner_id = entry.get("owner_id")
        team = entry.get("team")
        line = entry.get("line")
        match_slug = entry.get("match_slug")

        message = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            try:
                channel = await client.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                break
            except (discord.NotFound, discord.Forbidden):
                break
            except discord.HTTPException:
                if attempt < MAX_CONSECUTIVE_MISSES - 1:
                    await asyncio.sleep(5)
        if message is None:
            botlog.event(f"⚠️ Dropped on resume (snooker): match `{match_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        match = None
        if match_slug:
            for attempt in range(MAX_CONSECUTIVE_MISSES):
                match = await asyncio.to_thread(snooker.get_live_update, {"slug": match_slug})
                if match:
                    break
                if attempt < MAX_CONSECUTIVE_MISSES - 1:
                    await asyncio.sleep(5)
        if not match:
            botlog.event(f"⚠️ Dropped on resume (snooker): match `{match_id}` not found on scores24.live, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(message, match, channel_id, market, owner_id, team, line)
        log.info("Resumed snooker tracking for match %s in channel %s", match_id, channel_id)

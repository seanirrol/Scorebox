#!/usr/bin/env python3
"""
Manages background tasks for soccer "Double Chance" picks - a market
covering two of the three possible full-time outcomes (home win / draw /
away win) in one pick, e.g. "Paris FC or Draw" ("1X"), "Draw or Nice"
("X2"), or "Paris FC or Nice" ("12" - anyone but a draw). Settles once the
WHOLE match finishes, not a partial-game signal - see
scores365.grade_double_chance for the actual grading rule (full-time score,
not isWinner, so a cup match decided by penalties still settles on the
scoreboard result).

Backed by the same bulk live-update polling as tracker.py (scores365.
get_live_update/main_scores) - no separate per-game detail call needed,
unlike inning1tracker.py's own innings_breakdown.

Mirrors tracker.py/inning1tracker.py otherwise: hibernation before kickoff,
🗑️-reaction delete, restart-safe persistence, and a win/loss result reaction
once the pick is graded.
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
import mergetracker
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.doublechancetracker")

MAX_CONSECUTIVE_MISSES = 6  # raised from 3 - see tracker.py's own comment

MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "void": "<:cashback:1533844020839841832>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via 365scores" if message_id else "Scorebox • data via 365scores"


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_double_chance().values()
        if track_key(entry["channel_id"], entry["game_id"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(channel_id: int, game_id, message_id: int, sport_id, owner_id: int, team: str, covered: tuple):
    data = state.load_double_chance()
    data[track_key(channel_id, game_id)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "team": team, "covered": list(covered),
    }
    state.save_double_chance(data)


def _forget(channel_id: int, game_id):
    data = state.load_double_chance()
    data.pop(track_key(channel_id, game_id), None)
    state.save_double_chance(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_double_chance()
    data.pop(key, None)
    state.save_double_chance(data)


def stop_tracking(channel_id: int, game_id) -> bool:
    key = track_key(channel_id, game_id)
    task = _active.pop(key, None)
    _forget(channel_id, game_id)
    dailylog.record_result(channel_id, "doublechancetracker", key, "void", "Manually untracked")
    for message_id, (c_id, g_id, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def pick_label(covered: tuple) -> str:
    side_a, side_b = ("Draw" if s == "DRAW" else s for s in covered)
    return f"{side_a} or {side_b}"


async def build_embed(
    game: dict, sport_id: Optional[int], team: str, covered: tuple, force_result: Optional[str] = None,
    message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """team is used to locate the game (either side works); covered is the
    two full-time outcomes this pick backs (see scores365.grade_double_
    chance).

    force_result overrides the color/title as if this were already graded
    that way, regardless of the game's actual live status - used only by
    _track_loop's interrupted-and-never-resumed timeout branch."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))

    decided = scores365.is_finished(game)
    result = scores365.grade_double_chance(game, covered) if decided else None

    if force_result:
        color_status = force_result
    elif result:
        color_status = result
    elif status in ("notstarted", "finished"):
        color_status = status
    else:
        color_status = "inprogress"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]

    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    description_lines = [f"Double Chance: {pick_label(covered)}"]
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    period_text = "Final" if decided else (scores365.status_line(game, sport_id) if status == "inprogress" else "")

    live_scores = scores365.main_scores(game)
    home_cols = [scores365.fmt_score(live_scores[0])] if live_scores else ["-"]
    away_cols = [scores365.fmt_score(live_scores[1])] if live_scores else ["-"]

    home_name = home_competitor.get("name", "?")
    away_name = away_competitor.get("name", "?")
    home_logo_url = scores365.competitor_logo_url(home_competitor)
    away_logo_url = scores365.competitor_logo_url(away_competitor)

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, home_logo_url, away_logo_url, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, owner_id: int, team: str, covered: tuple
):
    key = track_key(channel_id, game_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-kickoff, graded,
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
            log.warning("Failed to repost final double-chance tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit double-chance tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final double-chance tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old double-chance tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        """Called on every path where this tracker gives up without ever
        reaching a real result (game never found again, Discord edits
        failing repeatedly, or a MAX_TRACK_HOURS timeout with no interrupted
        signal to go on) - reports the leg as Voided to its parlay group
        instead of leaving the summary card frozen on whatever pending
        detail it last reported, forever, once this task quietly stops
        polling. reason is shown in /summary - see tracker.py's identical
        helper for why this matters."""
        dailylog.record_result(channel_id, "doublechancetracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
        if not group_ids:
            return
        if game:
            matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
        else:
            matchup = f"Game `{game_id}`"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "doublechancetracker", key,
            f"{matchup} - Double Chance: {pick_label(covered)}", "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    last_sent_image_bytes = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            # A notstarted game's score can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
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

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation ends
                # (right before kickoff) is too late: a leg with a kickoff
                # hours away would still never appear on its parlay's
                # summary card until it was basically already live anyway.
                dailylog.touch(channel_id, "doublechancetracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
                if group_ids:
                    pre_embed, _pre_file = await build_embed(game, sport_id, team, covered, message_id=message.id)
                    pre_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
                    pre_pick = pre_embed.description.splitlines()[0] if pre_embed.description else None
                    pre_label = f"{pre_matchup} - {pre_pick}" if pre_pick and pre_pick != pre_matchup else pre_matchup
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "doublechancetracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Double-chance game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "Double-chance game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (double chance): game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Game not found on 365scores")
                    break
                continue
            consecutive_misses = 0

            # Checked once per cycle, then threaded through every branch
            # below as `if not merge_key: <touch the individual card>` - see
            # tracker.py's identical guard for why a leg's own parlay
            # reporting must keep running unconditionally even once merged
            # (a leg can be merged AND in a parlay at the same time,
            # independently of each other).
            merge_key = mergetracker.merged_into(channel_id, "doublechancetracker", key)

            embed, file = await build_embed(game, sport_id, team, covered, message_id=message.id)
            leg_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            leg_pick = embed.description.splitlines()[0] if embed.description else None
            leg_label = f"{leg_matchup} - {leg_pick}" if leg_pick and leg_pick != leg_matchup else leg_matchup

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/f5tracker.py.
                if not merge_key:
                    await _repost_final(embed, file)
                    _persist(channel_id, game_id, message.id, sport_id, owner_id, team, covered)
                continue

            decided = scores365.is_finished(game)
            if decided:
                result = scores365.grade_double_chance(game, covered)
                if not merge_key:
                    await _repost_final(embed, file)

                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                    pendingdelete.start(channel_id, message, embed.description or "")
                if result:
                    dailylog.record_result(channel_id, "doublechancetracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "doublechancetracker", key, leg_label, result, group_ids,
                    )
                    if merge_key:
                        await mergetracker.handle_leg_result(
                            message.channel, channel_id, merge_key, "doublechancetracker", key, result,
                        )
                break

            if scores365.is_cancelled(game):
                # A cancelled game will never finish - see f5tracker.py's
                # identical fix for why this matters (the loop above would
                # otherwise wait here forever, showing a misleading "LIVE"
                # detail on a match that's never resuming).
                if not merge_key:
                    void_embed, void_file = await build_embed(
                        game, sport_id, team, covered, force_result="void", message_id=message.id,
                    )
                    void_embed.title = _RESULT_TITLES["void"]
                    await _repost_final(void_embed, void_file)
                    try:
                        await message.add_reaction(_RESULT_REACTIONS["void"])
                    except discord.HTTPException as e:
                        log.warning("Failed to add void reaction: %s", e)
                    pendingdelete.start(channel_id, message, void_embed.description or "")
                dailylog.record_result(channel_id, "doublechancetracker", key, "void", "Cancelled")
                group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "doublechancetracker", key, leg_label, "void", group_ids,
                )
                if merge_key:
                    await mergetracker.handle_leg_result(
                        message.channel, channel_id, merge_key, "doublechancetracker", key, "void",
                    )
                botlog.event(f"➖ Voided (double chance, cancelled): game `{game_id}` in <#{channel_id}>")
                break

            kickoff = scores365.start_epoch(game)
            if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
            else:
                detail = f"LIVE, {scores365.status_line(game, sport_id)}"
            dailylog.touch(channel_id, "doublechancetracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "doublechancetracker", key, leg_label, detail, group_ids,
                )
            if merge_key:
                await mergetracker.report_leg(
                    message.channel, channel_id, merge_key, "doublechancetracker", key, detail, game, sport_id,
                )
                continue

            # Skips a redundant image re-upload (and the Discord edit call
            # entirely) when the rendered card is byte-identical to what's
            # already posted - same fix as boxingtracker.py's identical
            # guard (this was tripping Discord's error 400009).
            image_bytes = file.fp.getvalue()
            if image_bytes == last_sent_image_bytes:
                continue

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
                last_sent_image_bytes = image_bytes
            except discord.HTTPException as e:
                # 400009 ("message edit attachment upload limit") is
                # Discord's own congestion signal this generous budget
                # exists for - never a literal 429 though, always a 400
                # with this code embedded. Confirmed live elsewhere in
                # this bot: without this, it hit the much stricter
                # MAX_CONSECUTIVE_MISSES budget instead and voided a pick
                # after just 3 failures, freezing its card.
                if e.status == 429 or e.code == 400009:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit double-chance tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (double chance): game `{game_id}` message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit double-chance tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (double chance): game `{game_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the match ever finishing. If
            # the game was left mid-interruption (rain/weather, crowd
            # trouble, etc.) rather than genuinely still in progress,
            # that's stalled, not just slow - tag it Voided/No Action
            # instead of silently leaving the card stuck with no result
            # and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(
                    game, sport_id, team, covered, force_result="void", message_id=message.id,
                )
                embed.title = _RESULT_TITLES["void"]
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                dailylog.record_result(channel_id, "doublechancetracker", key, "void", "Interrupted, never resumed")
                group_ids = parlaytracker.groups_for_leg(channel_id, "doublechancetracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "doublechancetracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided (double chance, interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
            else:
                # Timed out without ever settling and without an interrupted
                # signal to go on either - the standalone card is left alone
                # (no reliable result to guess at), but a parlay leg still
                # gets Voided so its summary card isn't stuck forever.
                botlog.event(f"⚠️ Auto-stopped tracking (double chance): game `{game_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
                await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Double-chance tracker crashed unexpectedly for game %s in channel %s", game_id, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (double chance): game `{game_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int, team: str, covered: tuple,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, sport_id, game_id, channel_id, owner_id, team, covered))
    _active[key] = task
    register_message(message.id, channel_id, game_id, owner_id)
    _persist(channel_id, game_id, message.id, sport_id, owner_id, team, covered)
    dailylog.record_pick(
        channel_id, "doublechancetracker", key, section, label or f"Double Chance: {pick_label(covered)}", message.id,
        origin_channel_id, sport=scores365.sport_label(sport_id, game.get("competitionDisplayName")),
        tournament=scores365.tournament_name(game),
        game_date=scores365.eastern_date_str(scores365.start_epoch(game)),
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for key, entry in list(state.load_double_chance().items()):
        try:
            channel_id, game_id, message_id, sport_id = (
                entry["channel_id"], entry["game_id"], entry["message_id"], entry["sport_id"]
            )
        except KeyError:
            log.warning("Dropping double-chance entry from an incompatible state schema: %r", entry)
            continue
        owner_id = entry.get("owner_id")
        message = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            try:
                channel = await client.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
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
            # Silent before this fix - see tracker.py's identical resume_all
            # fix for why this matters.
            botlog.event(f"⚠️ Dropped on resume (double chance): game `{game_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        # A single miss right here at startup used to forget the game
        # forever, permanently killing tracking on the unlucky restart that
        # lands during one transient 365scores hiccup, even though the live
        # loop itself tolerates MAX_CONSECUTIVE_MISSES misses in a row.
        # Retrying here closes that gap.
        game = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
            if game:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not game:
            botlog.event(f"⚠️ Dropped on resume (double chance): game `{game_id}` not found on 365scores after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(message, sport_id, game, channel_id, owner_id, entry["team"], tuple(entry["covered"]))
        log.info("Resumed double-chance tracking for game %s in channel %s", game_id, channel_id)

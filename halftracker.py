#!/usr/bin/env python3
"""
Manages background tasks for "1H" (1st Half) picks - team total (one side's
own Q1+Q2 points vs. a line) or combined total (both sides summed vs. a
line, same as tracker.py's game total but scoped to just the 1st half) -
both settle once the 2nd quarter is fully complete, not when the whole game
finishes, so they don't share tracker.py's wait-for-the-full-game design.
No moneyline/handicap flavor here (unlike f5tracker.py's F5) - not asked
for, and NFL/CFB 1st-half wording confirmed live only ever meant a total.

Backed by 365scores' per-game detail call (see scores365.quarters_breakdown),
which works across every league 365scores covers under football (NFL, CFL,
etc.) - confirmed live via a real finished CFL game's Q1/Q2 stage data.

Mirrors f5tracker.py otherwise: hibernation before kickoff, 🗑️-reaction
delete, restart-safe persistence, and a ✅/❌ result reaction once the pick
is graded.
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
import scores365
import state
import throttle

log = logging.getLogger("scorebox.halftracker")

MAX_CONSECUTIVE_MISSES = 3

MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"
THROUGH_QUARTER = scores365.THROUGH_1H_QUARTER

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖", "void": "<:cashback:1533844020839841832>"}

_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via 365scores" if message_id else "Scorebox • data via 365scores"


def track_key(
    channel_id: int, game_id, picked_team: Optional[str] = None, total_direction: Optional[str] = None,
    total_line: Optional[float] = None,
) -> str:
    """Different 1H bet types on the same game must never collide - same
    fix as tracker.py's track_key (see its docstring) - this used to be
    bare channel_id:game_id, so a 1H team total and a 1H combined total on
    the same game couldn't both be tracked at once."""
    if picked_team and total_direction and total_line is not None:
        market = f"tt:{picked_team}:{total_direction}:{total_line:g}"
    elif total_direction and total_line is not None:
        market = f"total:{total_direction}:{total_line:g}"
    else:
        market = "manual"
    return f"{channel_id}:{game_id}:{market}"


def is_tracked(
    channel_id: int, game_id, picked_team: Optional[str] = None, total_direction: Optional[str] = None,
    total_line: Optional[float] = None,
) -> bool:
    return track_key(channel_id, game_id, picked_team, total_direction, total_line) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_half().values()
        if track_key(
            entry["channel_id"], entry["game_id"], entry.get("picked_team"), entry.get("total_direction"),
            entry.get("total_line"),
        ) in active_keys
    ]


def register_message(
    message_id: int, channel_id: int, game_id, owner_id: int, picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, picked_team, total_direction, total_line, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, message_id: int, sport_id, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """section/label/origin_channel_id are persisted purely so resume_all
    can hand them back to dailylog.record_pick on restart - see
    tracker.py's _persist for why this matters."""
    data = state.load_half()
    data[track_key(channel_id, game_id, picked_team, total_direction, total_line)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "picked_team": picked_team,
        "total_direction": total_direction, "total_line": total_line,
        "section": section, "label": label, "origin_channel_id": origin_channel_id,
    }
    state.save_half(data)


def _forget(
    channel_id: int, game_id, picked_team: Optional[str] = None, total_direction: Optional[str] = None,
    total_line: Optional[float] = None,
):
    data = state.load_half()
    data.pop(track_key(channel_id, game_id, picked_team, total_direction, total_line), None)
    state.save_half(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_half()
    data.pop(key, None)
    state.save_half(data)


def stop_tracking(
    channel_id: int, game_id, picked_team: Optional[str] = None, total_direction: Optional[str] = None,
    total_line: Optional[float] = None,
) -> bool:
    key = track_key(channel_id, game_id, picked_team, total_direction, total_line)
    task = _active.pop(key, None)
    _forget(channel_id, game_id, picked_team, total_direction, total_line)
    dailylog.record_result(channel_id, "halftracker", key, "void", "Manually untracked")
    for message_id, (c_id, g_id, pt, td, tl, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and pt == picked_team and td == total_direction and tl == total_line:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _grade(game: dict, home_pts: int, away_pts: int, picked_team: Optional[str], total_direction: str, total_line: float) -> Optional[str]:
    if picked_team:
        return scores365.grade_1h_team_total(game, home_pts, away_pts, picked_team, total_direction, total_line)
    return scores365.grade_1h_combined_total(home_pts, away_pts, total_direction, total_line)


async def build_embed(
    game: dict, sport_id: Optional[int], picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """picked_team + total_direction/total_line grades that team's own
    Q1+Q2 total against a line; total_direction/total_line with no
    picked_team grades the *combined* (both sides summed) Q1+Q2 total
    against a line instead.

    force_result overrides the color/title as if this were already graded
    that way, regardless of the game's actual live status - used only by
    _track_loop's interrupted-and-never-resumed timeout branch."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))
    home_name = home_competitor.get("name", "?")
    away_name = away_competitor.get("name", "?")

    breakdown = await asyncio.to_thread(scores365.quarters_breakdown, game.get("id"), THROUGH_QUARTER)
    decided = breakdown is not None
    result = _grade(game, breakdown[0], breakdown[1], picked_team, total_direction, total_line) if decided else None

    # The live (through however many quarters have completed so far) or
    # final (through Q2) 1H total - shown alongside the line in the
    # description below (e.g. "1H Over 4.5 (3)"). Computed once here since
    # both the early-win check below and the description text need the
    # same number.
    current_total_value = None
    if total_direction in ("over", "under") and total_line is not None and status != "notstarted":
        if decided:
            if picked_team:
                current_total_value = breakdown[0] if scores365.names_match(home_name, picked_team) else breakdown[1]
            else:
                current_total_value = breakdown[0] + breakdown[1]
        elif picked_team:
            current_total_value = await asyncio.to_thread(
                scores365.partial_1h_team_total, game.get("id"), picked_team, home_name, away_name
            )
        else:
            current_total_value = await asyncio.to_thread(scores365.partial_1h_combined_total, game.get("id"))

    early_win = False
    if not result and status == "inprogress" and total_direction == "over" and total_line is not None:
        # Same early-win idea as f5tracker.py's Over tagging - a team's (or
        # the combined) Q1+Q2 point total only climbs as quarters complete,
        # so once the partial total already clears the line, the pick can't
        # become anything but a win even before both quarters are done.
        if current_total_value is not None and current_total_value > total_line:
            early_win = True

    if force_result:
        color_status = force_result
    elif result:
        color_status = result
    elif early_win:
        color_status = "won"
    elif status in ("notstarted", "finished"):
        color_status = status
    else:
        color_status = "inprogress"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]
    elif early_win:
        embed.title = _RESULT_TITLES["won"]

    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    total_suffix = f" ({current_total_value:g})" if current_total_value is not None else ""
    if picked_team:
        description_lines = [f"{picked_team} 1H {total_direction.title()} {total_line:g}{total_suffix}"]
    else:
        description_lines = [f"1H {total_direction.title()} {total_line:g}{total_suffix}"]
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "1H Final"
    elif status == "inprogress":
        period_text = scores365.status_line(game, sport_id)
    else:
        period_text = ""

    if decided:
        # Frozen at whatever the score was through the 2nd quarter - the
        # number that actually decided the pick - even though the game (and
        # its overall score) may keep going past this point.
        home_cols = [str(breakdown[0])]
        away_cols = [str(breakdown[1])]
    else:
        live_scores = scores365.main_scores(game)
        home_cols = [scores365.fmt_score(live_scores[0])] if live_scores else ["-"]
        away_cols = [scores365.fmt_score(live_scores[1])] if live_scores else ["-"]

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
    message: discord.Message, sport_id: int, game_id, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    key = track_key(channel_id, game_id, picked_team, total_direction, total_line)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final 1H tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit 1H tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final 1H tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, owner_id, picked_team, total_direction, total_line)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old 1H tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        # reason is shown in /summary - see tracker.py's identical helper
        # for why this matters.
        dailylog.record_result(channel_id, "halftracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
        if not group_ids:
            return
        if game:
            matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
        else:
            matchup = f"Game `{game_id}`"
        if picked_team:
            pick_desc = f"{picked_team} 1H {total_direction.title()} {total_line:g}"
        else:
            pick_desc = f"1H {total_direction.title()} {total_line:g}"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "halftracker", key, f"{matchup} - {pick_desc}", "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

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

                dailylog.touch(channel_id, "halftracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
                if group_ids:
                    pre_embed, _pre_file = await build_embed(
                        game, sport_id, picked_team, total_direction, total_line, message_id=message.id,
                    )
                    pre_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
                    pre_pick = pre_embed.description.splitlines()[0] if pre_embed.description else None
                    pre_label = f"{pre_matchup} - {pre_pick}" if pre_pick and pre_pick != pre_matchup else pre_matchup
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "halftracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("1H game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "1H game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (1H): game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Game not found on 365scores")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(
                game, sport_id, picked_team, total_direction, total_line, message_id=message.id,
            )
            leg_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            leg_pick = embed.description.splitlines()[0] if embed.description else None
            leg_label = f"{leg_matchup} - {leg_pick}" if leg_pick and leg_pick != leg_matchup else leg_matchup

            if hibernated:
                await _repost_final(embed, file)
                _persist(channel_id, game_id, message.id, sport_id, owner_id, picked_team, total_direction, total_line)
                continue

            breakdown = await asyncio.to_thread(scores365.quarters_breakdown, game_id, THROUGH_QUARTER)
            if breakdown is not None:
                result = _grade(game, breakdown[0], breakdown[1], picked_team, total_direction, total_line)

                await _repost_final(embed, file)

                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                if result:
                    dailylog.record_result(channel_id, "halftracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "halftracker", key, leg_label, result, group_ids,
                    )
                break

            if scores365.is_cancelled(game):
                # A cancelled game will never produce a 1H quarters
                # breakdown - see f5tracker.py's identical fix for why this
                # matters (the loop above would otherwise wait here forever,
                # showing a misleading "LIVE" detail on a match that's never
                # resuming).
                void_embed, void_file = await build_embed(
                    game, sport_id, picked_team, total_direction, total_line,
                    force_result="void", message_id=message.id,
                )
                void_embed.title = _RESULT_TITLES["void"]
                await _repost_final(void_embed, void_file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, void_embed.description or "")
                dailylog.record_result(channel_id, "halftracker", key, "void", "Cancelled")
                group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "halftracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided (1H, cancelled): game `{game_id}` in <#{channel_id}>")
                break

            kickoff = scores365.start_epoch(game)
            if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
            else:
                detail = f"LIVE, {scores365.status_line(game, sport_id)}"
            dailylog.touch(channel_id, "halftracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "halftracker", key, leg_label, detail, group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
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
                        "Failed to edit 1H tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (1H): game `{game_id}` message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit 1H tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (1H): game `{game_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the 1st half ever settling
            # (rain delay/stoppage, etc.) - tag it Voided/No Action instead
            # of silently leaving the card stuck with no result and no
            # cleanup, same treatment as f5tracker.py.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(
                    game, sport_id, picked_team, total_direction, total_line,
                    force_result="void", message_id=message.id,
                )
                embed.title = _RESULT_TITLES["void"]
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                dailylog.record_result(channel_id, "halftracker", key, "void", "Interrupted, never resumed")
                group_ids = parlaytracker.groups_for_leg(channel_id, "halftracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "halftracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided (1H, interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
            else:
                botlog.event(f"⚠️ Auto-stopped tracking (1H): game `{game_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
                await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("1H tracker crashed unexpectedly for game %s in channel %s", game_id, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (1H): game `{game_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, picked_team, total_direction, total_line)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id, picked_team, total_direction, total_line)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, owner_id, picked_team, total_direction, total_line)
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, owner_id, picked_team, total_direction, total_line)
    if not label:
        label = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
    _persist(
        channel_id, game_id, message.id, sport_id, owner_id, picked_team, total_direction, total_line,
        section, label, origin_channel_id,
    )
    dailylog.record_pick(
        channel_id, "halftracker", key, section, label, message.id, origin_channel_id, sport="NFL", tournament="NFL",
        game_date=scores365.eastern_date_str(scores365.start_epoch(game)),
    )


async def resume_all(client: discord.Client):
    """Called once from on_ready. Reads whatever was still active when the
    bot last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead."""
    for key, entry in list(state.load_half().items()):
        try:
            channel_id, game_id, message_id, sport_id = (
                entry["channel_id"], entry["game_id"], entry["message_id"], entry["sport_id"]
            )
        except KeyError:
            log.warning("Dropping 1H entry from an incompatible state schema: %r", entry)
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
            botlog.event(f"⚠️ Dropped on resume (1H): game `{game_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        game = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
            if game:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not game:
            botlog.event(f"⚠️ Dropped on resume (1H): game `{game_id}` not found on 365scores after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(
            message, sport_id, game, channel_id, owner_id, entry["picked_team"],
            entry.get("total_direction"), entry.get("total_line"),
            entry.get("section"), entry.get("label"), entry.get("origin_channel_id"),
        )
        log.info("Resumed 1H tracking for game %s in channel %s", game_id, channel_id)

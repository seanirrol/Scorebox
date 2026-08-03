#!/usr/bin/env python3
"""
Manages background tasks for tennis "extra markets" beyond the standard
game moneyline/total (see tracker.py) - all four settle on some part of a
match rather than requiring the whole thing to finish normally, and all
four are backed by 365scores' bulk game list's own per-set `stages`
breakdown (see scores365.tennis_first_set_result/tennis_match_games), no
separate per-game detail call needed:

- "set1_moneyline": who wins Set 1 (e.g. "Naomi Osaka to win 1st Set").
- "set1_total_games": combined games in Set 1 vs. a line.
- "match_total_games": combined games across the WHOLE match vs. a line -
  settles with the match itself finishing, unlike the other three, which
  all settle once Set 1 alone is complete.
- "win_a_set": whether a named player wins at least one set during the
  match (Yes/No) - can be graded a "Yes" win as soon as it happens, since a
  player can't un-win a set.

Mirrors f5tracker.py's multi-mode design (one tracker module, several
distinct grading shapes selected by which optional params are set) rather
than a file per market. track_key includes the market (unlike every other
single-mode tracker in this bot) so more than one of these four can be
tracked on the same match at once - e.g. a Set 1 moneyline pick and a
Match Total Games pick on the same match are two genuinely different bets,
not a duplicate of each other.

Otherwise mirrors tracker.py/f5tracker.py elsewhere: hibernation before
kickoff, 🗑️-reaction delete, restart-safe persistence, and a
Won/Lost/Push/Voided result reaction once the pick is graded (or abandoned
mid-interruption).
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

log = logging.getLogger("scorebox.settracker")
MAX_CONSECUTIVE_MISSES = 3
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, market, owner_id) - lets the
# reaction-based delete handler in bot.py look up who's allowed to delete a
# given message.
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


def track_key(channel_id: int, game_id, market: str) -> str:
    return f"{channel_id}:{game_id}:{market}"


def is_tracked(channel_id: int, game_id, market: str) -> bool:
    return track_key(channel_id, game_id, market) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_set1().values()
        if track_key(entry["channel_id"], entry["game_id"], entry["market"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, market: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, market, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, market: str, message_id: int, sport_id, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
):
    data = state.load_set1()
    data[track_key(channel_id, game_id, market)] = {
        "channel_id": channel_id, "game_id": game_id, "market": market, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "team": team, "direction": direction, "line": line,
    }
    state.save_set1(data)


def _forget(channel_id: int, game_id, market: str):
    data = state.load_set1()
    data.pop(track_key(channel_id, game_id, market), None)
    state.save_set1(data)


def stop_tracking(channel_id: int, game_id, market: str) -> bool:
    key = track_key(channel_id, game_id, market)
    task = _active.pop(key, None)
    _forget(channel_id, game_id, market)
    for message_id, (c_id, g_id, m, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and m == market:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def pick_label(market: str, team: Optional[str], direction: Optional[str], line: Optional[float]) -> str:
    if market == "set1_moneyline":
        return f"{team} 1st Set ML"
    if market == "set1_total_games":
        return f"1st Set {direction.title()} {line:g} Games"
    if market == "match_total_games":
        return f"{direction.title()} {line:g} Total Games"
    return f"{team} {'to Win a Set' if direction == 'yes' else 'Not to Win a Set'}"  # win_a_set


async def build_embed(
    game: dict, sport_id: Optional[int], market: str,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    force_result: Optional[str] = None,
) -> tuple[discord.Embed, discord.File]:
    """Exactly one of the four markets applies per pick (see this module's
    docstring) - team is the named player for set1_moneyline/win_a_set,
    direction+line are the Over/Under (or Yes/No) line for the other two.

    force_result overrides the color/title as if this were already graded
    that way, regardless of the game's actual live status - used only by
    _track_loop's interrupted-and-never-resumed timeout branch."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))

    decided = False
    result = None
    frozen_cols: Optional[tuple[str, str]] = None
    final_period_text = "Final"

    if market == "set1_moneyline":
        breakdown = scores365.tennis_first_set_result(game)
        decided = breakdown is not None
        if decided:
            result = scores365.grade_tennis_set(game, breakdown[0], breakdown[1], team)
            frozen_cols = (str(breakdown[0]), str(breakdown[1]))
        final_period_text = "1st Set Final"

    elif market == "set1_total_games":
        breakdown = scores365.tennis_first_set_result(game)
        decided = breakdown is not None
        if decided:
            result = scores365.grade_over_under(breakdown[0] + breakdown[1], direction, line)
            frozen_cols = (str(breakdown[0]), str(breakdown[1]))
        final_period_text = "1st Set Final"

    elif market == "match_total_games":
        decided = scores365.is_finished(game)
        home_games, away_games = scores365.tennis_match_games(game)
        if decided:
            result = scores365.grade_over_under(home_games + away_games, direction, line)
        frozen_cols = (str(home_games), str(away_games))  # live-running, shown whether decided or not

    else:  # win_a_set
        decided_result = scores365.grade_win_a_set(game, team, direction)
        decided = decided_result is not None
        result = decided_result

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

    description_lines = [pick_label(market, team, direction, line)]
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = final_period_text
    elif status == "inprogress":
        period_text = scores365.status_line(game, sport_id)
    else:
        period_text = ""

    if frozen_cols is not None:
        home_cols, away_cols = [frozen_cols[0]], [frozen_cols[1]]
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
        home_name, away_name, home_logo_url, away_logo_url, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text="Scorebox • data via 365scores")
    embed.timestamp = discord.utils.utcnow()
    return embed, file


def grade_now(game: dict, market: str, team: Optional[str], direction: Optional[str], line: Optional[float]) -> tuple[bool, Optional[str]]:
    """Returns (decided, result) for the current game state - shared by
    _track_loop's polling and its final grading step so the two can't drift
    apart on what counts as "decided" for a given market."""
    if market == "set1_moneyline":
        breakdown = scores365.tennis_first_set_result(game)
        if breakdown is None:
            return False, None
        return True, scores365.grade_tennis_set(game, breakdown[0], breakdown[1], team)
    if market == "set1_total_games":
        breakdown = scores365.tennis_first_set_result(game)
        if breakdown is None:
            return False, None
        return True, scores365.grade_over_under(breakdown[0] + breakdown[1], direction, line)
    if market == "match_total_games":
        if not scores365.is_finished(game):
            return False, None
        home_games, away_games = scores365.tennis_match_games(game)
        return True, scores365.grade_over_under(home_games + away_games, direction, line)
    # win_a_set
    result = scores365.grade_win_a_set(game, team, direction)
    return result is not None, result


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, market: str, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
):
    key = track_key(channel_id, game_id, market)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

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
            log.warning("Failed to repost final tennis-market tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit tennis-market tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final tennis-market tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, market, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old tennis-market tracking message after final repost: %s", e)
        return carry_emojis

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
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
                log.info("Tennis-market game %s (%s) not starting soon; hibernating %.0fs", game_id, market, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "Tennis-market game %s (%s) not found in 365scores' current list (miss %d/%d)",
                    game_id, market, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({market}): game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, market, team, direction, line)
            leg_label = embed.description.splitlines()[0] if embed.description else (
                f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            )

            if hibernated:
                # Report in as NOT STARTED even while still hibernating -
                # otherwise a leg with a kickoff hours away would never
                # appear on its parlay's summary card until it woke up
                # within 90s of starting.
                try:
                    fresh = await message.channel.fetch_message(message.id)
                    marker_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
                except discord.HTTPException:
                    marker_emojis = []
                if marker_emojis:
                    kickoff = scores365.start_epoch(game)
                    detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "settracker", key, leg_label, detail, marker_emojis,
                    )
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/f5tracker.py.
                await _repost_final(embed, file)
                _persist(channel_id, game_id, market, message.id, sport_id, owner_id, team, direction, line)
                continue

            decided, result = grade_now(game, market, team, direction, line)
            if decided:
                carry_emojis = await _repost_final(embed, file)

                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                if result:
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "settracker", key, leg_label, result, carry_emojis,
                    )
                break

            try:
                fresh = await message.channel.fetch_message(message.id)
                marker_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
            except discord.HTTPException:
                marker_emojis = []
            if marker_emojis:
                kickoff = scores365.start_epoch(game)
                if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                    detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
                else:
                    detail = f"LIVE, {scores365.status_line(game, sport_id)}"
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "settracker", key, leg_label, detail, marker_emojis,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit tennis-market tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({market}): game `{game_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the pick ever settling. If the
            # match was left mid-interruption (rain delay, darkness, etc.)
            # rather than genuinely still in progress, that's stalled, not
            # just slow - tag it Voided/No Action instead of silently
            # leaving the card stuck with no result and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(game, sport_id, market, team, direction, line, force_result="void")
                embed.title = _RESULT_TITLES["void"]
                carry_emojis = await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "settracker", key, leg_label, "void", carry_emojis,
                )
                botlog.event(f"➖ Voided ({market}, interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
    except asyncio.CancelledError:
        raise
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, market)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, market: str, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id, market)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, market, owner_id, team, direction, line)
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, market, owner_id)
    _persist(channel_id, game_id, market, message.id, sport_id, owner_id, team, direction, line)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_set1().values()):
        try:
            channel_id, game_id, market, message_id, sport_id = (
                entry["channel_id"], entry["game_id"], entry["market"], entry["message_id"], entry["sport_id"]
            )
        except KeyError:
            # Belongs to the pre-multi-market schema (no "market" key, just
            # a bare set1-moneyline-only "team") - can't be resumed, just
            # drop it rather than crashing the rest of startup.
            log.warning("Dropping 1st-set entry from an old state schema: %r", entry)
            continue
        owner_id = entry.get("owner_id")
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, game_id, market)
            continue

        game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
        if not game:
            _forget(channel_id, game_id, market)
            continue

        start_tracking(
            message, sport_id, game, channel_id, market, owner_id,
            entry.get("team"), entry.get("direction"), entry.get("line"),
        )
        log.info("Resumed tennis-market (%s) tracking for game %s in channel %s", market, game_id, channel_id)

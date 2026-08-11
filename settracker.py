#!/usr/bin/env python3
"""
Manages background tasks for tennis "extra markets" beyond the standard
game moneyline/total (see tracker.py) - all of these settle on some part of
a match rather than requiring the whole thing to finish normally, and all
are backed by 365scores' bulk game list's own per-set `stages` breakdown
(see scores365.tennis_first_set_result/tennis_match_games), no separate
per-game detail call needed:

- "set1_moneyline": who wins Set 1 (e.g. "Naomi Osaka to win 1st Set").
- "set1_total_games": combined games in Set 1 vs. a line.
- "match_total_games": combined games across the WHOLE match vs. a line -
  settles with the match itself finishing, unlike set1_moneyline/
  set1_total_games, which both settle once Set 1 alone is complete.
- "player_total_games": one named player's own total games won across the
  whole match vs. a line (distinct from match_total_games, which sums both
  sides) - also settles with the match finishing.
- "games_handicap": a games-margin spread (e.g. "Brandon Nakashima -2.5
  Games") - the picked player's own total games, adjusted by the
  (already-signed) line, compared against the opponent's total games. Also
  settles with the match finishing.
- "sets_handicap": a sets-margin spread (e.g. "Wang Xiyu +1.5 Sets") - same
  shape as games_handicap but against sets won (main_scores) instead of
  games won.
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
import dailylog
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

# message_id -> (channel_id, game_id, market, team, owner_id) - lets the
# reaction-based delete handler in bot.py look up who's allowed to delete a
# given message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖", "void": "<:cashback:1533844020839841832>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via 365scores" if message_id else "Scorebox • data via 365scores"


# Markets where team actually identifies WHICH player the pick is graded
# against - two different players' picks on the same match+market used to
# collide onto the identical track_key (team wasn't part of the key at
# all), which made start_tracking's own "already active" dedup check (see
# start_tracking) silently drop the second one: its card still posted with
# a real live snapshot (built independently before the collision was ever
# checked), footer patched, reaction added - everything looked normal - but
# it never got registered or given a _track_loop, so it sat frozen forever
# with no error anywhere. Confirmed live: a player_total_games pick absent
# from its own channel's /tracked list despite its card still showing what
# looked like a live score.
#
# set1_total_games/match_total_games are deliberately excluded - for those,
# _auto_tennis_market's "team" is just whichever matchup side was used to
# look the match up, not part of the bet's identity, so including it in the
# key would wrongly let the same match-total pick get double-tracked if two
# people referenced it via different anchor names.
_PER_PLAYER_MARKETS = {"set1_moneyline", "player_total_games", "win_a_set", "games_handicap", "sets_handicap"}


def track_key(channel_id: int, game_id, market: str, team: Optional[str] = None) -> str:
    disambiguator = team if market in _PER_PLAYER_MARKETS else None
    return f"{channel_id}:{game_id}:{market}:{disambiguator}"


def is_tracked(channel_id: int, game_id, market: str, team: Optional[str] = None) -> bool:
    return track_key(channel_id, game_id, market, team) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_set1().values()
        if track_key(entry["channel_id"], entry["game_id"], entry["market"], entry.get("team")) in active_keys
    ]


def register_message(message_id: int, channel_id: int, game_id, market: str, team: Optional[str], owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, market, team, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, market: str, message_id: int, sport_id, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
):
    data = state.load_set1()
    data[track_key(channel_id, game_id, market, team)] = {
        "channel_id": channel_id, "game_id": game_id, "market": market, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "team": team, "direction": direction, "line": line,
    }
    state.save_set1(data)


def _forget(channel_id: int, game_id, market: str, team: Optional[str] = None):
    data = state.load_set1()
    data.pop(track_key(channel_id, game_id, market, team), None)
    state.save_set1(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters. Confirmed
    live: an old set1_total_games entry had no trailing disambiguator
    segment at all (missing entirely, not just a different value), so
    _forget()'s reconstruction could never match it."""
    data = state.load_set1()
    data.pop(key, None)
    state.save_set1(data)


def stop_tracking(channel_id: int, game_id, market: str, team: Optional[str] = None) -> bool:
    key = track_key(channel_id, game_id, market, team)
    task = _active.pop(key, None)
    _forget(channel_id, game_id, market, team)
    dailylog.record_result(channel_id, "settracker", key, "void")
    for message_id, (c_id, g_id, m, t, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and m == market and t == team:
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
    if market == "player_total_games":
        return f"{team} {direction.title()} {line:g} Total Games"
    if market == "games_handicap":
        return f"{team} {line:+g} Games"
    if market == "sets_handicap":
        return f"{team} {line:+g} Sets"
    return f"{team} {'to Win a Set' if direction == 'yes' else 'Not to Win a Set'}"  # win_a_set


async def build_embed(
    game: dict, sport_id: Optional[int], market: str,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
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
            frozen_cols = (scores365.fmt_score(breakdown[0]), scores365.fmt_score(breakdown[1]))
        final_period_text = "1st Set Final"

    elif market == "set1_total_games":
        breakdown = scores365.tennis_first_set_result(game)
        decided = breakdown is not None
        if decided:
            result = scores365.grade_over_under(breakdown[0] + breakdown[1], direction, line)
            frozen_cols = (scores365.fmt_score(breakdown[0]), scores365.fmt_score(breakdown[1]))
        final_period_text = "1st Set Final"

    elif market == "match_total_games":
        decided = scores365.is_finished(game)
        home_games, away_games = scores365.tennis_match_games(game)
        if decided:
            result = scores365.grade_over_under(home_games + away_games, direction, line)
        frozen_cols = (scores365.fmt_score(home_games), scores365.fmt_score(away_games))  # live-running, shown whether decided or not

    elif market == "player_total_games":
        # Unlike match_total_games (both sides summed), this is one named
        # player's own games won across the whole match - a distinct,
        # commonly-bet prop line, not to be confused with the combined total.
        decided = scores365.is_finished(game)
        home_games, away_games = scores365.tennis_match_games(game)
        player_games = home_games if scores365.names_match(home_competitor.get("name", ""), team) else away_games
        if decided:
            result = scores365.grade_over_under(player_games, direction, line)
        frozen_cols = (scores365.fmt_score(home_games), scores365.fmt_score(away_games))  # live-running, shown whether decided or not

    elif market == "games_handicap":
        decided = scores365.is_finished(game)
        home_games, away_games = scores365.tennis_match_games(game)
        if decided:
            result = scores365.grade_games_handicap(game, team, line)
        frozen_cols = (scores365.fmt_score(home_games), scores365.fmt_score(away_games))  # live-running, shown whether decided or not

    elif market == "sets_handicap":
        decided = scores365.is_finished(game)
        if decided:
            result = scores365.grade_sets_handicap(game, team, line)
        # frozen_cols left None - falls through to the live main_scores
        # (sets-won) display below, same score that's already shown for a
        # plain moneyline pick, no separate freeze needed for this market.

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
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


def grade_now(game: dict, market: str, team: Optional[str], direction: Optional[str], line: Optional[float]) -> tuple[bool, Optional[str]]:
    """Returns (decided, result) for the current game state - shared by
    _track_loop's polling and its final grading step so the two can't drift
    apart on what counts as "decided" for a given market.

    player_total_games previously had no branch here at all and silently
    fell through to the win_a_set fallback below - confirmed live this made
    _track_loop treat the pick as "decided" the moment the picked player won
    their first set (win_a_set's own, much earlier decision point), grading
    it via direction="yes"/"no" logic against an actual "under"/"over" pick,
    which flips the result backwards on top of being premature. The embed
    itself (built separately by build_embed, which already had the correct
    is_finished gate) never visibly changed, so the card was left frozen
    mid-match while a wrong result reaction still got added underneath it."""
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
    if market == "player_total_games":
        if not scores365.is_finished(game):
            return False, None
        home_competitor = game.get("homeCompetitor") or {}
        home_games, away_games = scores365.tennis_match_games(game)
        player_games = home_games if scores365.names_match(home_competitor.get("name", ""), team) else away_games
        return True, scores365.grade_over_under(player_games, direction, line)
    if market == "games_handicap":
        if not scores365.is_finished(game):
            return False, None
        return True, scores365.grade_games_handicap(game, team, line)
    if market == "sets_handicap":
        if not scores365.is_finished(game):
            return False, None
        return True, scores365.grade_sets_handicap(game, team, line)
    # win_a_set
    result = scores365.grade_win_a_set(game, team, direction)
    return result is not None, result


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, market: str, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
):
    key = track_key(channel_id, game_id, market, team)
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
        register_message(message.id, channel_id, game_id, market, team, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old tennis-market tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up():
        """Called on every path where this tracker gives up without ever
        reaching a real result (game never found again, Discord edits
        failing repeatedly, or a MAX_TRACK_HOURS timeout with no interrupted
        signal to go on) - reports the leg as Voided to its parlay group
        instead of leaving the summary card frozen on whatever pending
        detail it last reported, forever, once this task quietly stops
        polling."""
        dailylog.record_result(channel_id, "settracker", key, "void")
        group_ids = parlaytracker.groups_for_leg(channel_id, "settracker", key)
        if not group_ids:
            return
        if game:
            matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
        else:
            matchup = f"Game `{game_id}`"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "settracker", key,
            f"{matchup} - {pick_label(market, team, direction, line)}", "void", group_ids,
        )

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

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation ends
                # (right before kickoff) is too late: a leg with a kickoff
                # hours away would still never appear on its parlay's
                # summary card until it was basically already live anyway.
                dailylog.touch(channel_id, "settracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "settracker", key)
                if group_ids:
                    pre_embed, _pre_file = await build_embed(game, sport_id, market, team, direction, line, message_id=message.id)
                    pre_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
                    pre_pick = pre_embed.description.splitlines()[0] if pre_embed.description else None
                    pre_label = f"{pre_matchup} - {pre_pick}" if pre_pick and pre_pick != pre_matchup else pre_matchup
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "settracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

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
                    await _void_leg_and_give_up()
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, market, team, direction, line, message_id=message.id)
            leg_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            leg_pick = embed.description.splitlines()[0] if embed.description else None
            leg_label = f"{leg_matchup} - {leg_pick}" if leg_pick and leg_pick != leg_matchup else leg_matchup

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/f5tracker.py.
                await _repost_final(embed, file)
                _persist(channel_id, game_id, market, message.id, sport_id, owner_id, team, direction, line)
                continue

            decided, result = grade_now(game, market, team, direction, line)
            if decided:
                await _repost_final(embed, file)

                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                if result:
                    dailylog.record_result(channel_id, "settracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "settracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "settracker", key, leg_label, result, group_ids,
                    )
                break

            kickoff = scores365.start_epoch(game)
            if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
            else:
                detail = f"LIVE, {scores365.status_line(game, sport_id)}"
            dailylog.touch(channel_id, "settracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "settracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "settracker", key, leg_label, detail, group_ids,
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
                    await _void_leg_and_give_up()
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the pick ever settling. If the
            # match was left mid-interruption (rain delay, darkness, etc.)
            # rather than genuinely still in progress, that's stalled, not
            # just slow - tag it Voided/No Action instead of silently
            # leaving the card stuck with no result and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(
                    game, sport_id, market, team, direction, line, force_result="void", message_id=message.id,
                )
                embed.title = _RESULT_TITLES["void"]
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                dailylog.record_result(channel_id, "settracker", key, "void")
                group_ids = parlaytracker.groups_for_leg(channel_id, "settracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "settracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided ({market}, interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
            else:
                # Timed out without ever settling and without an interrupted
                # signal to go on either - the standalone card is left alone
                # (no reliable result to guess at), but a parlay leg still
                # gets Voided so its summary card isn't stuck forever.
                botlog.event(f"⚠️ Auto-stopped tracking ({market}): game `{game_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
                await _void_leg_and_give_up()
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Set tracker crashed unexpectedly for game %s (%s) in channel %s", game_id, market, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking ({market}): game `{game_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up()
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, market, team)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, market: str, owner_id: int,
    team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id, market, team)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, market, owner_id, team, direction, line)
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, market, team, owner_id)
    _persist(channel_id, game_id, market, message.id, sport_id, owner_id, team, direction, line)
    dailylog.record_pick(
        channel_id, "settracker", key, section, label or pick_label(market, team, direction, line), message.id,
        origin_channel_id,
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for key, entry in list(state.load_set1().items()):
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
        team = entry.get("team")
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Silent before this fix - see tracker.py's identical resume_all
            # fix for why this matters.
            botlog.event(f"⚠️ Dropped on resume ({market}): game `{game_id}` — message/channel no longer reachable, in <#{channel_id}>")
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
            botlog.event(f"⚠️ Dropped on resume ({market}): game `{game_id}` not found on 365scores after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(
            message, sport_id, game, channel_id, market, owner_id,
            team, entry.get("direction"), entry.get("line"),
        )
        log.info("Resumed tennis-market (%s) tracking for game %s in channel %s", market, game_id, channel_id)

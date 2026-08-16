#!/usr/bin/env python3
"""
Manages background tasks for Dota 2 / CS2 esports picks - eight markets, all
settling on the overall best-of-N series or one specific map within it (see
esports.py's own docstring for where the underlying data comes from):

- "match_winner": who wins the series outright.
- "map_handicap": a team's maps-won count adjusted by a line, vs. the other
  side's raw maps-won count (e.g. "PlayTime (-1.5) Map Handicap").
- "total_maps": combined maps played in the whole series vs. a line.
- "map_winner": winner of one specific individual map (e.g. "Map 2 Winner").
- "win_at_least_one_map": whether a named team wins at least one map during
  the series (not swept) - can resolve the moment it happens, since a map
  win can't be undone.
- "correct_score": the exact series score (e.g. "Team X to win 2-0").
- "total_kills": combined kills across every map played in the series vs. a
  line - Dota 2 only, CS2 has no kill data anywhere (see
  esports.live_kill_count's own docstring).
- "team_total_kills": one named team's own kill total across every map
  played - same Dota 2-only restriction.

Mirrors settracker.py's multi-mode design (one tracker module, several
distinct grading shapes selected by `market`) rather than a file per market.
Differs from every other tracker in this bot in one structural way: there's
no numeric game_id to refetch the same match by across polls (neither
hawk.live nor GosuGamers expose one) - every poll re-resolves the match
fresh by team name via esports.get_series, exactly mirroring how the
Torn-BetSync project's own hawklive.js/gosugamers.js work. track_key is
built from (channel_id, sport, team_a, team_b, market) instead of a game_id.

Otherwise mirrors every other tracker: hibernation before kickoff,
🗑️-reaction delete, restart-safe persistence, and a Won/Lost/Push result
reaction once the pick is graded. Unlike the 365scores/ESPN-backed trackers,
neither provider exposes a reliable "interrupted/postponed, never resumed"
signal, so there's no Voided-on-timeout branch here - same limitation as
ufctracker.py.
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
import esports
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.esportstracker")
MAX_CONSECUTIVE_MISSES = 3
MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_SPORT_LABELS = {"dota2": "Dota 2", "cs2": "CS2"}

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, sport, team_a, team_b, market, owner_id) - lets
# the reaction-based delete handler in bot.py look up who's allowed to
# delete a given message.
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
    return f"Scorebox ({message_id}) • data via hawk.live/GosuGamers" if message_id else "Scorebox • data via hawk.live/GosuGamers"


def track_key(channel_id: int, sport: str, team_a: str, team_b: str, market: str) -> str:
    return f"{channel_id}:{sport}:{team_a}|{team_b}:{market}"


def is_tracked(channel_id: int, sport: str, team_a: str, team_b: str, market: str) -> bool:
    return track_key(channel_id, sport, team_a, team_b, market) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_esports().values()
        if track_key(entry["channel_id"], entry["sport"], entry["team_a"], entry["team_b"], entry["market"]) in active_keys
    ]


def register_message(message_id: int, channel_id: int, sport: str, team_a: str, team_b: str, market: str, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, sport, team_a, team_b, market, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, sport: str, team_a: str, team_b: str, market: str, message_id: int, owner_id: int,
    picked_team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
):
    data = state.load_esports()
    data[track_key(channel_id, sport, team_a, team_b, market)] = {
        "channel_id": channel_id, "sport": sport, "team_a": team_a, "team_b": team_b, "market": market,
        "message_id": message_id, "owner_id": owner_id, "picked_team": picked_team,
        "direction": direction, "line": line,
        "map_number": map_number, "picked_maps": picked_maps, "other_maps": other_maps,
    }
    state.save_esports(data)


def _forget(channel_id: int, sport: str, team_a: str, team_b: str, market: str):
    data = state.load_esports()
    data.pop(track_key(channel_id, sport, team_a, team_b, market), None)
    state.save_esports(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_esports()
    data.pop(key, None)
    state.save_esports(data)


def stop_tracking(channel_id: int, sport: str, team_a: str, team_b: str, market: str) -> bool:
    key = track_key(channel_id, sport, team_a, team_b, market)
    task = _active.pop(key, None)
    _forget(channel_id, sport, team_a, team_b, market)
    dailylog.record_result(channel_id, "esportstracker", key, "void", "Manually untracked")
    for message_id, (c_id, s, a, b, m, _owner) in list(_message_owners.items()):
        if c_id == channel_id and s == sport and a == team_a and b == team_b and m == market:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def pick_label(
    market: str, picked_team: Optional[str], direction: Optional[str], line: Optional[float],
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
) -> str:
    if market == "match_winner":
        return f"{picked_team} ML"
    if market == "map_handicap":
        return f"{picked_team} {line:+g} Map Handicap"
    if market == "total_maps":
        return f"{direction.title()} {line:g} Total Maps"
    if market == "map_winner":
        return f"{picked_team} Map {map_number} Winner"
    if market == "match_and_map_winner":
        return f"{picked_team} ML + Map {map_number} Winner"
    if market == "win_at_least_one_map":
        return f"{picked_team} to {'Not ' if direction == 'no' else ''}Win at Least One Map"
    if market == "correct_score":
        return f"{picked_team} to Win {picked_maps}-{other_maps}"
    if market == "total_kills":
        return f"{direction.title()} {line:g} Total Kills"
    return f"{picked_team} {direction.title()} {line:g} Total Kills"  # team_total_kills


def grade_now(
    series_data: dict, market: str, picked_team: Optional[str], direction: Optional[str], line: Optional[float],
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Returns (decided, result) for the current series state - shared by
    build_embed and _track_loop's own final-grading step so the two can't
    drift apart on what counts as "decided" for a given market."""
    if market == "match_winner":
        result = esports.grade_match_winner(series_data, picked_team)
    elif market == "map_handicap":
        result = esports.grade_map_handicap(series_data, picked_team, line)
    elif market == "total_maps":
        result = esports.grade_total_maps(series_data, direction, line)
    elif market == "map_winner":
        result = esports.grade_map_winner(series_data, map_number, picked_team)
    elif market == "match_and_map_winner":
        result = esports.grade_match_and_map_winner(series_data, map_number, picked_team)
    elif market == "win_at_least_one_map":
        result = esports.grade_win_at_least_one_map(series_data, picked_team, direction or "yes")
    elif market == "correct_score":
        result = esports.grade_correct_score(series_data, picked_team, picked_maps, other_maps)
    elif market == "total_kills":
        result = esports.grade_total_kills(series_data, direction, line)
    else:  # team_total_kills
        result = esports.grade_team_total_kills(series_data, picked_team, direction, line)
    return result is not None, result


async def build_embed(
    series_data: dict, market: str,
    picked_team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """force_result overrides the color/title as if this were already
    graded that way, regardless of the series' actual live status - not
    currently used here (no interrupted/postponed signal to react to), kept
    for signature parity with every other tracker's build_embed."""
    status = series_data["status"]
    decided, result = grade_now(series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps)

    # Current running kill count for total_kills/team_total_kills - both the
    # progress display below and the early-win check just after reuse this
    # same value. None for every other market, CS2, or a Dota 2 series only
    # ever resolved via the GosuGamers fallback (no per-map kill data).
    current_kills: Optional[int] = None
    if market in ("total_kills", "team_total_kills"):
        kills = esports.total_kills(series_data)
        if kills is not None:
            if market == "total_kills":
                current_kills = kills[0] + kills[1]
            elif esports.names_match(series_data["home_team"], picked_team):
                current_kills = kills[0]
            elif esports.names_match(series_data["away_team"], picked_team):
                current_kills = kills[1]

    # Total maps played so far - series_data's own home/away maps-won score
    # is already live regardless of decided status, and (like the kill
    # count above) only ever grows as more maps get played.
    current_maps: Optional[int] = None
    if market == "total_maps":
        current_maps = series_data["home_score"] + series_data["away_score"]

    # Picked team's own map count so far, for win_at_least_one_map - same
    # early-lock reasoning as the kill/map totals below, since that market's
    # own grade_win_at_least_one_map now deliberately waits for the series
    # to actually finish (see its docstring) rather than bumping the card
    # to Final the moment it's mathematically locked in.
    current_picked_maps: Optional[int] = None
    if market == "win_at_least_one_map":
        if esports.names_match(series_data["home_team"], picked_team):
            current_picked_maps = series_data["home_score"]
        elif esports.names_match(series_data["away_team"], picked_team):
            current_picked_maps = series_data["away_score"]

    # Both the kill total and the maps total only ever grow as more maps
    # get played, so once an Over line is already cleared it can't
    # un-clear - same reasoning as tracker.py's own early-win Over tagging
    # elsewhere in this bot, safe to apply here despite
    # grade_total_kills/grade_team_total_kills/grade_total_maps themselves
    # deliberately waiting for the series to end (see their own
    # docstrings) - that wait is about not being able to call an early
    # UNDER, not about Over. Purely cosmetic: the actual dailylog
    # settlement still only happens once `decided` for real, same as
    # every other early-win tag in this bot.
    current_value = current_kills if current_kills is not None else current_maps
    early_result: Optional[str] = None
    if not result and current_value is not None and direction == "over" and line is not None and current_value > line:
        early_result = "won"
    elif not result and current_picked_maps is not None and current_picked_maps >= 1:
        early_result = "lost" if direction == "no" else "won"

    if force_result:
        color_status = force_result
    elif result:
        color_status = result
    elif early_result:
        color_status = early_result
    elif status in ("notstarted", "finished"):
        color_status = status
    else:
        color_status = "inprogress"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]
    elif early_result:
        embed.title = _RESULT_TITLES[early_result]

    author_bits = [b for b in (_SPORT_LABELS.get(series_data["sport"]), series_data.get("tournament")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))

    label = pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps)
    if current_kills is not None:
        # e.g. "Over 108.5 Total Kills (100)" - progress toward the line.
        label = f"{label} ({current_kills})"
    description_lines = [label]
    if status == "notstarted" and series_data.get("start_epoch"):
        description_lines.append(f"<t:{int(series_data['start_epoch'])}:f>")
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "Final"
    elif status == "inprogress":
        period_text = f"Game {series_data['current_game_number']}"
    else:
        period_text = ""

    home_cols = [str(series_data["home_score"])]
    away_cols = [str(series_data["away_score"])]

    if status == "inprogress":
        # Live in-map score for whichever map is currently being played -
        # kills for Dota 2, rounds for CS2 - purely a supplementary display
        # value, same sub-score-row treatment tennis/volleyball already get
        # elsewhere in this bot. Not used for grading anything.
        kill_count = await asyncio.to_thread(esports.live_kill_count, series_data)
        if kill_count:
            home_cols.append(str(kill_count[0]))
            away_cols.append(str(kill_count[1]))

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        series_data.get("home_team") or "?", series_data.get("away_team") or "?",
        series_data.get("home_logo_url"), series_data.get("away_logo_url"),
        home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, sport: str, team_a: str, team_b: str, channel_id: int, market: str, owner_id: int,
    picked_team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
):
    key = track_key(channel_id, sport, team_a, team_b, market)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0
    # Filled in once the match is first resolved - passed back into later
    # get_series calls purely to help GosuGamers disambiguate a back-to-back
    # series between the same two teams, not required for hawk.live.
    expected_epoch: Optional[float] = None

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-kickoff or
        graded) - falls back to editing in place if the repost send itself
        fails. Carries forward any reaction someone added beyond the bot's
        own service ones (e.g. tagging a card as part of a parlay) -
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
            log.warning("Failed to repost final esports tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit esports tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final esports tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, sport, team_a, team_b, market, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old esports tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        """Called on every path where this tracker gives up without ever
        reaching a real result (match never found again, Discord edits
        failing repeatedly, MAX_TRACK_HOURS exhausted) - reports the leg as
        Voided to its parlay group instead of leaving the summary card
        frozen on whatever pending detail ("NOT STARTED"/"LIVE, Game N") it
        last reported, forever, once this task quietly stops polling.
        reason is shown in /summary - see tracker.py's identical helper
        for why this matters."""
        dailylog.record_result(channel_id, "esportstracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "esportstracker", key)
        if group_ids:
            leg_label = f"{team_a} vs {team_b} - {pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps)}"
            await parlaytracker.handle_leg_result(
                message.channel, channel_id, message, "esportstracker", key, leg_label, "void", group_ids,
            )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            series_data = await asyncio.to_thread(esports.get_series, sport, team_a, team_b, expected_epoch)

            # A notstarted series' maps-won score can't change before it
            # starts, so hibernate instead of polling every cycle - same
            # pattern as every other tracker in this bot.
            hibernated = False
            while series_data and series_data["status"] == "notstarted":
                kickoff = series_data.get("start_epoch")
                if not kickoff:
                    break
                seconds_until_kickoff = kickoff - time.time()
                if seconds_until_kickoff <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True
                expected_epoch = kickoff

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation ends
                # (right before kickoff) is too late: a leg with a kickoff
                # hours away would still never appear on its parlay's
                # summary card until it was basically already live anyway.
                dailylog.touch(channel_id, "esportstracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "esportstracker", key)
                if group_ids:
                    pre_matchup = f"{series_data.get('home_team') or '?'} vs {series_data.get('away_team') or '?'}"
                    pre_pick = pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps)
                    pre_label = f"{pre_matchup} - {pre_pick}"
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "esportstracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Esports match %s v %s (%s) not starting soon; hibernating %.0fs", team_a, team_b, market, hibernate_for)
                await asyncio.sleep(hibernate_for)
                series_data = await asyncio.to_thread(esports.get_series, sport, team_a, team_b, expected_epoch)

            if not series_data:
                consecutive_misses += 1
                log.warning(
                    "Esports match %s v %s (%s) not found on hawk.live/GosuGamers (miss %d/%d)",
                    team_a, team_b, market, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(
                        f"⚠️ Auto-stopped tracking (esports {market}): "
                        f"{team_a} v {team_b} not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>"
                    )
                    await _void_leg_and_give_up("Match not found on hawk.live/GosuGamers")
                    break
                continue
            consecutive_misses = 0
            expected_epoch = series_data.get("start_epoch") or expected_epoch

            embed, file = await build_embed(
                series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps,
                message_id=message.id,
            )

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that
                # may be buried under whatever chat happened during the
                # (possibly long) hibernation.
                await _repost_final(embed, file)
                _persist(
                    channel_id, sport, team_a, team_b, market, message.id, owner_id,
                    picked_team, direction, line, map_number, picked_maps, other_maps,
                )
                continue

            decided, result = grade_now(series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps)
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
                    leg_matchup = f"{series_data.get('home_team') or '?'} vs {series_data.get('away_team') or '?'}"
                    leg_pick = pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps)
                    leg_label = f"{leg_matchup} - {leg_pick}"
                    dailylog.record_result(channel_id, "esportstracker", key, result, "Unplayed map" if result == "void" else None)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "esportstracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "esportstracker", key, leg_label, result, group_ids,
                    )
                break

            if series_data["status"] == "notstarted" and series_data.get("start_epoch"):
                detail = f"NOT STARTED - <t:{int(series_data['start_epoch'])}:f>"
            elif series_data["status"] == "notstarted":
                detail = "NOT STARTED"
            else:
                detail = f"LIVE, Game {series_data['current_game_number']}"
            dailylog.touch(channel_id, "esportstracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "esportstracker", key)
            if group_ids:
                leg_matchup = f"{series_data.get('home_team') or '?'} vs {series_data.get('away_team') or '?'}"
                leg_pick = pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps)
                leg_label = f"{leg_matchup} - {leg_pick}"
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "esportstracker", key, leg_label, detail, group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
            except discord.HTTPException as e:
                if e.status == 429:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit esports tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(
                            f"⚠️ Auto-stopped tracking (esports {market}): "
                            f"{team_a} v {team_b} message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>"
                        )
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit esports tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(
                        f"⚠️ Auto-stopped tracking (esports {market}): "
                        f"{team_a} v {team_b} message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>"
                    )
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the pick ever settling. Unlike
            # every 365scores/ESPN-backed tracker in this bot, neither
            # hawk.live nor GosuGamers exposes a reliable "postponed/
            # interrupted, never resumed" signal to tag the standalone card
            # Voided instead (same limitation as ufctracker.py) - just gives
            # up silently (logged only) rather than guessing at a result
            # there. A parlay leg still gets Voided though - unlike guessing
            # won/lost, void is the same "can't tell, don't leave it stuck
            # forever" call already made for an unplayed map in
            # esports.grade_map_winner.
            botlog.event(
                f"⚠️ Auto-stopped tracking (esports {market}): "
                f"{team_a} v {team_b} never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>"
            )
            await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Esports tracker crashed unexpectedly for %s v %s (%s) in channel %s", team_a, team_b, market, channel_id)
        botlog.event(
            f"⚠️ Auto-stopped tracking (esports {market}): "
            f"{team_a} v {team_b} crashed unexpectedly (see server logs), in <#{channel_id}>"
        )
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, sport, team_a, team_b, market)


def start_tracking(
    message: discord.Message, sport: str, team_a: str, team_b: str, channel_id: int, market: str, owner_id: int,
    picked_team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    key = track_key(channel_id, sport, team_a, team_b, market)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(
            message, sport, team_a, team_b, channel_id, market, owner_id,
            picked_team, direction, line, map_number, picked_maps, other_maps,
        )
    )
    _active[key] = task
    register_message(message.id, channel_id, sport, team_a, team_b, market, owner_id)
    _persist(channel_id, sport, team_a, team_b, market, message.id, owner_id, picked_team, direction, line, map_number, picked_maps, other_maps)
    dailylog.record_pick(
        channel_id, "esportstracker", key, section,
        label or pick_label(market, picked_team, direction, line, map_number, picked_maps, other_maps), message.id,
        origin_channel_id, sport="Esports",
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/match is gone - cleans up instead.
    """
    for key, entry in list(state.load_esports().items()):
        try:
            channel_id, sport, team_a, team_b, market, message_id = (
                entry["channel_id"], entry["sport"], entry["team_a"], entry["team_b"], entry["market"], entry["message_id"]
            )
        except KeyError:
            log.warning("Dropping esports entry from an incompatible state schema: %r", entry)
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
            botlog.event(f"⚠️ Dropped on resume (esports {market}): **{team_a} v {team_b}** — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        # hawk.live/GosuGamers are flaky scrapers (bot-challenge pages,
        # transient fetch errors) - a single miss right here at startup used
        # to forget the match forever, permanently killing tracking on the
        # unlucky restart that lands during one bad fetch, even though the
        # live loop itself tolerates MAX_CONSECUTIVE_MISSES misses in a row.
        # Confirmed live: a match stuck showing its pre-kickoff "NOT
        # STARTED" card for hours after actually finishing, with hawk.live
        # itself resolving it fine once queried fresh - the resumed task had
        # simply never been recreated. Retrying here closes that gap.
        series_data = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            series_data = await asyncio.to_thread(esports.get_series, sport, team_a, team_b)
            if series_data:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not series_data:
            botlog.event(f"⚠️ Dropped on resume (esports {market}): **{team_a} v {team_b}** not found after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(
            message, sport, team_a, team_b, channel_id, market, owner_id,
            entry.get("picked_team"), entry.get("direction"), entry.get("line"),
            entry.get("map_number"), entry.get("picked_maps"), entry.get("other_maps"),
        )
        log.info("Resumed esports (%s, %s) tracking for %s v %s in channel %s", sport, market, team_a, team_b, channel_id)

#!/usr/bin/env python3
"""
Manages background tasks for soccer player prop picks - two data sources
depending on the stat. Goals/Assists/Yellow Cards/Red Cards are backed by
365scores' single-game detail endpoint (see scores365.soccer_player_stat/
find_soccer_player), unchanged since before playerstatsfootball.py existed.
Shots/Shots on Target/Tackles/Fouls Committed/Fouls Drawn/Dispossessed/
Offsides/Key Passes are backed by playerstats.football instead (see that
module's docstring) - 365scores only exposes those as team-level
aggregates, never broken out per player, confirmed live. ESPN doesn't
support soccer at all either way (see espn.py's module docstring).

365scores is ALWAYS still used for match status/timing/hibernation/kickoff
- even a playerstatsfootball-backed pick's game/notstarted/finished/
interrupted state comes from scores365.soccer_game_detail exactly as
before. playerstatsfootball only ever supplies the live stat VALUE itself,
via a separately-resolved fixture_path (see _current_value below) - this
keeps every other piece of this tracker (hibernation, void-on-timeout,
interrupted-match handling, parlay progress reporting) identical regardless
of which source a given pick's stat comes from.

Unlike tennis (where the "competitor" in 365scores' data model already IS
the player), a soccer "competitor" is a club - finding which live/imminent
match a named player is even in requires opening each candidate match's own
roster (see find_soccer_player), and 365scores-sourced stats are graded by
counting matching play-by-play events for that player rather than reading a
stat value directly (365scores has no continuous per-player stat endpoint
for soccer).

Mirrors proptracker.py's team-affiliated card layout (team name + opponent,
not just "vs opponent" like tennis) since a soccer player genuinely has a
club, unlike tennis. Otherwise mirrors tennispropstracker.py/settracker.py's
design: hibernation before start, 🗑️-reaction delete, restart-safe
persistence, early-win tagging for Over picks, and a Won/Lost/Push/Voided
result reaction once the match finishes (or is abandoned mid-interruption).
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
import playerstatsfootball
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.soccerpropstracker")
MAX_CONSECUTIVE_MISSES = 3

MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, member_id, stat_name, owner_id) - lets
# the reaction-based delete handler in bot.py look up who can delete a
# message.
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


def prop_key(
    channel_id: int, game_id, member_id, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None,
) -> str:
    """Different lines/directions on the same player+stat must never
    collide - same fix as tracker.py's track_key (see its docstring)."""
    market = f"{direction}:{line:g}" if direction and line is not None else "manual"
    return f"{channel_id}:{game_id}:{member_id}:{stat_name}:{market}"


def is_tracked(
    channel_id: int, game_id, member_id, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None,
) -> bool:
    return prop_key(channel_id, game_id, member_id, stat_name, direction, line) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_soccer_props().values()
        if prop_key(
            entry["channel_id"], entry["game_id"], entry["member_id"], entry["stat_name"],
            entry.get("direction"), entry.get("line"),
        ) in active_keys
    ]


def register_message(
    message_id: int, channel_id: int, game_id, member_id, stat_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, member_id, stat_name, direction, line, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, game_id, member_id, stat_name: str, message_id: int,
    member_competitor_id, photo_url: Optional[str], stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None, fixture_path: Optional[str] = None,
):
    data = state.load_soccer_props()
    data[prop_key(channel_id, game_id, member_id, stat_name, direction, line)] = {
        "channel_id": channel_id, "game_id": game_id, "member_id": member_id, "stat_name": stat_name,
        "message_id": message_id, "member_competitor_id": member_competitor_id, "photo_url": photo_url,
        "stat_label": stat_label, "player_name": player_name, "owner_id": owner_id,
        "direction": direction, "line": line, "fixture_path": fixture_path,
    }
    state.save_soccer_props(data)


def _forget(
    channel_id: int, game_id, member_id, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    data = state.load_soccer_props()
    data.pop(prop_key(channel_id, game_id, member_id, stat_name, direction, line), None)
    state.save_soccer_props(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via prop_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_soccer_props()
    data.pop(key, None)
    state.save_soccer_props(data)


def stop_tracking(
    channel_id: int, game_id, member_id, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None,
) -> bool:
    key = prop_key(channel_id, game_id, member_id, stat_name, direction, line)
    task = _active.pop(key, None)
    _forget(channel_id, game_id, member_id, stat_name, direction, line)
    dailylog.record_result(channel_id, "soccerpropstracker", key, "void", "Manually untracked")
    for message_id, (c_id, g_id, m_id, s_name, drct, ln, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and m_id == member_id and s_name == stat_name and drct == direction and ln == line:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _fmt_value(v) -> str:
    return "-" if v is None else str(v)


def _current_value(game: dict, member_id, player_name: str, stat_name: str, psf_match: Optional[dict]):
    """Routes to whichever source actually carries this stat - see this
    module's own docstring. psf_match is the already-fetched-and-parsed
    playerstats.football match dict (or None if that fetch failed/hasn't
    happened yet this cycle) - this function never fetches anything itself,
    same fetch/render separation as every other tracker in this codebase."""
    if stat_name in playerstatsfootball.STAT_CATALOG:
        return playerstatsfootball.get_player_stat(psf_match, player_name, stat_name) if psf_match else None
    return scores365.soccer_player_stat(game, member_id, stat_name)


async def build_embed(
    game: dict, member_id, member_competitor_id, player_name: str, photo_url: Optional[str],
    stat_label: str, stat_name: str,
    direction: Optional[str] = None, line: Optional[float] = None, force_result: Optional[str] = None,
    message_id: Optional[int] = None, psf_match: Optional[dict] = None,
) -> tuple[discord.Embed, discord.File]:
    """force_result overrides the color/title as if this were already graded
    that way, regardless of the game's actual live status - used only by
    _track_loop's interrupted-and-never-resumed timeout branch."""
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))
    is_home = home_competitor.get("id") == member_competitor_id
    team_name = (home_competitor if is_home else away_competitor).get("name", "?")

    current_value = None
    if status != "notstarted":
        current_value = _current_value(game, member_id, player_name, stat_name, psf_match)

    result = None
    if status == "finished" and direction is not None and line is not None:
        result = scores365.grade_over_under(current_value, direction, line)
        if result is None and scores365.is_cancelled(game):
            # The match will never produce a real stat value - see
            # tracker.py's identical fix for why this matters (falling
            # through to the generic "finished, nothing to grade" green
            # fallback misleadingly looked like the match settled normally).
            result = "void"

    early_win = False
    if not result and status == "inprogress" and direction == "over" and line is not None:
        # Same early-win idea as proptracker.py/tennispropstracker.py's Over
        # tagging - goals/assists/cards only ever accumulate during a match,
        # so once the line is already cleared it can't un-clear.
        try:
            if current_value is not None and float(current_value) > line:
                early_win = True
        except (TypeError, ValueError):
            pass

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

    description_lines = [f"{home_competitor.get('name', '?')} v {away_competitor.get('name', '?')}"]
    if direction is not None and line is not None:
        description_lines.append(f"{player_name} {direction.title()} {line:g} {stat_label}")
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    description = "\n".join(description_lines)

    sport_id = scores365.SPORT_IDS["soccer"]
    period_text = "" if status == "notstarted" else scores365.status_line(game, sport_id)
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        team_name, photo_url, player_name, stat_label, _fmt_value(current_value), color_status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if result:
        embed.title = _RESULT_TITLES[result]
    elif early_win:
        embed.title = _RESULT_TITLES["won"]
    author_bits = [b for b in (scores365.sport_label(sport_id), game.get("competitionDisplayName")) if b]
    if author_bits:
        embed.set_author(name=" • ".join(author_bits))
    embed.description = description
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message, game_id, channel_id: int, member_id, member_competitor_id, stat_name: str,
    photo_url: Optional[str], stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None, fixture_path: Optional[str] = None,
):
    key = prop_key(channel_id, game_id, member_id, stat_name, direction, line)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-start, graded,
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
            log.warning("Failed to repost final soccer prop tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit soccer prop tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final soccer prop tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, game_id, member_id, stat_name, owner_id, direction, line)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old soccer prop tracking message after final repost: %s", e)
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
        dailylog.record_result(channel_id, "soccerpropstracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "soccerpropstracker", key)
        if not group_ids:
            return
        pick_desc = f"{direction.title()} {line:g} {stat_label}" if direction is not None and line is not None else ""
        if game:
            void_home = game.get("homeCompetitor") or {}
            void_away = game.get("awayCompetitor") or {}
            opponent = void_away.get("name", "?") if void_home.get("id") == member_competitor_id else void_home.get("name", "?")
            matchup = f"{player_name} vs {opponent}"
        else:
            matchup = f"{player_name} (game `{game_id}`)"
        void_label = matchup + (f" - {pick_desc}" if pick_desc else "")
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "soccerpropstracker", key, void_label, "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    psf_match = None
    psf_html = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.soccer_game_detail, game_id)

            # A notstarted match's events can't change before it starts, so
            # hibernate instead of polling every cycle - same pattern as
            # tracker.py/tennispropstracker.py.
            hibernated = False
            while game and scores365.map_status_type(game.get("statusGroup"), game.get("statusText")) == "notstarted":
                kickoff = scores365.start_epoch(game)
                if not kickoff:
                    break
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
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
                dailylog.touch(channel_id, "soccerpropstracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "soccerpropstracker", key)
                if group_ids:
                    pre_home = game.get("homeCompetitor") or {}
                    pre_away = game.get("awayCompetitor") or {}
                    pre_opponent = pre_away.get("name", "?") if pre_home.get("id") == member_competitor_id else pre_home.get("name", "?")
                    pre_pick = (
                        f"{direction.title()} {line:g} {stat_label}" if direction is not None and line is not None else ""
                    )
                    pre_label = f"{player_name} vs {pre_opponent}" + (f" - {pre_pick}" if pre_pick else "")
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "soccerpropstracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Soccer prop game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.soccer_game_detail, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "Soccer prop game %s not found on 365scores (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (soccer prop): **{player_name}** — game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Game not found on 365scores")
                    break
                continue
            consecutive_misses = 0

            if fixture_path:
                psf_html = await asyncio.to_thread(playerstatsfootball.fetch_path, fixture_path)
                psf_match = playerstatsfootball.parse_match(psf_html) if psf_html else None

            embed, file = await build_embed(
                game, member_id, member_competitor_id, player_name, photo_url, stat_label, stat_name, direction, line,
                message_id=message.id, psf_match=psf_match,
            )
            leg_home = game.get("homeCompetitor") or {}
            leg_away = game.get("awayCompetitor") or {}
            leg_opponent = leg_away.get("name", "?") if leg_home.get("id") == member_competitor_id else leg_home.get("name", "?")
            leg_pick = f"{direction.title()} {line:g} {stat_label}" if direction is not None and line is not None else ""
            leg_label = f"{player_name} vs {leg_opponent}" + (f" - {leg_pick}" if leg_pick else "")

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                await _repost_final(embed, file)
                _persist(
                    channel_id, game_id, member_id, stat_name, message.id, member_competitor_id, photo_url,
                    stat_label, player_name, owner_id, direction, line, fixture_path,
                )
                continue

            if scores365.is_finished(game):
                await _repost_final(embed, file)

                if direction is not None and line is not None:
                    current_value = _current_value(game, member_id, player_name, stat_name, psf_match)
                    result = scores365.grade_over_under(current_value, direction, line)
                    reason = None
                    if result is None:
                        # The game finished but there's no usable value to
                        # grade against - previously this branch just left
                        # result as None, which meant dailylog.record_result
                        # below never even ran (falsy result), silently
                        # stranding the pick on "pending" forever. Confirmed
                        # live: a finished match's card sat showing "Not
                        # Started" for hours with nothing left running to
                        # ever fix it.
                        result = "void"
                        if scores365.is_cancelled(game):
                            reason = "Cancelled"
                        elif psf_html and playerstatsfootball.was_substituted(psf_html, player_name):
                            # Confirmed live: a player substituted out mid-
                            # match genuinely played, but this source's
                            # structured data only covers players still on
                            # the field at full time - get_player_stat
                            # returning None here doesn't mean DNP.
                            reason = "Player was substituted - stat not available from this source"
                        else:
                            reason = "No usable stat value"
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                    if result:
                        dailylog.record_result(channel_id, "soccerpropstracker", key, result, reason)
                        group_ids = parlaytracker.groups_for_leg(channel_id, "soccerpropstracker", key)
                        await parlaytracker.handle_leg_result(
                            message.channel, channel_id, message, "soccerpropstracker", key, leg_label, result, group_ids,
                        )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            kickoff = scores365.start_epoch(game)
            if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
            else:
                detail = f"LIVE, {scores365.status_line(game, scores365.SPORT_IDS['soccer'])}"
            dailylog.touch(channel_id, "soccerpropstracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "soccerpropstracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "soccerpropstracker", key, leg_label, detail, group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
                consecutive_rate_limit_failures = 0
            except discord.HTTPException as e:
                if e.status == 429:
                    consecutive_rate_limit_failures += 1
                    log.warning(
                        "Failed to edit soccer prop tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking (soccer prop): **{player_name}** — message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit soccer prop tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (soccer prop): **{player_name}** — message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the match ever finishing. If it
            # was left mid-interruption rather than genuinely still in
            # progress, that's stalled, not just slow - tag it Voided/No
            # Action instead of silently leaving the card stuck with no
            # result and no cleanup.
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(
                    game, member_id, member_competitor_id, player_name, photo_url, stat_label, stat_name,
                    direction, line, force_result="void", message_id=message.id, psf_match=psf_match,
                )
                embed.title = _RESULT_TITLES["void"]
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                dailylog.record_result(channel_id, "soccerpropstracker", key, "void", "Interrupted, never resumed")
                group_ids = parlaytracker.groups_for_leg(channel_id, "soccerpropstracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "soccerpropstracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided (soccer prop, interrupted, never resumed): **{player_name}** in <#{channel_id}>")
            else:
                # Timed out without ever finishing and without an interrupted
                # signal to go on either - the standalone card is left alone
                # (no reliable result to guess at), but a parlay leg still
                # gets Voided so its summary card isn't stuck forever.
                botlog.event(f"⚠️ Auto-stopped tracking (soccer prop): **{player_name}** never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
                await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Soccer prop tracker crashed unexpectedly for game %s (%s) in channel %s", game_id, player_name, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (soccer prop): **{player_name}** — game `{game_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, member_id, stat_name, direction, line)


def start_tracking(
    message: discord.Message, game_id, channel_id: int, member_id, member_competitor_id, stat_name: str,
    photo_url: Optional[str], stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None, fixture_path: Optional[str] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    key = prop_key(channel_id, game_id, member_id, stat_name, direction, line)
    if key in _active:
        return
    task = asyncio.create_task(
        _track_loop(
            message, game_id, channel_id, member_id, member_competitor_id, stat_name, photo_url,
            stat_label, player_name, owner_id, direction, line, fixture_path,
        )
    )
    _active[key] = task
    register_message(message.id, channel_id, game_id, member_id, stat_name, owner_id, direction, line)
    _persist(
        channel_id, game_id, member_id, stat_name, message.id, member_competitor_id, photo_url,
        stat_label, player_name, owner_id, direction, line, fixture_path,
    )
    dailylog.record_pick(
        channel_id, "soccerpropstracker", key, section, label or player_name, message.id, origin_channel_id,
        sport="Soccer",
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for key, entry in list(state.load_soccer_props().items()):
        try:
            channel_id, game_id, member_id, stat_name = (
                entry["channel_id"], entry["game_id"], entry["member_id"], entry["stat_name"]
            )
        except KeyError:
            log.warning("Dropping soccer prop entry from an incompatible state schema: %r", entry)
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
            # Silent before this fix - see tracker.py's identical resume_all
            # fix for why this matters.
            botlog.event(f"⚠️ Dropped on resume (soccer prop): **{entry.get('player_name', member_id)}** — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        # A single miss right here at startup used to forget the game
        # forever, permanently killing tracking on the unlucky restart that
        # lands during one transient 365scores hiccup, even though the live
        # loop itself tolerates MAX_CONSECUTIVE_MISSES misses in a row.
        # Retrying here closes that gap.
        game = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            game = await asyncio.to_thread(scores365.soccer_game_detail, game_id)
            if game:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not game:
            botlog.event(f"⚠️ Dropped on resume (soccer prop): **{entry.get('player_name', member_id)}** — game `{game_id}` not found on 365scores after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(
            message, game_id, channel_id, member_id, entry["member_competitor_id"], stat_name,
            entry.get("photo_url"), entry["stat_label"], entry["player_name"], entry.get("owner_id"),
            entry.get("direction"), entry.get("line"), entry.get("fixture_path"),
        )
        log.info("Resumed soccer prop tracking for %s in channel %s", entry["player_name"], channel_id)

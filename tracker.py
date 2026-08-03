#!/usr/bin/env python3
"""Manages background tasks that keep a live-score embed updated."""

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

log = logging.getLogger("scorebox.tracker")

TRASH_EMOJI = "🗑️"

# Tolerate this many consecutive "game not found" polls before giving up -
# 365scores' pagination can transiently return an incomplete list (e.g. a
# mid-fetch network hiccup), which would otherwise silently kill tracking
# for a game that's still very much alive.
MAX_CONSECUTIVE_MISSES = 3

# Keyed by f"{channel_id}:{game_id}" -> asyncio.Task
_active_tracks: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, game_id, owner_id) - lets the reaction-based
# delete handler in bot.py look up who's allowed to delete a given message.
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


def _grade(
    game: dict, picked_team: Optional[str], team_total: Optional[str],
    total_direction: Optional[str], total_line: Optional[float],
) -> Optional[str]:
    if picked_team:
        return scores365.grade_moneyline(game, picked_team)
    if team_total and total_direction and total_line is not None:
        return scores365.grade_team_total(game, team_total, total_direction, total_line)
    if total_direction and total_line is not None:
        return scores365.grade_total(game, total_direction, total_line)
    return None


async def build_embed(
    game: dict, sport_id: Optional[int] = None, picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None, force_result: Optional[str] = None,
) -> tuple[discord.Embed, discord.File]:
    """Returns (embed, file) - the score image must be sent/edited alongside the embed.

    picked_team/(total_direction, total_line)/team_total are only set for
    auto-tracked picks (not manual /track usage, which isn't grading a bet) -
    exactly one of these three modes applies per pick: picked_team is a
    moneyline, total_direction+total_line alone is a combined game total,
    and total_direction+total_line+team_total is one side's own total
    instead of the combined score. Once the match finishes, whichever mode
    is set gets compared against the final score to show a Won/Lost/Push
    badge in the embed title.

    force_result overrides the color/title as if this were already graded
    that way, regardless of the game's actual live status - used only by
    _track_loop's interrupted-and-never-resumed timeout branch, where the
    match itself is still sitting at "inprogress" in 365scores' own status
    but the pick is being voided anyway.
    """
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"), game.get("statusText"))

    result = _grade(game, picked_team, team_total, total_direction, total_line) if status == "finished" else None

    early_win = False
    if not result and status == "inprogress" and total_direction == "over" and total_line is not None:
        # A score only ever climbs during a game - once an Over line is
        # already cleared, it can't un-clear, so it's safe to tag the pick a
        # win before the match actually ends. Unders and moneyline picks are
        # deliberately excluded here - a leading score can still change, and
        # an Under total could still climb past the line later, so only a
        # cleared Over is ever safe to call early.
        current_scores = scores365.main_scores(game)
        if current_scores:
            if team_total:
                home = home_competitor.get("name", "")
                away = away_competitor.get("name", "")
                if scores365.names_match(home, team_total):
                    value = current_scores[0]
                elif scores365.names_match(away, team_total):
                    value = current_scores[1]
                else:
                    value = None
            else:
                value = current_scores[0] + current_scores[1]
            if value is not None and value > total_line:
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

    # The pick stays visible for the card's whole lifetime (auto-tracked
    # picks only - manual /track has no pick to label). Discord's own
    # <t:...:f> tag renders "Today"/"Tomorrow"/weekday name and the clock time
    # already localized to whoever's viewing it - no need to compute that
    # ourselves per-viewer. Only shown pre-match; dropped once live to save
    # space, since the card itself covers it from then on.
    if picked_team:
        description_lines = [f"{picked_team} ML"]
    elif team_total and total_direction and total_line is not None:
        description_lines = [f"{team_total} {total_direction.title()} {total_line:g}"]
    elif total_direction and total_line is not None:
        description_lines = [f"{total_direction.title()} {total_line:g}"]
    else:
        description_lines = []
    if status == "notstarted":
        kickoff = scores365.start_epoch(game)
        if kickoff:
            description_lines.append(f"<t:{int(kickoff)}:f>")
    if description_lines:
        embed.description = "\n".join(description_lines)

    # Pre-match, the pill is redundant now that the localized kickoff time
    # shows above the image (embed description) - skip it there and only
    # show the live period/clock (e.g. "2nd Half (98:00)") once it starts.
    period_text = "" if status == "notstarted" else scores365.status_line(game, sport_id)

    # Each row is [main score, smaller sub-tier(s)...] left-to-right, main
    # score first (rendered biggest/boldest, on the left) - tennis gets
    # sets+games+points, volleyball gets sets+current-set score, everything
    # else just gets its plain score.
    main_scores = scores365.main_scores(game)
    home_cols: list[str] = [scores365.fmt_score(main_scores[0]) if main_scores else "-"]
    away_cols: list[str] = [scores365.fmt_score(main_scores[1]) if main_scores else "-"]

    set_score = scores365.current_set_score(game, sport_id)
    if set_score:
        home_cols.append(scores365.fmt_score(set_score[0]))
        away_cols.append(scores365.fmt_score(set_score[1]))

    if sport_id == scores365.SPORT_IDS["tennis"] and status == "inprogress":
        points = scores365.tennis_current_game_points(game)
        if points:
            home_cols.append(scores365.tennis_point_label(points[0]))
            away_cols.append(scores365.tennis_point_label(points[1]))

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


def register_message(message_id: int, channel_id: int, game_id, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def track_key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def is_tracked(channel_id: int, game_id) -> bool:
    return track_key(channel_id, game_id) in _active_tracks


def list_tracked(channel_id: int) -> list[str]:
    prefix = f"{channel_id}:"
    return [key.split(":", 1)[1] for key in _active_tracks if key.startswith(prefix)]


def list_tracked_details(channel_id: int) -> list[dict]:
    """Same game IDs as list_tracked, paired with their sport_id (from
    persisted state) so callers can look up team names for display."""
    game_ids = set(list_tracked(channel_id))  # strings, e.g. "4790091"
    return [
        entry for entry in state.load_tracks().values()
        if entry["channel_id"] == channel_id and str(entry["game_id"]) in game_ids
    ]


def _persist(
    channel_id: int, game_id, message_id: int, sport_id, owner_id: int, picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None, team_total: Optional[str] = None,
):
    data = state.load_tracks()
    data[track_key(channel_id, game_id)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "picked_team": picked_team,
        "total_direction": total_direction, "total_line": total_line, "team_total": team_total,
    }
    state.save_tracks(data)


def _forget(channel_id: int, game_id):
    data = state.load_tracks()
    data.pop(track_key(channel_id, game_id), None)
    state.save_tracks(data)


def stop_tracking(channel_id: int, game_id) -> bool:
    key = track_key(channel_id, game_id)
    task = _active_tracks.pop(key, None)
    _forget(channel_id, game_id)
    for message_id, (c_id, g_id, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


async def _track_loop(
    message: discord.Message, sport_id: int, game_id, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None,
):
    key = track_key(channel_id, game_id)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    consecutive_misses = 0
    consecutive_edit_failures = 0

    async def _repost_final(embed: discord.Embed, file: discord.File):
        """Bumps the card to the bottom of the channel (pre-kickoff, graded,
        or voided) - a long-running match can get buried under chat by the
        time anything actually changes. Falls back to editing in place if
        the repost send itself fails.

        Carries forward any reaction someone added beyond the bot's own
        service ones (e.g. tagging a card as part of a parlay) - confirmed
        live this was otherwise silently lost every time a repost deleted
        the old message. Discord has no way to make a reaction reappear as
        added by the original user once that message is gone, so this
        re-adds the same emoji under the bot's own account instead - the
        marker survives, just no longer attributed to whoever added it."""
        nonlocal message
        try:
            fresh = await message.channel.fetch_message(message.id)
            carry_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
        except discord.HTTPException:
            carry_emojis = []

        try:
            new_message = await throttle.run(channel_id, lambda: message.channel.send(embed=embed, file=file))
        except discord.HTTPException as e:
            log.warning("Failed to repost final tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final tracking message: %s", e)
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
            log.warning("Failed to delete old tracking message after final repost: %s", e)
        return carry_emojis

    # Random head start so many trackers started around the same moment don't
    # all land on the same wall-clock instant every cycle and pile up against
    # Discord's per-channel edit rate limit together.
    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    game = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            # A notstarted match's score/status can't change before kickoff,
            # so hibernate instead of polling every cycle - this is also what
            # was driving the rate-limit contention with several simultaneous
            # trackers. Wakes once per Eastern-time day boundary (to keep the
            # displayed "Today"/"Tomorrow" label accurate) and once more just
            # before it actually starts, editing the message each time.
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
                # Hibernating shouldn't eat into the MAX_TRACK_HOURS budget
                # meant for the actual live match - push the deadline out by
                # the same amount so a distant kickoff doesn't get silently
                # killed before it ever goes live.
                deadline += hibernate_for
                hibernated = True
                log.info("Game %s not starting soon; hibernating %.0fs", game_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)

            if not game:
                consecutive_misses += 1
                log.warning(
                    "Game %s not found in 365scores' current list (miss %d/%d)",
                    game_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (game `{game_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row) in <#{channel_id}>")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(game, sport_id, picked_team, total_direction, total_line, team_total)
            # Used for a parlay leg's own label if this card carries a marker
            # emoji - the pick line when there is one (auto-tracked), else a
            # plain matchup (manual /track has no pick to describe).
            leg_label = embed.description.splitlines()[0] if embed.description else (
                f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            )

            if hibernated:
                # Report in as NOT STARTED even while still hibernating -
                # otherwise a leg with a kickoff hours away would never
                # appear on its parlay's summary card at all until it woke
                # up within 90s of starting (confirmed live: a 4-leg parlay
                # only showed 1 leg, the only one already live).
                try:
                    fresh = await message.channel.fetch_message(message.id)
                    marker_emojis = [r.emoji for r in fresh.reactions if str(r.emoji) not in _SERVICE_EMOJIS]
                except discord.HTTPException:
                    marker_emojis = []
                if marker_emojis:
                    kickoff = scores365.start_epoch(game)
                    detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "tracker", key, leg_label, detail, marker_emojis,
                    )
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. This is the one moment reposting helps
                # rather than hurts, since it's about to actually go live.
                await _repost_final(embed, file)
                _persist(
                    channel_id, game_id, message.id, sport_id, owner_id, picked_team,
                    total_direction, total_line, team_total,
                )
                continue

            if scores365.is_finished(game):
                carry_emojis = await _repost_final(embed, file)

                if picked_team or (total_direction and total_line is not None):
                    result = _grade(game, picked_team, team_total, total_direction, total_line)
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                    if result:
                        await parlaytracker.handle_leg_result(
                            message.channel, channel_id, message, "tracker", key, leg_label, result, carry_emojis,
                        )
                pendingdelete.start(channel_id, message, embed.description or "")
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
                    message.channel, channel_id, message, "tracker", key, leg_label, detail, marker_emojis,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (message edit failed {MAX_CONSECUTIVE_MISSES}x in a row) for game `{game_id}` in <#{channel_id}>")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without ever reaching a real result. If
            # the match was left mid-interruption (rain delay, darkness,
            # etc.) rather than genuinely still in progress, that's stalled,
            # not just slow - tag it Voided/No Action instead of silently
            # leaving the card stuck showing "Interrupted" forever with no
            # result and no cleanup (confirmed this was the prior behavior).
            if game and scores365.is_interrupted(game):
                embed, file = await build_embed(
                    game, sport_id, picked_team, total_direction, total_line, team_total, force_result="void",
                )
                embed.title = _RESULT_TITLES["void"]
                carry_emojis = await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "tracker", key, leg_label, "void", carry_emojis,
                )
                botlog.event(f"➖ Voided (interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
    except asyncio.CancelledError:
        raise
    finally:
        _active_tracks.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id)
    if key in _active_tracks:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, owner_id, picked_team, total_direction, total_line, team_total)
    )
    _active_tracks[key] = task
    register_message(message.id, channel_id, game_id, owner_id)
    _persist(channel_id, game_id, message.id, sport_id, owner_id, picked_team, total_direction, total_line, team_total)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for entry in list(state.load_tracks().values()):
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

        # Even an already-finished game still needs to be handed to
        # start_tracking/_track_loop - it re-registers the message (so the
        # 🗑️ reaction keeps working) and re-arms the 24h auto-delete timer,
        # which would otherwise be silently lost on every restart.
        start_tracking(
            message, sport_id, game, channel_id, owner_id,
            entry.get("picked_team"), entry.get("total_direction"), entry.get("total_line"),
            entry.get("team_total"),
        )
        log.info("Resumed tracking game %s in channel %s", game_id, channel_id)

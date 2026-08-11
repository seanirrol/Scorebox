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
import dailylog
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
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "push": "➖", "void": "<:cashback:1533844020839841832>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via 365scores" if message_id else "Scorebox • data via 365scores"


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
    team_total: Optional[str] = None, force_result: Optional[str] = None, message_id: Optional[int] = None,
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
    has_pick = picked_team or (total_direction and total_line is not None)
    if not result and has_pick and scores365.is_cancelled(game):
        result = "void"

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

    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


def register_message(
    message_id: int, channel_id: int, game_id, owner_id: int, picked_team: Optional[str] = None,
    team_total: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, game_id, picked_team, team_total, total_direction, total_line, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def track_key(
    channel_id: int, game_id, picked_team: Optional[str] = None, team_total: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
) -> str:
    """Different bet types on the same game must never collide - confirmed
    live: tracking "Los Angeles Dodgers Over 3.5" (a team total) then
    "Los Angeles Dodgers ML" (moneyline) on the same game rejected the ML
    as "already tracked", since this key used to be bare channel_id:game_id
    with no market discriminator at all - only one pick per game per
    channel could ever be tracked here, regardless of bet type."""
    if picked_team:
        market = f"ml:{picked_team}"
    elif team_total and total_direction and total_line is not None:
        market = f"tt:{team_total}:{total_direction}:{total_line:g}"
    elif total_direction and total_line is not None:
        market = f"total:{total_direction}:{total_line:g}"
    else:
        market = "manual"
    return f"{channel_id}:{game_id}:{market}"


def is_tracked(
    channel_id: int, game_id, picked_team: Optional[str] = None, team_total: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
) -> bool:
    return track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line) in _active_tracks


def list_tracked_details(channel_id: int) -> list[dict]:
    """Recomputes each persisted entry's own key (same pattern as
    f5tracker.py/halftracker.py) rather than matching by bare game_id -
    necessary now that multiple entries can share the same game_id with
    different markets."""
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active_tracks if k.startswith(prefix)}
    return [
        entry for entry in state.load_tracks().values()
        if track_key(
            entry["channel_id"], entry["game_id"], entry.get("picked_team"), entry.get("team_total"),
            entry.get("total_direction"), entry.get("total_line"),
        ) in active_keys
    ]


def _persist(
    channel_id: int, game_id, message_id: int, sport_id, owner_id: int, picked_team: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None, team_total: Optional[str] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """section/label/origin_channel_id are persisted purely so resume_all
    can hand them back to dailylog.record_pick on restart (see start_tracking)
    - without them, a resumed pick's dailylog entry silently stops updating
    forever the moment track_key's own format ever changes (confirmed live:
    exactly this happened when track_key gained a market discriminator -
    a restart mid-tracking orphaned the old-format dailylog entry, and
    resume_all had nothing to re-link a fresh one with, since it never had
    section/label to pass to record_pick in the first place)."""
    data = state.load_tracks()
    data[track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line)] = {
        "channel_id": channel_id, "game_id": game_id, "message_id": message_id,
        "sport_id": sport_id, "owner_id": owner_id, "picked_team": picked_team,
        "total_direction": total_direction, "total_line": total_line, "team_total": team_total,
        "section": section, "label": label, "origin_channel_id": origin_channel_id,
    }
    state.save_tracks(data)


def _forget(
    channel_id: int, game_id, picked_team: Optional[str] = None, team_total: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    data = state.load_tracks()
    data.pop(track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line), None)
    state.save_tracks(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - only resume_all
    has that original key on hand (from iterating state.load_tracks()
    itself). Confirmed live: track_key()'s shape has changed over time (a
    team_total/direction/line discriminator was added after some entries
    were already persisted), so _forget()'s reconstructed key can silently
    fail to match an old-format entry - it never raises, .pop(key, None)
    just quietly finds nothing, so the "dropped" entry actually stays in
    state forever and re-fails on every future restart. Reusing the exact
    key resume_all already has sidesteps any future schema drift entirely."""
    data = state.load_tracks()
    data.pop(key, None)
    state.save_tracks(data)


def stop_tracking(
    channel_id: int, game_id, picked_team: Optional[str] = None, team_total: Optional[str] = None,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
) -> bool:
    key = track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line)
    task = _active_tracks.pop(key, None)
    _forget(channel_id, game_id, picked_team, team_total, total_direction, total_line)
    dailylog.record_result(channel_id, "tracker", key, "void")
    for message_id, (c_id, g_id, pt, tt, td, tl, _owner) in list(_message_owners.items()):
        if c_id == channel_id and g_id == game_id and pt == picked_team and tt == team_total and td == total_direction and tl == total_line:
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
    key = track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line)
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
        register_message(message.id, channel_id, game_id, owner_id, picked_team, team_total, total_direction, total_line)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up():
        """Called on every path where this tracker gives up without ever
        reaching a real result (game never found again, Discord edits
        failing repeatedly, or a MAX_TRACK_HOURS timeout with no reliable
        interrupted/never-resumed signal to go on) - reports the leg as
        Voided to its parlay group instead of leaving the summary card
        frozen on whatever pending detail it last reported, forever, once
        this task quietly stops polling."""
        dailylog.record_result(channel_id, "tracker", key, "void")
        group_ids = parlaytracker.groups_for_leg(channel_id, "tracker", key)
        if not group_ids:
            return
        if game:
            matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
        else:
            matchup = f"Game `{game_id}`"
        if picked_team:
            pick_desc = f"{picked_team} ML"
        elif team_total and total_direction and total_line is not None:
            pick_desc = f"{team_total} {total_direction.title()} {total_line:g}"
        elif total_direction and total_line is not None:
            pick_desc = f"{total_direction.title()} {total_line:g}"
        else:
            pick_desc = None
        void_label = f"{matchup} - {pick_desc}" if pick_desc else matchup
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "tracker", key, void_label, "void", group_ids,
        )

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

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation later
                # ends (right before kickoff, see below) is too late to
                # matter: a leg with a kickoff hours away would still never
                # appear on its parlay's summary card until it was
                # basically already live anyway.
                dailylog.touch(channel_id, "tracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "tracker", key)
                if group_ids:
                    pre_embed, _pre_file = await build_embed(
                        game, sport_id, picked_team, total_direction, total_line, team_total, message_id=message.id,
                    )
                    pre_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
                    pre_pick = pre_embed.description.splitlines()[0] if pre_embed.description else None
                    pre_label = f"{pre_matchup} - {pre_pick}" if pre_pick and pre_pick != pre_matchup else pre_matchup
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "tracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

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
                    await _void_leg_and_give_up()
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(
                game, sport_id, picked_team, total_direction, total_line, team_total, message_id=message.id,
            )
            # Used for a parlay leg's own label if this card is part of a
            # parlay - always matchup-prefixed, so a leg's card is
            # identifiable on its own even sitting next to other legs on the
            # same market type from a totally different match. The pick line
            # itself (when there is one - manual /track has none) follows it.
            leg_matchup = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
            leg_pick = embed.description.splitlines()[0] if embed.description else None
            leg_label = f"{leg_matchup} - {leg_pick}" if leg_pick and leg_pick != leg_matchup else leg_matchup

            if hibernated:
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
                    if not result and scores365.is_cancelled(game):
                        # The match will never produce a real score to grade
                        # against - matches build_embed's own cancelled
                        # handling so the card's void color/title and the
                        # actual recorded result never disagree.
                        result = "void"
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                    if result:
                        dailylog.record_result(channel_id, "tracker", key, result)
                        group_ids = parlaytracker.groups_for_leg(channel_id, "tracker", key)
                        await parlaytracker.handle_leg_result(
                            message.channel, channel_id, message, "tracker", key, leg_label, result, group_ids,
                        )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            kickoff = scores365.start_epoch(game)
            if scores365.map_status_type(game.get("statusGroup")) == "notstarted":
                detail = f"NOT STARTED - <t:{int(kickoff)}:f>" if kickoff else "NOT STARTED"
            else:
                detail = f"LIVE, {scores365.status_line(game, sport_id)}"
            dailylog.touch(channel_id, "tracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "tracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "tracker", key, leg_label, detail, group_ids,
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
                    await _void_leg_and_give_up()
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
                    game, sport_id, picked_team, total_direction, total_line, team_total,
                    force_result="void", message_id=message.id,
                )
                embed.title = _RESULT_TITLES["void"]
                await _repost_final(embed, file)
                try:
                    await message.add_reaction(_RESULT_REACTIONS["void"])
                except discord.HTTPException as e:
                    log.warning("Failed to add void reaction: %s", e)
                pendingdelete.start(channel_id, message, embed.description or "")
                dailylog.record_result(channel_id, "tracker", key, "void")
                group_ids = parlaytracker.groups_for_leg(channel_id, "tracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "tracker", key, leg_label, "void", group_ids,
                )
                botlog.event(f"➖ Voided (interrupted, never resumed): game `{game_id}` in <#{channel_id}>")
            else:
                # Timed out without ever settling and without an interrupted
                # signal to go on either - the standalone card is left alone
                # (no reliable result to guess at), but a parlay leg still
                # gets Voided so its summary card isn't stuck forever.
                botlog.event(f"⚠️ Auto-stopped tracking: game `{game_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
                await _void_leg_and_give_up()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Anything else escaping this loop (a data shape we didn't guard
        # against, etc.) used to just kill the task silently - the card
        # frozen forever with no Discord/botlog signal, "Task exception was
        # never retrieved" only ever visible in server-side stderr.
        # Confirmed live: exactly this symptom on a real pick. Logging +
        # botlog + voiding the parlay leg turns a silent permanent freeze
        # into something visible and diagnosable, matching every other
        # give-up path above.
        log.exception("Tracker crashed unexpectedly for game %s in channel %s", game_id, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking: game `{game_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up()
    finally:
        _active_tracks.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, game_id, picked_team, team_total, total_direction, total_line)


def start_tracking(
    message: discord.Message, sport_id: int, game: dict, channel_id: int, owner_id: int,
    picked_team: Optional[str] = None, total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None, section: Optional[str] = None, label: Optional[str] = None,
    origin_channel_id: Optional[int] = None,
):
    game_id = game["id"]
    key = track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line)
    if key in _active_tracks:
        return
    task = asyncio.create_task(
        _track_loop(message, sport_id, game_id, channel_id, owner_id, picked_team, total_direction, total_line, team_total)
    )
    _active_tracks[key] = task
    register_message(message.id, channel_id, game_id, owner_id, picked_team, team_total, total_direction, total_line)
    if not label:
        label = f"{(game.get('homeCompetitor') or {}).get('name', '?')} vs {(game.get('awayCompetitor') or {}).get('name', '?')}"
    _persist(
        channel_id, game_id, message.id, sport_id, owner_id, picked_team, total_direction, total_line, team_total,
        section, label, origin_channel_id,
    )
    dailylog.record_pick(channel_id, "tracker", key, section, label, message.id, origin_channel_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/game is gone - cleans up instead.
    """
    for key, entry in list(state.load_tracks().items()):
        try:
            channel_id, game_id, message_id, sport_id = (
                entry["channel_id"], entry["game_id"], entry["message_id"], entry["sport_id"]
            )
        except KeyError:
            # Belongs to an incompatible/old state schema - can't be
            # resumed, just drop it rather than blocking every other entry
            # in this same loop (a single bad entry used to abort the whole
            # resume_all call, silently skipping every pick after it too).
            log.warning("Dropping track entry from an incompatible state schema: %r", entry)
            continue
        owner_id = entry.get("owner_id")
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Silent before this fix - confirmed live this made a pick
            # vanish from tracking on restart with zero trace anywhere,
            # unlike every other give-up path in this module, which always
            # logs. Worse, since the persisted state entry is gone too, the
            # exact same pick showing up again in a later message (a resend/
            # live-odds repost) reads as brand new rather than a duplicate,
            # silently resetting its /summary progress.
            botlog.event(f"⚠️ Dropped on resume: game `{game_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        # 365scores' pagination can transiently return an incomplete list
        # (e.g. a mid-fetch network hiccup) - a single miss right here at
        # startup used to forget the game forever, permanently killing
        # tracking on the unlucky restart that lands during one bad fetch,
        # even though the live loop itself tolerates MAX_CONSECUTIVE_MISSES
        # misses in a row. Retrying here closes that gap.
        game = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            game = await asyncio.to_thread(scores365.get_live_update, sport_id, game_id)
            if game:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not game:
            botlog.event(f"⚠️ Dropped on resume: game `{game_id}` not found on 365scores after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        # Even an already-finished game still needs to be handed to
        # start_tracking/_track_loop - it re-registers the message (so the
        # 🗑️ reaction keeps working) and re-arms the 24h auto-delete timer,
        # which would otherwise be silently lost on every restart.
        start_tracking(
            message, sport_id, game, channel_id, owner_id,
            entry.get("picked_team"), entry.get("total_direction"), entry.get("total_line"),
            entry.get("team_total"), entry.get("section"), entry.get("label"), entry.get("origin_channel_id"),
        )
        log.info("Resumed tracking game %s in channel %s", game_id, channel_id)

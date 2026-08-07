#!/usr/bin/env python3
"""
Manages background tasks that keep a live player-stat embed updated
(e.g. "Julio Rodriguez - Hits, currently 0"). Backed by ESPN's free site API
(see espn.py) for baseball/basketball/nfl/hockey - tennis and soccer aren't
supported yet (ESPN needs bespoke per-competition handling for those).

Mirrors tracker.py's design: same hibernation (a notstarted event can't
change, so sleep instead of polling every cycle, waking once per Eastern-time
day boundary and once more right before it starts), same repost-to-the-
bottom-of-the-channel on that final pre-start wake, same 🗑️-reaction delete
and restart-safe persistence.
"""

import asyncio
import datetime
import io
import logging
import random
import time
from typing import Optional

import discord

import botlog
import config
import dailylog
import espn
import parlaytracker
import pendingdelete
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.proptracker")

# While hibernating pre-game, how often to re-check whether the player has
# shown up in ESPN's boxscore yet (lineups typically post 1-3 hours before
# first pitch/tip-off) - keeps a "?" team name from sitting unrefreshed for
# however many hours are left until kickoff.
_LINEUP_CHECK_INTERVAL_SECONDS = 30 * 60

# Tolerate this many consecutive "event not found"/edit-failure polls before
# giving up - guards against a transient ESPN or Discord hiccup silently
# killing tracking for something that's still very much alive.
MAX_CONSECUTIVE_MISSES = 3

TRASH_EMOJI = "🗑️"

_active_props: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, entity_id, stat_key, owner_id) - lets
# the reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}


def _stat_key_str(stat_key: tuple) -> str:
    label, discriminator = stat_key
    return f"{label}:{discriminator}"


def prop_key(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> str:
    return f"{channel_id}:{event_id}:{entity_id}:{_stat_key_str(stat_key)}"


def is_tracked(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> bool:
    return prop_key(channel_id, event_id, entity_id, stat_key) in _active_props


def list_tracked_details(channel_id: int) -> list[dict]:
    """Active prop-tracking entries for this channel, from persisted state."""
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active_props if k.startswith(prefix)}
    return [
        entry for entry in state.load_props().values()
        if prop_key(entry["channel_id"], entry["event_id"], entry["entity_id"], tuple(entry["stat_key"])) in active_keys
    ]


def register_message(message_id: int, channel_id: int, event_id, entity_id: str, stat_key: tuple, owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, entity_id, stat_key, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(
    channel_id: int, event_id, entity_id: str, stat_key: tuple, message_id: int,
    sport: str, team_id: str, photo_url: Optional[str], stat_label: str, player_name: str, owner_id: int,
    direction: Optional[str] = None, line: Optional[float] = None, known_team_name: Optional[str] = None,
):
    data = state.load_props()
    data[prop_key(channel_id, event_id, entity_id, stat_key)] = {
        "channel_id": channel_id, "event_id": event_id, "entity_id": entity_id,
        "stat_key": list(stat_key), "message_id": message_id, "sport": sport,
        "team_id": team_id, "photo_url": photo_url, "stat_label": stat_label,
        "player_name": player_name, "owner_id": owner_id,
        "direction": direction, "line": line, "known_team_name": known_team_name,
    }
    state.save_props(data)


def _forget(channel_id: int, event_id, entity_id: str, stat_key: tuple):
    data = state.load_props()
    data.pop(prop_key(channel_id, event_id, entity_id, stat_key), None)
    state.save_props(data)


def stop_tracking(channel_id: int, event_id, entity_id: str, stat_key: tuple) -> bool:
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    task = _active_props.pop(key, None)
    _forget(channel_id, event_id, entity_id, stat_key)
    dailylog.record_result(channel_id, "proptracker", key, "void")
    for message_id, (c_id, e_id, ent_id, s_key, _owner) in list(_message_owners.items()):
        if c_id == channel_id and e_id == event_id and ent_id == entity_id and s_key == stat_key:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _fmt_value(v) -> str:
    return "-" if v is None else str(v)


_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "push": "➖ Push", "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {"won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>", "void": "<:cashback:1533844020839841832>"}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via ESPN" if message_id else "Scorebox • data via ESPN"


async def build_embed(
    player_name: str,
    entity_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_label: str,
    current_value,
    is_home,
    team: Optional[dict],
    event: dict,
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
    force_result: Optional[str] = None,
    message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """
    Returns (embed, file). Sport/tournament goes in the embed author line and
    matchup + status/kickoff-time in the description - both outside the
    image, same placement as /track's author/description.

    direction/line are only set for auto-tracked picks (not manual
    /playerprops usage, which has no line to grade against) - once the event
    finishes, they're compared against the final stat value to show a
    Won/Lost/Push badge in the embed title.

    force_result overrides the color/title as if this were already graded
    that way, regardless of the event's actual live status - used only by
    _track_loop's own postponed-past-its-grace-window branch, where the
    event itself is still just sitting "postponed" in ESPN's own status but
    the pick is being voided anyway.

    known_team_name is the player's roster team from the original ESPN
    player search (espn.find_player), independent of this specific event's
    boxscore - used as a fallback so the card shows a real team name
    immediately instead of "?" for however many hours it takes ESPN to
    publish this event's lineups (team can't change between search and event
    lookup within the same tracking session).
    """
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {}).get("type", {})
    # espn.is_finished() is the single source of truth for "finished", since
    # a naive state=="post" check also matches postponed/canceled games
    # (confirmed live) - those get their own "postponed" status instead,
    # rather than showing a false "Final" badge.
    if espn.is_finished(event):
        status_type = "finished"
    elif espn.is_postponed(event):
        status_type = "postponed"
    elif status.get("state") == "in":
        status_type = "inprogress"
    else:
        status_type = "notstarted"

    result = None
    if status_type == "finished" and direction is not None and line is not None:
        result = espn.grade_over_under(current_value, direction, line)
        if result is None:
            # The event finished but the player never produced a usable
            # value for this stat (DNP, coach's decision, never appeared in
            # the boxscore group, etc.) - the pick can never be graded, so
            # void it rather than silently sitting there forever looking
            # like a plain, unresolved "Final".
            result = "void"

    early_win = False
    if not result and status_type == "inprogress" and direction == "over" and line is not None:
        # A counting stat (hits, runs, yards, etc.) only ever climbs during a
        # game - once an Over line is already cleared, it can't un-clear, so
        # it's safe to tag the pick a win before the match actually ends.
        # Unders are deliberately excluded - the value could still climb past
        # the line later, so an early "Won" tag there could turn out wrong.
        # Exact-equal isn't tagged either - that's still genuinely undecided
        # (could become a push or an Over by the final value).
        try:
            if current_value is not None and float(current_value) > line:
                early_win = True
        except (TypeError, ValueError):
            pass

    if force_result:
        color_status = force_result
    elif status_type == "postponed":
        # Not voided the instant ESPN marks it postponed - _track_loop
        # gives it up to config.POSTPONED_VOID_HOURS to publish a new
        # schedule before calling it dead (force_result="void" once that
        # runs out). Shown the same blue as a not-yet-started pick in the
        # meantime, since that's genuinely what a postponed event is.
        color_status = "notstarted"
    elif result:
        color_status = result
    elif early_win:
        color_status = "won"
    elif status_type in ("notstarted", "finished"):
        color_status = status_type
    else:
        color_status = "inprogress"

    competitors = comp.get("competitors", [])
    home_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "home"), "?")
    away_name = next((c.get("team", {}).get("displayName", "?") for c in competitors if c.get("homeAway") == "away"), "?")
    matchup = f"{home_name} v {away_name}"

    team_name = (team or {}).get("displayName") or known_team_name or "?"

    sport_label = espn.SPORT_DISPLAY_LABELS.get(sport, sport.title())
    league_name = ((event.get("header", {}).get("league") or {}).get("name")) or sport_label
    sport_tournament = f"{sport_label} • {league_name}" if league_name != sport_label else sport_label

    # The pick's own line (e.g. "Chris Sale Over 6.5 Strikeouts") stays
    # visible below the matchup for the card's whole lifetime, same as
    # moneyline cards showing "<Team> ML" - without it there's no way to
    # tell at a glance what the current value needs to beat. Live/final
    # status is drawn inside the image as a pill instead (see
    # scoreimage.render_player_card), same treatment as /track's cards.
    description_lines = [matchup]
    if direction is not None and line is not None:
        description_lines.append(f"{player_name} {direction.title()} {line:g} {stat_label}")
    if status_type == "notstarted" and comp.get("date"):
        try:
            kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
            description_lines.append(f"<t:{kickoff}:f>")
        except ValueError:
            pass
    description = "\n".join(description_lines)

    period_text = "" if status_type == "notstarted" else espn.match_status_text(event, sport)
    image_bytes = await asyncio.to_thread(
        scoreimage.render_player_card,
        team_name, photo_url, player_name, stat_label, _fmt_value(current_value), color_status, period_text,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if force_result == "void":
        embed.title = _RESULT_TITLES["void"]
    elif result:
        embed.title = _RESULT_TITLES[result]
    elif early_win:
        embed.title = _RESULT_TITLES["won"]
    embed.set_author(name=sport_tournament)
    embed.description = description
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(
    message: discord.Message,
    channel_id: int,
    event_id,
    entity_id: str,
    team_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_key: tuple,
    stat_label: str,
    player_name: str,
    owner_id: int,
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    # When this event was first observed postponed (wall-clock epoch
    # seconds, not the monotonic deadline above) - loaded back from
    # persisted state on resume so a bot restart doesn't reset the grace
    # clock. None means either never postponed, or postponed and already
    # cleared (a new schedule showed up).
    postponed_since: Optional[float] = state.load_props().get(key, {}).get("postponed_since")

    consecutive_misses = 0
    consecutive_edit_failures = 0

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
            log.warning("Failed to repost final prop tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit prop tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final prop tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old prop tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up():
        """Called on every path where this tracker gives up without ever
        reaching a real result (event never found again, Discord edits
        failing repeatedly, MAX_TRACK_HOURS exhausted) - reports the leg as
        Voided to its parlay group instead of leaving the summary card
        frozen on whatever pending detail it last reported, forever, once
        this task quietly stops polling."""
        dailylog.record_result(channel_id, "proptracker", key, "void")
        group_ids = parlaytracker.groups_for_leg(channel_id, "proptracker", key)
        if not group_ids:
            return
        pick_desc = (
            f"{player_name} {direction.title()} {line:g} {stat_label}" if direction is not None and line is not None
            else player_name
        )
        if event:
            void_comp = (event.get("header", {}).get("competitions") or [{}])[0]
            void_competitors = void_comp.get("competitors", [])
            void_home = next((c.get("team", {}).get("displayName", "?") for c in void_competitors if c.get("homeAway") == "home"), "?")
            void_away = next((c.get("team", {}).get("displayName", "?") for c in void_competitors if c.get("homeAway") == "away"), "?")
            matchup = f"{void_home} v {void_away}"
        else:
            matchup = f"Event `{event_id}`"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "proptracker", key, f"{matchup} - {pick_desc}", "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    event = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(espn.get_event, sport, event_id)

            # A notstarted event's stats can't change before it starts, so
            # hibernate instead of polling every cycle. Wakes once per
            # Eastern-time day boundary and once more just before it starts -
            # but each individual sleep is capped at _LINEUP_CHECK_INTERVAL_
            # SECONDS so a card whose player hasn't appeared in ESPN's
            # boxscore yet (lineups aren't posted until a couple hours before
            # first pitch - confirmed live the boxscore's athlete lists are
            # empty/missing that far out) gets re-checked periodically and its
            # team name refreshed in place, instead of showing "?" for
            # however many hours are left until kickoff.
            hibernated = False
            team_shown = False
            while event and not espn.is_finished(event):
                status = (event.get("header", {}).get("competitions") or [{}])[0].get("status", {})
                if status.get("type", {}).get("state") != "pre":
                    break
                start = (event.get("header", {}).get("competitions") or [{}])[0].get("date")
                if not start:
                    break
                try:
                    kickoff = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    break
                seconds_until_start = kickoff - time.time()
                if seconds_until_start <= 90:
                    break
                wake_at = min(kickoff - 60, scores365.next_eastern_midnight_epoch(time.time()))
                hibernate_for = min(wake_at - time.time(), _LINEUP_CHECK_INTERVAL_SECONDS)
                deadline += hibernate_for
                hibernated = True

                # Report in as NOT STARTED right before going to sleep -
                # reporting only once hibernation ends (right before
                # kickoff) is too late: a leg with a kickoff hours away
                # would still never appear on its parlay's summary card
                # until it was basically already live anyway.
                dailylog.touch(channel_id, "proptracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "proptracker", key)
                if group_ids:
                    pre_comp = (event.get("header", {}).get("competitions") or [{}])[0]
                    pre_competitors = pre_comp.get("competitors", [])
                    pre_home = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "home"), "?")
                    pre_away = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "away"), "?")
                    pre_pick = (
                        f"{player_name} {direction.title()} {line:g} {stat_label}"
                        if direction is not None and line is not None else player_name
                    )
                    pre_label = f"{pre_home} v {pre_away} - {pre_pick}"
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "proptracker", key, pre_label,
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(espn.get_event, sport, event_id)

                if not team_shown and event:
                    current_value, is_home, team = await asyncio.to_thread(
                        espn.get_stat_value, event, entity_id, stat_key
                    )
                    if team is not None:
                        team_shown = True
                        refreshed_embed, refreshed_file = await build_embed(
                            player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team,
                            event, direction, line, known_team_name, message_id=message.id,
                        )
                        try:
                            await throttle.run(
                                channel_id, lambda: message.edit(embed=refreshed_embed, attachments=[refreshed_file])
                            )
                        except discord.HTTPException as e:
                            log.warning("Failed to refresh prop card once team resolved: %s", e)

            if not event:
                consecutive_misses += 1
                log.warning(
                    "Event %s not found on ESPN (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (prop): **{player_name}** — event `{event_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up()
                    break
                continue
            consecutive_misses = 0

            current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity_id, stat_key)
            embed, file = await build_embed(
                player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team, event,
                direction, line, known_team_name, message_id=message.id,
            )
            leg_comp = (event.get("header", {}).get("competitions") or [{}])[0]
            leg_competitors = leg_comp.get("competitors", [])
            leg_home = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "home"), "?")
            leg_away = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "away"), "?")
            leg_pick = (
                f"{player_name} {direction.title()} {line:g} {stat_label}" if direction is not None and line is not None
                else player_name
            )
            leg_label = f"{leg_home} v {leg_away} - {leg_pick}"

            if hibernated:
                # The final wake right before start - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during hibernation.
                await _repost_final(embed, file)
                _persist(
                    channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url,
                    stat_label, player_name, owner_id, direction, line, known_team_name,
                )
                continue

            if espn.is_finished(event):
                # Bump the graded result to the bottom of the channel instead
                # of editing in place - same reasoning as the pre-kickoff bump
                # above: a live event can run long enough that the original
                # card is buried under chat by the time it's graded.
                carry_emojis = await _repost_final(embed, file)

                result = None
                if direction is not None and line is not None:
                    result = espn.grade_over_under(current_value, direction, line)
                    if result is None:
                        # Player never produced a usable value for this stat
                        # (DNP, etc.) - same reasoning as build_embed's.
                        result = "void"
                        botlog.event(f"➖ Voided (prop, player didn't record a usable value): **{player_name}** in <#{channel_id}>")
                    reaction = _RESULT_REACTIONS.get(result)
                    if reaction:
                        try:
                            await message.add_reaction(reaction)
                        except discord.HTTPException as e:
                            log.warning("Failed to add result reaction: %s", e)
                if result:
                    dailylog.record_result(channel_id, "proptracker", key, result)
                    group_ids = parlaytracker.groups_for_leg(channel_id, "proptracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "proptracker", key, leg_label, result, group_ids,
                    )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            if espn.is_postponed(event):
                # Not voided the instant ESPN marks it postponed - a rain
                # delay/short reschedule shouldn't torch the pick immediately.
                # Given up to config.POSTPONED_VOID_HOURS (wall-clock, since
                # this can easily span a bot restart) to either publish a new
                # schedule (is_postponed simply goes back to False on its
                # own, no special handling needed - normal tracking, incl.
                # hibernation toward the new kickoff, just resumes) or
                # confirm it's genuinely dead.
                now = time.time()
                if postponed_since is None:
                    postponed_since = now
                    data = state.load_props()
                    if key in data:
                        data[key]["postponed_since"] = postponed_since
                        state.save_props(data)
                    botlog.event(
                        f"⏸️ Postponed - waiting up to {config.POSTPONED_VOID_HOURS}h for a new schedule "
                        f"before voiding: **{player_name}** in <#{channel_id}>"
                    )
                grace_deadline = postponed_since + config.POSTPONED_VOID_HOURS * 3600
                if now >= grace_deadline:
                    void_embed, void_file = await build_embed(
                        player_name, entity_id, photo_url, sport, stat_label, current_value, is_home, team, event,
                        direction, line, known_team_name, force_result="void", message_id=message.id,
                    )
                    await _repost_final(void_embed, void_file)
                    try:
                        await message.add_reaction(_RESULT_REACTIONS["void"])
                    except discord.HTTPException as e:
                        log.warning("Failed to add void reaction: %s", e)
                    dailylog.record_result(channel_id, "proptracker", key, "void")
                    group_ids = parlaytracker.groups_for_leg(channel_id, "proptracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "proptracker", key, leg_label, "void", group_ids,
                    )
                    pendingdelete.start(channel_id, message, void_embed.description or "")
                    botlog.event(
                        f"➖ Voided (prop, postponed {config.POSTPONED_VOID_HOURS}h+ with no new schedule): "
                        f"**{player_name}** in <#{channel_id}>"
                    )
                    break
                # Still inside the grace window - keep this pick's overall
                # deadline alive for the rest of it (otherwise the generic
                # MAX_TRACK_HOURS timeout would cut the wait short and give
                # up on the standalone card well before the full window),
                # then fall through to the normal per-poll update below so
                # the card and any parlay leg keep showing "Postponed"
                # instead of freezing.
                deadline = max(deadline, time.monotonic() + (grace_deadline - now))
                dailylog.touch(channel_id, "proptracker", key, "⏸️ Postponed - waiting for a new schedule")
            elif postponed_since is not None:
                # ESPN cleared the postponed status on its own (a new
                # schedule got published) - drop the marker and let normal
                # tracking take back over from here.
                postponed_since = None
                data = state.load_props()
                if key in data:
                    data[key].pop("postponed_since", None)
                    state.save_props(data)

            comp = (event.get("header", {}).get("competitions") or [{}])[0]
            if comp.get("status", {}).get("type", {}).get("state") == "pre":
                try:
                    kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
                    detail = f"NOT STARTED - <t:{kickoff}:f>"
                except (KeyError, ValueError):
                    detail = "NOT STARTED"
            else:
                detail = f"LIVE, {espn.match_status_text(event, sport)}"
            if not espn.is_postponed(event):
                # A still-postponed event already got a more accurate
                # "Postponed" detail logged above - don't clobber it with
                # whatever ESPN's generic status text says instead.
                dailylog.touch(channel_id, "proptracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "proptracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "proptracker", key, leg_label, detail, group_ids,
                )

            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
                consecutive_edit_failures = 0
            except discord.HTTPException as e:
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit prop tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking (prop): **{player_name}** — message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up()
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the pick ever settling - no
            # reliable result to guess at, so the standalone card is left
            # alone, but a parlay leg still gets Voided so its summary card
            # isn't stuck forever.
            botlog.event(f"⚠️ Auto-stopped tracking (prop): **{player_name}** — event `{event_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
            await _void_leg_and_give_up()
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists - an
        # unguarded exception here used to kill the task silently, freezing
        # the card forever with no Discord/botlog signal.
        log.exception("Prop tracker crashed unexpectedly for event %s (%s) in channel %s", event_id, player_name, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking (prop): **{player_name}** — event `{event_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up()
    finally:
        _active_props.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, event_id, entity_id, stat_key)


def start_tracking(
    message: discord.Message,
    channel_id: int,
    event_id,
    entity_id: str,
    team_id: str,
    photo_url: Optional[str],
    sport: str,
    stat_key: tuple,
    stat_label: str,
    player_name: str,
    owner_id: int,
    direction: Optional[str] = None,
    line: Optional[float] = None,
    known_team_name: Optional[str] = None,
    section: Optional[str] = None,
    label: Optional[str] = None,
    origin_channel_id: Optional[int] = None,
):
    key = prop_key(channel_id, event_id, entity_id, stat_key)
    if key in _active_props:
        return
    task = asyncio.create_task(
        _track_loop(
            message, channel_id, event_id, entity_id, team_id, photo_url, sport, stat_key, stat_label,
            player_name, owner_id, direction, line, known_team_name,
        )
    )
    _active_props[key] = task
    register_message(message.id, channel_id, event_id, entity_id, stat_key, owner_id)
    _persist(
        channel_id, event_id, entity_id, stat_key, message.id, sport, team_id, photo_url, stat_label,
        player_name, owner_id, direction, line, known_team_name,
    )
    dailylog.record_pick(channel_id, "proptracker", key, section, label or player_name, message.id, origin_channel_id)


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone - cleans up instead.
    """
    for entry in list(state.load_props().values()):
        try:
            stat_key = tuple(entry["stat_key"])
            channel_id, event_id, entity_id = entry["channel_id"], entry["event_id"], entry["entity_id"]
        except KeyError:
            # Belongs to an incompatible/old state schema - can't be
            # resumed, just drop it rather than blocking every other entry
            # in this same loop.
            log.warning("Dropping prop entry from an incompatible state schema: %r", entry)
            continue
        try:
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        # A single miss right here at startup used to forget the event
        # forever, permanently killing tracking on the unlucky restart that
        # lands during one transient ESPN hiccup, even though the live loop
        # itself tolerates MAX_CONSECUTIVE_MISSES misses in a row. Retrying
        # here closes that gap.
        event = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            event = await asyncio.to_thread(espn.get_event, entry["sport"], event_id)
            if event:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not event:
            _forget(channel_id, event_id, entity_id, stat_key)
            continue

        # Even an already-finished event still needs to be handed to
        # start_tracking/_track_loop - it re-registers the message (so the
        # 🗑️ reaction keeps working) and re-arms the 24h auto-delete timer,
        # which would otherwise be silently lost on every restart.
        start_tracking(
            message, channel_id, event_id, entity_id, entry["team_id"], entry.get("photo_url"), entry["sport"],
            stat_key, entry["stat_label"], entry["player_name"], entry.get("owner_id"),
            entry.get("direction"), entry.get("line"), entry.get("known_team_name"),
        )
        log.info("Resumed prop tracking for %s in channel %s", entry["player_name"], channel_id)

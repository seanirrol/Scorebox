#!/usr/bin/env python3
"""
Manages background tasks for YRFI/NRFI ("Yes/No Runs First Inning") picks,
plus the general "1st Inning Total Runs Over/Under N" market (pick_type
"INNING1_TOTAL_OVER"/"INNING1_TOTAL_UNDER", with a `line`) - YRFI/NRFI is
just that same market's 0.5 line, worded differently (see picks.py's
_parse_inning_run_total). Both settle as soon as the 1st inning is fully
complete, not when the whole game finishes, so they don't share
tracker.py/proptracker.py's wait-for-the-full-game design. Backed by ESPN's
per-inning linescores (see espn.get_first_inning_breakdown) - MLB only,
first-inning scoring isn't a bet type tracked here for other sports.

Mirrors tracker.py/proptracker.py otherwise: hibernation before kickoff
(a notstarted game's linescore can't change), 🗑️-reaction delete, and
restart-safe persistence.
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

log = logging.getLogger("scorebox.inningtracker")

MAX_CONSECUTIVE_MISSES = 3

MAX_CONSECUTIVE_RATE_LIMIT_FAILURES = 20  # separate, more generous threshold for a 429 on the edit itself - see tracker.py
TRASH_EMOJI = "🗑️"

_active: dict[str, asyncio.Task] = {}

# message_id -> (channel_id, event_id, pick_type, owner_id) - lets the
# reaction-based delete handler in bot.py look up who can delete a message.
_message_owners: dict[int, tuple] = {}

_RESULT_TITLES = {
    "won": "<:winmark:1532115635071488221> Pick Won", "lost": "<:lossmark:1532115600162422894> Pick Lost",
    "void": "<:cashback:1533844020839841832> Pick Voided",
}
_RESULT_REACTIONS = {
    "won": "<:winmark:1532115635071488221>", "lost": "<:lossmark:1532115600162422894>",
    "void": "<:cashback:1533844020839841832>",
}

# Reactions the bot itself ever adds - excluded when carrying reactions
# forward across a repost (see _repost_final) so a manually-added marker
# (e.g. tagging a card as part of a parlay) isn't confused for one of these.
_SERVICE_EMOJIS = {TRASH_EMOJI, *_RESULT_REACTIONS.values()}

_PICK_LABELS = {"YRFI": "Yes Runs 1st Inning", "NRFI": "No Runs 1st Inning"}


def _pick_label(pick_type: str, line: Optional[float] = None) -> str:
    if pick_type in _PICK_LABELS:
        return _PICK_LABELS[pick_type]
    if pick_type == "INNING1_TOTAL_OVER":
        return f"Over {line:g} 1st Inning Runs"
    if pick_type == "INNING1_TOTAL_UNDER":
        return f"Under {line:g} 1st Inning Runs"
    return pick_type


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via ESPN" if message_id else "Scorebox • data via ESPN"


def track_key(channel_id: int, event_id, pick_type: str, line: Optional[float] = None) -> str:
    # Distinct lines on the same event/pick_type (e.g. Over 1.5 vs Over 2.5
    # 1st Inning Runs) need their own key - YRFI/NRFI never pass a line at
    # all (fixed at 0.5, see module docstring), so the suffix stays empty
    # for those, unchanged from before this market existed.
    suffix = f":{line:g}" if line is not None else ""
    return f"{channel_id}:{event_id}:{pick_type}{suffix}"


def is_tracked(channel_id: int, event_id, pick_type: str, line: Optional[float] = None) -> bool:
    return track_key(channel_id, event_id, pick_type, line) in _active


def list_tracked_details(channel_id: int) -> list[dict]:
    prefix = f"{channel_id}:"
    active_keys = {k for k in _active if k.startswith(prefix)}
    return [
        entry for entry in state.load_innings().values()
        if track_key(entry["channel_id"], entry["event_id"], entry["pick_type"], entry.get("line")) in active_keys
    ]


def register_message(message_id: int, channel_id: int, event_id, pick_type: str, line: Optional[float], owner_id: int):
    """Lets bot.py's 🗑️-reaction handler know who's allowed to delete this message."""
    _message_owners[message_id] = (channel_id, event_id, pick_type, line, owner_id)


def get_message_owner(message_id: int) -> Optional[tuple]:
    return _message_owners.get(message_id)


def unregister_message(message_id: int):
    _message_owners.pop(message_id, None)


def _persist(channel_id: int, event_id, pick_type: str, line: Optional[float], message_id: int, team_id: str, owner_id: int):
    data = state.load_innings()
    data[track_key(channel_id, event_id, pick_type, line)] = {
        "channel_id": channel_id, "event_id": event_id, "pick_type": pick_type, "line": line,
        "message_id": message_id, "team_id": team_id, "owner_id": owner_id,
    }
    state.save_innings(data)


def _forget(channel_id: int, event_id, pick_type: str, line: Optional[float] = None):
    data = state.load_innings()
    data.pop(track_key(channel_id, event_id, pick_type, line), None)
    state.save_innings(data)


def _forget_key(key: str):
    """Same cleanup as _forget, but pops the exact persisted dict key
    directly instead of reconstructing it via track_key() - see
    tracker.py's identical _forget_key for why this matters."""
    data = state.load_innings()
    data.pop(key, None)
    state.save_innings(data)


def stop_tracking(channel_id: int, event_id, pick_type: str, line: Optional[float] = None) -> bool:
    key = track_key(channel_id, event_id, pick_type, line)
    task = _active.pop(key, None)
    _forget(channel_id, event_id, pick_type, line)
    dailylog.record_result(channel_id, "inningtracker", key, "void", "Manually untracked")
    for message_id, (c_id, e_id, p_type, ln, _owner) in list(_message_owners.items()):
        if c_id == channel_id and e_id == event_id and p_type == pick_type and ln == line:
            _message_owners.pop(message_id, None)
    if task:
        task.cancel()
        return True
    return False


def _grade(pick_type: str, line: Optional[float], total_runs: int) -> str:
    if pick_type == "INNING1_TOTAL_OVER":
        return espn.grade_over_under(total_runs, "over", line)
    if pick_type == "INNING1_TOTAL_UNDER":
        return espn.grade_over_under(total_runs, "under", line)
    return espn.grade_yrfi(total_runs, pick_type)


async def build_embed(
    event: dict, pick_type: str, line: Optional[float] = None,
    force_result: Optional[str] = None, message_id: Optional[int] = None,
) -> tuple[discord.Embed, discord.File]:
    """force_result overrides the color/title as if this were already graded
    that way, regardless of the event's actual live status - used only by
    _track_loop's own postponed-past-its-grace-window branch."""
    comp = (event.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {})
    state_name = status.get("type", {}).get("state")
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home_name = home.get("team", {}).get("displayName", "?")
    away_name = away.get("team", {}).get("displayName", "?")
    home_logo = espn.team_logo_url(home.get("team", {}))
    away_logo = espn.team_logo_url(away.get("team", {}))

    breakdown = espn.get_first_inning_breakdown(event)
    decided = breakdown is not None
    result = _grade(pick_type, line, sum(breakdown)) if decided else None
    postponed = not decided and espn.is_postponed(event)

    if force_result:
        color_status = force_result
    elif postponed:
        # Not voided the instant ESPN marks it postponed - _track_loop gives
        # it up to config.POSTPONED_VOID_HOURS to publish a new schedule
        # before calling it dead (force_result="void" once that runs out).
        # Shown the same blue as a not-yet-started pick in the meantime.
        color_status = "notstarted"
    elif result:
        color_status = result
    elif decided:
        color_status = "finished"
    elif state_name == "in":
        color_status = "inprogress"
    else:
        color_status = "notstarted"

    league_name = ((event.get("header", {}).get("league") or {}).get("name")) or "MLB"
    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status])
    if force_result == "void":
        embed.title = _RESULT_TITLES["void"]
    elif result:
        embed.title = _RESULT_TITLES[result]
    embed.set_author(name=f"MLB • {league_name}" if league_name != "MLB" else "MLB")

    description_lines = [f"{away_name} v {home_name}", _pick_label(pick_type, line)]
    if state_name == "pre" and comp.get("date"):
        try:
            kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
            description_lines.append(f"<t:{kickoff}:f>")
        except ValueError:
            pass
    embed.description = "\n".join(description_lines)

    if decided:
        period_text = "1st Inning Final"
    elif postponed:
        period_text = espn.match_status_text(event, "baseball")
    elif state_name == "in":
        period_text = status.get("type", {}).get("detail") or "Live"
    else:
        period_text = ""

    home_cols = [str(breakdown[0])] if decided else ["-"]
    away_cols = [str(breakdown[1])] if decided else ["-"]

    image_bytes = await asyncio.to_thread(
        scoreimage.render_score_card,
        home_name, away_name, home_logo, away_logo, home_cols, away_cols, period_text, color_status,
    )
    file = discord.File(io.BytesIO(image_bytes), filename="score.png")
    embed.set_image(url="attachment://score.png")
    embed.set_footer(text=_footer_text(message_id))
    embed.timestamp = discord.utils.utcnow()
    return embed, file


async def _track_loop(message: discord.Message, channel_id: int, event_id, pick_type: str, line: Optional[float], team_id: str, owner_id: int):
    key = track_key(channel_id, event_id, pick_type, line)
    deadline = time.monotonic() + config.MAX_TRACK_HOURS * 3600

    # When this event was first observed postponed (wall-clock epoch
    # seconds, not the monotonic deadline above) - loaded back from
    # persisted state on resume so a bot restart doesn't reset the grace
    # clock. None means either never postponed, or postponed and already
    # cleared (a new schedule showed up).
    postponed_since: Optional[float] = state.load_innings().get(key, {}).get("postponed_since")

    consecutive_misses = 0
    consecutive_edit_failures = 0
    consecutive_rate_limit_failures = 0

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
            log.warning("Failed to repost final inning tracking message: %s", e)
            try:
                await throttle.run(channel_id, lambda: message.edit(embed=embed, attachments=[file]))
            except discord.HTTPException as e2:
                log.warning("Failed to edit inning tracking message as a fallback: %s", e2)
            return carry_emojis
        try:
            await new_message.add_reaction(TRASH_EMOJI)
        except discord.HTTPException as e:
            log.warning("Failed to react to reposted final inning tracking message: %s", e)
        for emoji in carry_emojis:
            try:
                await new_message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.warning("Failed to carry forward reaction %s: %s", emoji, e)
        old_message = message
        message = new_message
        _message_owners.pop(old_message.id, None)
        register_message(message.id, channel_id, event_id, pick_type, line, owner_id)
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old inning tracking message after final repost: %s", e)
        return carry_emojis

    async def _void_leg_and_give_up(reason: str):
        """Called on every path where this tracker gives up without ever
        reaching a real result (event never found again, Discord edits
        failing repeatedly, MAX_TRACK_HOURS exhausted) - reports the leg as
        Voided to its parlay group instead of leaving the summary card
        frozen on whatever pending detail it last reported, forever, once
        this task quietly stops polling. reason is shown in /summary - see
        tracker.py's identical helper for why this matters."""
        dailylog.record_result(channel_id, "inningtracker", key, "void", reason)
        group_ids = parlaytracker.groups_for_leg(channel_id, "inningtracker", key)
        if not group_ids:
            return
        if event:
            void_competitors = (event.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])
            void_home = next((c.get("team", {}).get("displayName", "?") for c in void_competitors if c.get("homeAway") == "home"), "?")
            void_away = next((c.get("team", {}).get("displayName", "?") for c in void_competitors if c.get("homeAway") == "away"), "?")
            matchup = f"{void_away} v {void_home}"
        else:
            matchup = f"Event `{event_id}`"
        await parlaytracker.handle_leg_result(
            message.channel, channel_id, message, "inningtracker", key, f"{matchup} - {_pick_label(pick_type, line)}", "void", group_ids,
        )

    await asyncio.sleep(random.uniform(0, config.UPDATE_INTERVAL_SECONDS))
    event = None
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(config.UPDATE_INTERVAL_SECONDS)

            event = await asyncio.to_thread(espn.get_event, "baseball", event_id)

            # A notstarted event's linescore can't change before it starts,
            # so hibernate instead of polling every cycle - same pattern as
            # tracker.py/proptracker.py. Wakes once per Eastern-time day
            # boundary and once more just before it starts.
            hibernated = False
            while event:
                comp = (event.get("header", {}).get("competitions") or [{}])[0]
                if comp.get("status", {}).get("type", {}).get("state") != "pre":
                    break
                start = comp.get("date")
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
                hibernate_for = wake_at - time.time()
                deadline += hibernate_for
                hibernated = True

                # Report in as NOT STARTED right before going to sleep for
                # potentially hours - reporting only once hibernation ends
                # (right before kickoff) is too late: a leg with a kickoff
                # hours away would still never appear on its parlay's
                # summary card until it was basically already live anyway.
                dailylog.touch(channel_id, "inningtracker", key, f"NOT STARTED - <t:{int(kickoff)}:f>")
                group_ids = parlaytracker.groups_for_leg(channel_id, "inningtracker", key)
                if group_ids:
                    pre_competitors = comp.get("competitors", [])
                    pre_home = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "home"), "?")
                    pre_away = next((c.get("team", {}).get("displayName", "?") for c in pre_competitors if c.get("homeAway") == "away"), "?")
                    await parlaytracker.report_leg_progress(
                        message.channel, channel_id, message, "inningtracker", key,
                        f"{pre_away} v {pre_home} - {_pick_label(pick_type, line)}",
                        f"NOT STARTED - <t:{int(kickoff)}:f>", group_ids,
                    )

                log.info("Event %s not starting soon; hibernating %.0fs", event_id, hibernate_for)
                await asyncio.sleep(hibernate_for)
                event = await asyncio.to_thread(espn.get_event, "baseball", event_id)

            if not event:
                consecutive_misses += 1
                log.warning(
                    "Event %s not found on ESPN (miss %d/%d)",
                    event_id, consecutive_misses, MAX_CONSECUTIVE_MISSES,
                )
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` not found {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Event not found on ESPN")
                    break
                continue
            consecutive_misses = 0

            embed, file = await build_embed(event, pick_type, line, message_id=message.id)
            leg_competitors = (event.get("header", {}).get("competitions") or [{}])[0].get("competitors", [])
            leg_home = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "home"), "?")
            leg_away = next((c.get("team", {}).get("displayName", "?") for c in leg_competitors if c.get("homeAway") == "away"), "?")
            leg_label = f"{leg_away} v {leg_home} - {_pick_label(pick_type, line)}"

            if hibernated:
                # The final wake right before kickoff - bump the card to the
                # bottom of the channel instead of editing a message that may
                # be buried under whatever chat happened during the (possibly
                # long) hibernation. Same treatment as tracker.py/proptracker.py.
                await _repost_final(embed, file)
                _persist(channel_id, event_id, pick_type, line, message.id, team_id, owner_id)
                continue

            breakdown = espn.get_first_inning_breakdown(event)
            if breakdown is not None:
                # Bump the graded result to the bottom of the channel instead
                # of editing in place - same reasoning as the pre-kickoff bump
                # above: a live game can run long enough that the original
                # card is buried under chat by the time the 1st inning wraps.
                await _repost_final(embed, file)

                result = _grade(pick_type, line, sum(breakdown))
                reaction = _RESULT_REACTIONS.get(result)
                if reaction:
                    try:
                        await message.add_reaction(reaction)
                    except discord.HTTPException as e:
                        log.warning("Failed to add result reaction: %s", e)
                dailylog.record_result(channel_id, "inningtracker", key, result)
                group_ids = parlaytracker.groups_for_leg(channel_id, "inningtracker", key)
                await parlaytracker.handle_leg_result(
                    message.channel, channel_id, message, "inningtracker", key, leg_label, result, group_ids,
                )
                pendingdelete.start(channel_id, message, embed.description or "")
                break

            if espn.is_postponed(event):
                # Not voided the instant ESPN marks it postponed - a rain
                # delay/short reschedule shouldn't torch the pick
                # immediately. Given up to config.POSTPONED_VOID_HOURS
                # (wall-clock, since this can easily span a bot restart) to
                # either publish a new schedule (is_postponed simply goes
                # back to False on its own - normal tracking, incl.
                # hibernation toward the new kickoff, just resumes) or
                # confirm it's genuinely dead.
                now = time.time()
                if postponed_since is None:
                    postponed_since = now
                    data = state.load_innings()
                    if key in data:
                        data[key]["postponed_since"] = postponed_since
                        state.save_innings(data)
                    botlog.event(
                        f"⏸️ Postponed - waiting up to {config.POSTPONED_VOID_HOURS}h for a new schedule "
                        f"before voiding ({pick_type}): event `{event_id}` in <#{channel_id}>"
                    )
                grace_deadline = postponed_since + config.POSTPONED_VOID_HOURS * 3600
                if now >= grace_deadline:
                    void_embed, void_file = await build_embed(event, pick_type, line, force_result="void", message_id=message.id)
                    await _repost_final(void_embed, void_file)
                    try:
                        await message.add_reaction(_RESULT_REACTIONS["void"])
                    except discord.HTTPException as e:
                        log.warning("Failed to add void reaction: %s", e)
                    dailylog.record_result(channel_id, "inningtracker", key, "void", "Postponed, no new schedule published")
                    group_ids = parlaytracker.groups_for_leg(channel_id, "inningtracker", key)
                    await parlaytracker.handle_leg_result(
                        message.channel, channel_id, message, "inningtracker", key, leg_label, "void", group_ids,
                    )
                    pendingdelete.start(channel_id, message, void_embed.description or "")
                    botlog.event(
                        f"➖ Voided ({pick_type}, postponed {config.POSTPONED_VOID_HOURS}h+ with no new schedule): "
                        f"event `{event_id}` in <#{channel_id}>"
                    )
                    break
                # Still inside the grace window - keep this pick's overall
                # deadline alive for the rest of it, then fall through to
                # the normal per-poll update below so the card and any
                # parlay leg keep showing "Postponed" instead of freezing.
                deadline = max(deadline, time.monotonic() + (grace_deadline - now))
                dailylog.touch(channel_id, "inningtracker", key, "⏸️ Postponed - waiting for a new schedule")
            elif postponed_since is not None:
                # ESPN cleared the postponed status on its own (a new
                # schedule got published) - drop the marker and let normal
                # tracking take back over from here.
                postponed_since = None
                data = state.load_innings()
                if key in data:
                    data[key].pop("postponed_since", None)
                    state.save_innings(data)

            comp = (event.get("header", {}).get("competitions") or [{}])[0]
            if comp.get("status", {}).get("type", {}).get("state") == "pre":
                try:
                    kickoff = int(datetime.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp())
                    detail = f"NOT STARTED - <t:{kickoff}:f>"
                except (KeyError, ValueError):
                    detail = "NOT STARTED"
            else:
                detail = f"LIVE, {comp.get('status', {}).get('type', {}).get('detail') or 'Live'}"
            if not espn.is_postponed(event):
                dailylog.touch(channel_id, "inningtracker", key, detail)
            group_ids = parlaytracker.groups_for_leg(channel_id, "inningtracker", key)
            if group_ids:
                await parlaytracker.report_leg_progress(
                    message.channel, channel_id, message, "inningtracker", key, leg_label, detail, group_ids,
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
                        "Failed to edit inning tracking message, rate limited (failure %d/%d): %s",
                        consecutive_rate_limit_failures, MAX_CONSECUTIVE_RATE_LIMIT_FAILURES, e,
                    )
                    if consecutive_rate_limit_failures >= MAX_CONSECUTIVE_RATE_LIMIT_FAILURES:
                        botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` message edit rate-limited {MAX_CONSECUTIVE_RATE_LIMIT_FAILURES}x in a row, in <#{channel_id}>")
                        await _void_leg_and_give_up("Message edit rate-limited repeatedly")
                        break
                    continue
                consecutive_edit_failures += 1
                log.warning(
                    "Failed to edit inning tracking message (failure %d/%d): %s",
                    consecutive_edit_failures, MAX_CONSECUTIVE_MISSES, e,
                )
                if consecutive_edit_failures >= MAX_CONSECUTIVE_MISSES:
                    botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` message edit failed {MAX_CONSECUTIVE_MISSES}x in a row, in <#{channel_id}>")
                    await _void_leg_and_give_up("Message edit failed repeatedly")
                    break
                continue
        else:
            # MAX_TRACK_HOURS ran out without the pick ever settling - no
            # reliable result to guess at, so the standalone card is left
            # alone, but a parlay leg still gets Voided so its summary card
            # isn't stuck forever.
            botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` never settled within {config.MAX_TRACK_HOURS}h, in <#{channel_id}>")
            await _void_leg_and_give_up("Timed out without settling")
    except asyncio.CancelledError:
        raise
    except Exception:
        # See tracker.py's identical handler for why this exists.
        log.exception("Inning tracker crashed unexpectedly for event %s (%s) in channel %s", event_id, pick_type, channel_id)
        botlog.event(f"⚠️ Auto-stopped tracking ({pick_type}): event `{event_id}` crashed unexpectedly (see server logs), in <#{channel_id}>")
        await _void_leg_and_give_up("Crashed unexpectedly")
    finally:
        _active.pop(key, None)
        _message_owners.pop(message.id, None)
        _forget(channel_id, event_id, pick_type)


def start_tracking(
    message: discord.Message, channel_id: int, event_id, pick_type: str, team_id: str, owner_id: int,
    line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    game_date: Optional[str] = None,
):
    key = track_key(channel_id, event_id, pick_type, line)
    if key in _active:
        return
    task = asyncio.create_task(_track_loop(message, channel_id, event_id, pick_type, line, team_id, owner_id))
    _active[key] = task
    register_message(message.id, channel_id, event_id, pick_type, line, owner_id)
    _persist(channel_id, event_id, pick_type, line, message.id, team_id, owner_id)
    dailylog.record_pick(
        channel_id, "inningtracker", key, section, label or _pick_label(pick_type, line), message.id, origin_channel_id,
        sport="MLB", tournament="MLB", game_date=game_date,
    )


async def resume_all(client: discord.Client):
    """
    Called once from on_ready. Reads whatever was still active when the bot
    last stopped and either picks the tracking loop back up on the same
    message, or - if the message/channel/event is gone - cleans up instead.
    """
    for key, entry in list(state.load_innings().items()):
        try:
            channel_id, event_id, pick_type = entry["channel_id"], entry["event_id"], entry["pick_type"]
        except KeyError:
            log.warning("Dropping inning entry from an incompatible state schema: %r", entry)
            continue
        line = entry.get("line")
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
            botlog.event(f"⚠️ Dropped on resume ({pick_type}): event `{event_id}` — message/channel no longer reachable, in <#{channel_id}>")
            _forget_key(key)
            continue

        # A single miss right here at startup used to forget the event
        # forever, permanently killing tracking on the unlucky restart that
        # lands during one transient ESPN hiccup, even though the live loop
        # itself tolerates MAX_CONSECUTIVE_MISSES misses in a row. Retrying
        # here closes that gap.
        event = None
        for attempt in range(MAX_CONSECUTIVE_MISSES):
            event = await asyncio.to_thread(espn.get_event, "baseball", event_id)
            if event:
                break
            if attempt < MAX_CONSECUTIVE_MISSES - 1:
                await asyncio.sleep(5)
        if not event:
            botlog.event(f"⚠️ Dropped on resume ({pick_type}): event `{event_id}` not found on ESPN after {MAX_CONSECUTIVE_MISSES} attempts, in <#{channel_id}>")
            _forget_key(key)
            continue

        start_tracking(message, channel_id, event_id, pick_type, entry["team_id"], entry.get("owner_id"), line)
        log.info("Resumed inning tracking for event %s in channel %s", event_id, channel_id)

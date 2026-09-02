#!/usr/bin/env python3
"""
Scorebox — a Discord bot that posts live sports scores, powered by
365scores.com's public JSON API, plus live player-prop-stat tracking powered
by Sofascore.com.

Commands:
  /score sport:<pick> team:<name>                  One-off lookup of a team's live/today match.
  /track sport:<pick> team:<name>                  Posts a live-updating embed that refreshes automatically.
  /playerprops sport:<pick> player: stat:           Tracks a player's live stat (e.g. Points, Earned Runs, Aces).
  /untrack game_id:<id[,id...]>                     Stops one or more active tracking loops in this channel.
  /tracked                                          Lists games currently being tracked in this channel.
"""

import asyncio
import datetime
import io
import logging
import re
import time
from collections import defaultdict
from typing import NamedTuple, Optional

import discord
from discord import app_commands

import botlog
import boxing
import boxingtracker
import config
import dailylog
import doublechancetracker
import espn
import espn_ufc
import esports
import esportstracker
import f5tracker
import halftracker
import htfttracker
import image_picks
import inning1tracker
import inningtotaltracker
import inningtracker
import kboproptracker
import koreabaseball
import masterparlay
import parlaytracker
import pendingdelete
import pendingauto
import pendingsoccerprops
import pendingtrack
import picks
import playerstatsfootball
import proptracker
import scores365
import settracker
import soccerpropstracker
import state
import tennispropstracker
import throttle
import tracker
import ufctracker
import performance
import winlossgraph

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorebox.bot")

intents = discord.Intents.default()
intents.message_content = True  # needed to read pick messages in config.PICKS_CHANNEL_MAP
# Needed for on_raw_reaction_add's admin check - Discord's gateway only
# includes the reacting member's roles/permissions on a MESSAGE_REACTION_ADD
# event when this privileged intent is enabled. Without it, payload.member
# is always None, so is_admin always evaluates False for every user - the
# trash-reaction delete on an owner-less auto-tracked card (owner_id=None)
# silently never worked for anyone, admin or not. Also requires "Server
# Members Intent" to be toggled ON for this bot in the Discord Developer
# Portal (Bot page, Privileged Gateway Intents) - the client fails to log
# in with PrivilegedIntentsRequired if that portal toggle is off while this
# is requested in code.
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
botlog.init(client)

SPORT_CHOICES = [
    app_commands.Choice(name="Soccer", value="soccer"),
    app_commands.Choice(name="Basketball", value="basketball"),
    app_commands.Choice(name="Tennis", value="tennis"),
    app_commands.Choice(name="Hockey", value="hockey"),
    app_commands.Choice(name="NFL (American Football)", value="nfl"),
    app_commands.Choice(name="Baseball", value="baseball"),
    app_commands.Choice(name="Volleyball", value="volleyball"),
    app_commands.Choice(name="Rugby", value="rugby"),
]

TRASH_EMOJI = "🗑️"


async def _safe_add_trash_reaction(message: discord.Message):
    """Every auto-track function's own initial 🗑️-reaction add, right after
    posting a fresh card and before calling that tracker's start_tracking -
    confirmed live, an unhandled discord.Forbidden here (a target channel
    missing the "Add Reactions" permission - error code 50013) propagated
    all the way out to _dispatch_pick's broad exception handler, which
    logged the whole pick as "not tracked" even though the card had
    already posted successfully. Worse than a missing reaction: since
    start_tracking is always the very next line, the pick was silently
    never tracked at all - a permanently orphaned card that looks like a
    normal tracked pick but never updates or grades. Swallowing the
    failure here (loud in the logs, not fatal) means a missing permission
    degrades to "no 🗑️ delete button" instead of "no tracking whatsoever"."""
    try:
        await message.add_reaction(TRASH_EMOJI)
    except discord.HTTPException as e:
        log.warning("Failed to add initial trash-reaction to %s in channel %s: %s", message.id, message.channel.id, e)
        botlog.event(f"⚠️ Couldn't add 🗑️ reaction to a new card in <#{message.channel.id}> (missing permissions?) — tracking still started: {e}")

# message_id (the picks-channel source message) -> {raw_line: card_message_id}
# for every pick successfully tracked from it - lets on_message_edit diff a
# later edit against what was originally tracked (see on_message_edit).
# In-memory only, not persisted - an edit made while the bot is offline
# can't be detected anyway (there's nothing to diff against once this
# dict is gone), same limitation as every other in-memory registry in
# this bot (_message_owners, _active_tracks, etc).
_tracked_lines: dict[int, dict[str, int]] = {}

# Picks-channel message ids on_message has already started processing -
# guards against the exact same Discord message being handed to on_message
# more than once (discord.py/the gateway can redeliver a MESSAGE_CREATE,
# e.g. around a session resume) triggering two full concurrent passes over
# the same picks. Each individual pick's own is_tracked()/is_queued() check
# only guards against a SECOND identical pick line within a single pass or
# a genuinely later repost - it does nothing against two passes racing each
# other, since neither has registered the pick as tracked yet when the
# other's check runs. Confirmed live: a 26-pick message whose later picks
# each take real seconds (sequential ESPN/365scores network lookups) got
# several of its slower picks (player props, the last game before them)
# posted 2-3x each, a few seconds apart - consistent with on_message
# running that many times concurrently for the same message, not a bug in
# any individual _auto_* function. Checked and inserted before any `await`
# in on_message so two "concurrent" deliveries can't both pass the check -
# asyncio only interleaves tasks at an await point. Same in-memory,
# never-pruned, restart-clears-it convention as _tracked_lines above.
_processed_message_ids: set[int] = set()


_masterparlay_auto_archive_started = False


async def _masterparlay_auto_archive_loop():
    """Runs forever, waking once per parlay day at its 3:00 AM Eastern
    cutoff (see masterparlay's module docstring) to archive whatever
    slip just closed - auto_archive_if_needed itself skips a day that
    was already manually published via /premiumparlay's Publish button,
    so this is purely a safety net for days nobody touched it. Also
    fires once immediately on startup (before the first sleep) so a
    restart that straddled the 3am boundary doesn't silently skip a
    day."""
    await client.wait_until_ready()
    slip_channel = client.get_channel(masterparlay.PARLAY_SLIP_CHANNEL_ID) or await client.fetch_channel(masterparlay.PARLAY_SLIP_CHANNEL_ID)
    archive_channel = client.get_channel(masterparlay.PUBLISH_CHANNEL_ID) or await client.fetch_channel(masterparlay.PUBLISH_CHANNEL_ID)
    while True:
        date_str = masterparlay.previous_parlay_day_str(time.time())
        try:
            archived = await masterparlay.auto_archive_if_needed(slip_channel, archive_channel, date_str)
            if archived:
                botlog.event(f"🎟️ Auto-archived {archived} parlay(s) for {date_str} to <#{masterparlay.PUBLISH_CHANNEL_ID}>")
        except Exception:
            log.exception("Masterparlay auto-archive failed for %s", date_str)
            botlog.event(f"⚠️ Masterparlay auto-archive failed for {date_str} (see server logs)")
        await asyncio.sleep(masterparlay.seconds_until_next_parlay_day_cutoff(time.time()))


_parlay_auto_delete_started = False
_PARLAY_AUTO_DELETE_CHECK_INTERVAL_SECONDS = 6 * 3600  # 6 hours - plenty granular against a 2-day age threshold


async def _parlay_auto_delete_loop():
    """Runs forever, sweeping every channel's /parlay groups every 6h for
    ones at least parlaytracker.AUTO_DELETE_AGE_SECONDS old (see that
    function's own docstring for why this exists and why age, not
    resolution status, is what triggers it)."""
    await client.wait_until_ready()
    while True:
        try:
            deleted = await parlaytracker.auto_delete_old_groups()
            if deleted:
                by_channel: dict[int, list[str]] = defaultdict(list)
                for channel_id, identifier in deleted:
                    by_channel[channel_id].append(identifier)
                for channel_id, identifiers in by_channel.items():
                    botlog.event(f"🎟️ Auto-deleted {len(identifiers)} stale parlay(s) in <#{channel_id}>: {', '.join(identifiers)}")
        except Exception:
            log.exception("Parlay auto-delete sweep failed")
            botlog.event("⚠️ Parlay auto-delete sweep failed (see server logs)")
        await asyncio.sleep(_PARLAY_AUTO_DELETE_CHECK_INTERVAL_SECONDS)


async def _safe_resume(name: str, coro):
    """Isolates one module's resume_all() from every other - previously a
    single exception (e.g. a KeyError from an old persisted-state schema)
    would abort on_ready entirely, silently skipping resume_all for every
    module listed after the one that failed on that restart. Confirmed live:
    settracker.py/tennispropstracker.py both needed their own try/except
    after hitting exactly this, but the other modules never got the same
    fix, so the risk was still there for any of them."""
    try:
        await coro
    except Exception:
        log.exception("%s.resume_all() failed on startup - other modules still resumed normally", name)
        botlog.event(f"⚠️ {name}.resume_all() failed on startup (see server logs) - active picks in this module may not have resumed")


@client.event
async def on_ready():
    # Isolated the same way every _safe_resume call below is - confirmed
    # live: a command definition error (e.g. a description over Discord's
    # 100-char limit) raised here, uncaught, and since this ran before
    # every resume_all call, on_ready's own top-level exception handler
    # swallowed it and returned immediately - not one tracker resumed on
    # that restart, silently, with nothing in the logs pointing at the
    # real cause beyond "Ignoring exception in on_ready".
    try:
        await tree.sync()
    except Exception:
        log.exception("tree.sync() failed on startup - slash commands may be stale, but tracking still resumes normally")
        botlog.event("⚠️ Slash command sync failed on startup (see server logs) - commands may be stale until the next successful sync")
    log.info("Logged in as %s", client.user)
    await _safe_resume("tracker", tracker.resume_all(client))
    await _safe_resume("proptracker", proptracker.resume_all(client))
    await _safe_resume("inningtracker", inningtracker.resume_all(client))
    await _safe_resume("inningtotaltracker", inningtotaltracker.resume_all(client))
    await _safe_resume("f5tracker", f5tracker.resume_all(client))
    await _safe_resume("halftracker", halftracker.resume_all(client))
    await _safe_resume("htfttracker", htfttracker.resume_all(client))
    await _safe_resume("doublechancetracker", doublechancetracker.resume_all(client))
    await _safe_resume("inning1tracker", inning1tracker.resume_all(client))
    await _safe_resume("settracker", settracker.resume_all(client))
    await _safe_resume("tennispropstracker", tennispropstracker.resume_all(client))
    await _safe_resume("soccerpropstracker", soccerpropstracker.resume_all(client))
    await _safe_resume("ufctracker", ufctracker.resume_all(client))
    await _safe_resume("boxingtracker", boxingtracker.resume_all(client))
    await _safe_resume("kboproptracker", kboproptracker.resume_all(client))
    await _safe_resume("esportstracker", esportstracker.resume_all(client))
    await _safe_resume("parlaytracker", parlaytracker.resume_all(client))
    await _safe_resume("pendingdelete", pendingdelete.resume_all(client))
    await _safe_resume("pendingsoccerprops", pendingsoccerprops.resume_all(_resolve_pending_soccer_prop))
    await _safe_resume("pendingtrack", pendingtrack.resume_all(_resolve_pending_track))
    await _safe_resume("pendingauto", pendingauto.resume_all({
        "f5": _resolve_pending_f5,
        "1h": _resolve_pending_1h,
        "ht_ft": _resolve_pending_ht_ft,
        "double_chance": _resolve_pending_double_chance,
        "tennis_market": _resolve_pending_tennis_market,
        "tennis_playerprops": _resolve_pending_tennis_playerprops,
        "inning_runs": _resolve_pending_inning_runs,
        "inning1_result": _resolve_pending_inning1_result,
        "playerprops": _resolve_pending_playerprops,
    }))

    global _masterparlay_auto_archive_started
    if not _masterparlay_auto_archive_started:
        # on_ready can fire again on a gateway reconnect, not just once at
        # startup - this guard is what keeps that from spawning a second,
        # duplicate forever-loop each time.
        _masterparlay_auto_archive_started = True
        asyncio.create_task(_masterparlay_auto_archive_loop())

    global _parlay_auto_delete_started
    if not _parlay_auto_delete_started:
        _parlay_auto_delete_started = True
        asyncio.create_task(_parlay_auto_delete_loop())


def _find_message_owner(card_message_id: int) -> Optional[tuple[str, tuple]]:
    """Looks up which tracker (if any) owns a posted score card, across
    every module - shared by the 🗑️-reaction handler and the picks-message-
    edit handler (see on_message_edit), both of which need to go from "a
    card's message id" to "which tracker's stop_tracking to call"."""
    for kind, getter in (
        ("track", tracker.get_message_owner),
        ("prop", proptracker.get_message_owner),
        ("inning", inningtracker.get_message_owner),
        ("inning_total", inningtotaltracker.get_message_owner),
        ("f5", f5tracker.get_message_owner),
        ("inning1", inning1tracker.get_message_owner),
        ("set1", settracker.get_message_owner),
        ("tennis_prop", tennispropstracker.get_message_owner),
        ("ufc", ufctracker.get_message_owner),
        ("boxing", boxingtracker.get_message_owner),
        ("kbo_prop", kboproptracker.get_message_owner),
        ("soccer_prop", soccerpropstracker.get_message_owner),
        ("esports", esportstracker.get_message_owner),
        ("htft", htfttracker.get_message_owner),
        ("double_chance", doublechancetracker.get_message_owner),
    ):
        info = getter(card_message_id)
        if info:
            return kind, info
    return None


def _stop_tracking_by_card_message(card_message_id: int) -> Optional[str]:
    """Stops whichever tracker owns this card (see _find_message_owner) and
    returns its kind, or None if no tracker owns it (already resolved,
    already untracked, etc). Doesn't touch the Discord message itself -
    callers decide separately whether to delete/edit it."""
    found = _find_message_owner(card_message_id)
    if not found:
        return None
    kind, info = found
    if kind == "track":
        channel_id, game_id, picked_team, team_total, total_direction, total_line, _ = info
        tracker.stop_tracking(channel_id, game_id, picked_team, team_total, total_direction, total_line)
    elif kind == "prop":
        channel_id, event_id, entity_id, stat_key, direction, line, _ = info
        proptracker.stop_tracking(channel_id, event_id, entity_id, stat_key, direction, line)
    elif kind == "inning":
        channel_id, event_id, pick_type, line, _ = info
        inningtracker.stop_tracking(channel_id, event_id, pick_type, line)
    elif kind == "inning_total":
        channel_id, game_id, pick_type, line, _ = info
        inningtotaltracker.stop_tracking(channel_id, game_id, pick_type, line)
    elif kind == "f5":
        channel_id, game_id, picked_team, total_direction, total_line, handicap_line, _ = info
        f5tracker.stop_tracking(channel_id, game_id, picked_team, total_direction, total_line, handicap_line)
    elif kind == "inning1":
        channel_id, game_id, _ = info
        inning1tracker.stop_tracking(channel_id, game_id)
    elif kind == "set1":
        channel_id, game_id, market, team, _ = info
        settracker.stop_tracking(channel_id, game_id, market, team)
    elif kind == "tennis_prop":
        channel_id, game_id, competitor_id, stat_name, direction, line, _ = info
        tennispropstracker.stop_tracking(channel_id, game_id, competitor_id, stat_name, direction, line)
    elif kind == "ufc":
        channel_id, competition_id, fighter_id, total_direction, total_line, _ = info
        ufctracker.stop_tracking(channel_id, competition_id, fighter_id, total_direction, total_line)
    elif kind == "boxing":
        channel_id, fight_id, fighter_id, _ = info
        boxingtracker.stop_tracking(channel_id, fight_id, fighter_id)
    elif kind == "kbo_prop":
        channel_id, pcode, stat_label, direction, line, target_date, _ = info
        kboproptracker.stop_tracking(channel_id, pcode, stat_label, direction, line, target_date)
    elif kind == "esports":
        channel_id, sport, team_a, team_b, market, _ = info
        esportstracker.stop_tracking(channel_id, sport, team_a, team_b, market)
    elif kind == "htft":
        channel_id, game_id, ht_team, ft_team, _ = info
        htfttracker.stop_tracking(channel_id, game_id, ht_team, ft_team)
    elif kind == "double_chance":
        channel_id, game_id, _ = info
        doublechancetracker.stop_tracking(channel_id, game_id)
    else:
        channel_id, game_id, member_id, stat_name, direction, line, _ = info
        soccerpropstracker.stop_tracking(channel_id, game_id, member_id, stat_name, direction, line)
    return kind


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id or str(payload.emoji) != TRASH_EMOJI:
        return

    found = _find_message_owner(payload.message_id)
    if not found:
        return
    kind, info = found
    owner_id = info[-1]
    is_admin = bool(payload.member and payload.member.guild_permissions.administrator)

    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    if not is_admin and payload.user_id != owner_id:
        try:
            await message.remove_reaction(TRASH_EMOJI, discord.Object(id=payload.user_id))
        except discord.HTTPException:
            pass
        return

    _stop_tracking_by_card_message(payload.message_id)

    reactor = str(payload.member) if payload.member else f"user `{payload.user_id}`"
    botlog.event(f"🗑️ Untracked (🗑️ reaction, {kind}): message `{payload.message_id}` in <#{payload.channel_id}> — by **{reactor}**")

    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("Failed to delete message via reaction: %s", e)


async def _complete_track(
    channel: discord.abc.Messageable, sport_value: str, team: str, result: tuple,
    total_direction: Optional[str], total_line: Optional[float], team_total: Optional[str],
    section: Optional[str], label: Optional[str], origin_channel_id: Optional[int],
):
    """Shared tail of a successful scores365.find_match_for_team() lookup -
    called both right after a fresh auto-track attempt finds a match
    immediately, and later from a pendingtrack retry once one shows up.
    Keeping this split out means the retry path never has to duplicate (or
    drift from) the actual posting/tracking logic below."""
    game, sport_id = result
    game_id = game["id"]
    picked_team = team if total_direction is None and team_total is None else None
    if tracker.is_tracked(channel.id, game_id, picked_team, team_total, total_direction, total_line):
        botlog.event(f"⏭️ Skipped: **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await tracker.build_embed(game, sport_id, picked_team, total_direction, total_line, team_total)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    tracker.register_message(message.id, channel.id, game_id, None, picked_team, team_total, total_direction, total_line)
    await _safe_add_trash_reaction(message)

    tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, team_total,
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked: **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_track(entry: dict) -> bool:
    """resolve callback for pendingtrack - retried every
    RETRY_INTERVAL_SECONDS until it returns True (found and tracked) or the
    entry's max wait elapses. Re-fetches the channel by id since a resumed
    entry (after a bot restart) only has the persisted channel_id, not a
    live channel object."""
    try:
        channel = client.get_channel(entry["channel_id"]) or await client.fetch_channel(entry["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending track: couldn't resolve channel %s: %s", entry["channel_id"], e)
        return False
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, entry["team"], entry["sport_value"])
    except scores365.ScoresError as e:
        log.info("Pending track retry: couldn't reach 365scores for '%s': %s", entry["team"], e)
        return False
    if not result:
        return False
    await _complete_track(
        channel, entry["sport_value"], entry["team"], result,
        entry["total_direction"], entry["total_line"], entry["team_total"],
        entry["section"], entry["label"], entry["origin_channel_id"],
    )
    return True


async def _auto_track(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None, section: Optional[str] = None, label: Optional[str] = None,
    origin_channel_id: Optional[int] = None, manual: bool = False, game_id: Optional[str] = None,
):
    """Mirrors /track's core logic for an auto-detected pick - posts via
    channel.send() since there's no interaction to reply to, and has no
    owner (owner_id=None means only admins can 🗑️-delete it).

    total_direction/total_line (mutually exclusive with grading on team, both
    None otherwise) is for a game-total Over/Under pick instead of a
    moneyline - team is still used to find the match either way. team_total
    additionally set means it's one side's own total instead of the combined
    score - team_total is the actual named side being graded.

    manual=True is /tracktoday's own one-shot mode: searches today-or-
    yesterday INCLUDING an already-finished match (find_match_for_team's
    opposite bounds from auto-track's own "never finished" default - see
    that function's docstring), and reports a miss immediately instead of
    queuing a 24h retry, since a manual command has an interaction to reply
    to right away rather than a silent channel.send().

    game_id (also /tracktoday-only) bypasses find_match_for_team's own
    bulk-list team search entirely, going straight to the per-game detail
    call instead - confirmed live: 365scores' bulk games/current list can
    have its own multi-minute outage (empty response, no paging - see
    _fetch_games_for_sport's own comment) while the exact same game's
    per-game detail page keeps working fine, both on their site and via
    _get_game_detail. Lets a user who already has the game id from
    365scores' own match URL (the #id=... suffix) track it immediately
    instead of waiting out the bulk list's outage."""
    if not manual and pendingtrack.is_queued(channel.id, sport_value, team, total_direction, total_line, team_total):
        botlog.event(f"⏭️ Skipped: **{team}** ({sport_value}) — already queued, waiting for its match to be found")
        return "skipped"
    if game_id is not None:
        game = await asyncio.to_thread(scores365._get_game_detail, game_id)
        if not game:
            botlog.event(f"❌ Not tracked: **{team}** ({sport_value}) — game `{game_id}` not found on 365scores")
            return
        result = (game, game.get("sportId"))
        return await _complete_track(
            channel, sport_value, team, result, total_direction, total_line, team_total, section, label, origin_channel_id,
        )
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value, **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-track: couldn't reach 365scores for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked: **{team}** ({sport_value}) — couldn't reach 365scores: {e}")
        return
    if not result:
        if manual:
            log.info("Manual track: no match found for '%s' (%s)", team, sport_value)
            botlog.event(f"❌ Not tracked: **{team}** ({sport_value}) — no match found (manual /tracktoday)")
            return
        log.info("Auto-track: no match found for '%s' (%s), queuing retry", team, sport_value)
        pendingtrack.queue(
            channel.id, sport_value, team, total_direction, total_line, team_total,
            section, label, origin_channel_id, _resolve_pending_track,
        )
        botlog.event(
            f"⏳ Queued: **{team}** ({sport_value}) — no match found yet, will retry automatically"
        )
        return "queued"
    return await _complete_track(
        channel, sport_value, team, result, total_direction, total_line, team_total, section, label, origin_channel_id,
    )


async def _resolve_pending_f5(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending F5: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_f5(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_f5(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    combined: bool = False, handicap_line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """F5 (First 5 Innings) picks - moneyline, team total, combined total, or
    handicap/run-line - settle after the 5th inning, not the whole game - see
    f5tracker.py. combined=True means total_direction/total_line grade both
    sides' F5 runs summed together, not team's own - team is still used to
    find the match either way. manual - see _auto_track's own docstring.
    queue_on_miss=False (only set by _resolve_pending_f5's own retry call)
    skips re-queuing on a repeat miss - pendingauto's own retry loop is
    already driving this attempt, so a second miss just means "try again
    later", not "start a brand new queue entry"."""
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value, **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-F5: couldn't reach 365scores for '%s': %s", team, e)
        if manual:
            botlog.event(f"❌ Not tracked (F5): **{team}** ({sport_value}) — couldn't reach 365scores: {e}")
        return
    if not result:
        payload = {
            "channel_id": channel.id, "sport_value": sport_value, "team": team,
            "total_direction": total_direction, "total_line": total_line, "combined": combined,
            "handicap_line": handicap_line, "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("f5", payload):
            pendingauto.queue("f5", payload, _resolve_pending_f5)
            log.info("Auto-F5: no match found for '%s' (%s), queuing retry", team, sport_value)
            botlog.event(f"⏳ Queued (F5): **{team}** ({sport_value}) — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-F5: no match found for '%s' (%s)", team, sport_value)
        # Only a manual (one-shot, no retry) miss is worth posting - a
        # non-manual miss here is either still queued (queue_on_miss=True
        # but already queued from an earlier arrival) or is pendingauto's
        # own retry attempt (queue_on_miss=False) failing yet again, and
        # both of those keep trying silently in the background rather than
        # having actually given up - confirmed live, this used to post
        # "❌ Not tracked" to the log channel on EVERY 30-minute retry for
        # up to 24h straight for a pick that was never going to resolve
        # (a team name 365scores just doesn't have), spamming dozens of
        # identical messages instead of the one at queue time.
        if manual:
            botlog.event(f"❌ Not tracked (F5): **{team}** ({sport_value}) — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    picked_team = None if combined else team
    if f5tracker.is_tracked(channel.id, game_id, picked_team, total_direction, total_line, handicap_line):
        botlog.event(f"⏭️ Skipped (F5): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await f5tracker.build_embed(game, sport_id, picked_team, total_direction, total_line, handicap_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=f5tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    f5tracker.register_message(message.id, channel.id, game_id, None, picked_team, total_direction, total_line, handicap_line)
    await _safe_add_trash_reaction(message)

    f5tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, handicap_line,
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked F5 pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked (F5): **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_1h(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending 1H: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_1h_total(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_1h_total(
    channel: discord.abc.Messageable, sport_value: str, team: str, total_direction: str, total_line: float,
    combined: bool = False, section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """1H (1st Half) team total or combined total - settles after the 2nd
    quarter, not the whole game - see halftracker.py. combined=True means
    total_direction/total_line grade both sides' Q1+Q2 points summed
    together, not team's own - team is still used to find the match either
    way. manual/queue_on_miss - see _auto_track's/_auto_f5's own
    docstrings."""
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value, **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-1H: couldn't reach 365scores for '%s': %s", team, e)
        if manual:
            botlog.event(f"❌ Not tracked (1H): **{team}** ({sport_value}) — couldn't reach 365scores: {e}")
        return
    if not result:
        payload = {
            "channel_id": channel.id, "sport_value": sport_value, "team": team,
            "total_direction": total_direction, "total_line": total_line, "combined": combined,
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("1h", payload):
            pendingauto.queue("1h", payload, _resolve_pending_1h)
            log.info("Auto-1H: no match found for '%s' (%s), queuing retry", team, sport_value)
            botlog.event(f"⏳ Queued (1H): **{team}** ({sport_value}) — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-1H: no match found for '%s' (%s)", team, sport_value)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (1H): **{team}** ({sport_value}) — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    picked_team = None if combined else team
    if halftracker.is_tracked(channel.id, game_id, picked_team, total_direction, total_line):
        botlog.event(f"⏭️ Skipped (1H): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await halftracker.build_embed(game, sport_id, picked_team, total_direction, total_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=halftracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    halftracker.register_message(message.id, channel.id, game_id, None, picked_team, total_direction, total_line)
    await _safe_add_trash_reaction(message)

    halftracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, section, label, origin_channel_id,
    )
    log.info("Auto-tracked 1H pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked (1H): **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_ht_ft(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending HT/FT: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_ht_ft(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_ht_ft(
    channel: discord.abc.Messageable, sport_value: str, ht_team: str, ft_team: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """Halftime/Fulltime picks - a compound bet needing both legs to hit,
    settled across the whole game rather than just the half - see
    htfttracker.py/scores365.grade_ht_ft. ft_team is used to find the
    match (either name would work equally well - find_match_for_team just
    needs one valid side of the matchup). manual/queue_on_miss - see
    _auto_track's/_auto_f5's own docstrings."""
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, ft_team, sport_value, **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-HT/FT: couldn't reach 365scores for '%s': %s", ft_team, e)
        if manual:
            botlog.event(f"❌ Not tracked (HT/FT): **{ht_team}/{ft_team}** — couldn't reach 365scores: {e}")
        return
    if not result:
        payload = {
            "channel_id": channel.id, "sport_value": sport_value, "ht_team": ht_team, "ft_team": ft_team,
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("ht_ft", payload):
            pendingauto.queue("ht_ft", payload, _resolve_pending_ht_ft)
            log.info("Auto-HT/FT: no match found for '%s' (%s), queuing retry", ft_team, sport_value)
            botlog.event(f"⏳ Queued (HT/FT): **{ht_team}/{ft_team}** — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-HT/FT: no match found for '%s' (%s)", ft_team, sport_value)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (HT/FT): **{ht_team}/{ft_team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if htfttracker.is_tracked(channel.id, game_id, ht_team, ft_team):
        botlog.event(f"⏭️ Skipped (HT/FT): **{ht_team}/{ft_team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await htfttracker.build_embed(game, sport_id, ht_team, ft_team)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=htfttracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    htfttracker.register_message(message.id, channel.id, game_id, ht_team, ft_team, None)
    await _safe_add_trash_reaction(message)

    htfttracker.start_tracking(message, sport_id, game, channel.id, None, ht_team, ft_team, section, label, origin_channel_id)
    log.info("Auto-tracked HT/FT pick '%s/%s' -> game %s", ht_team, ft_team, game_id)
    botlog.event(f"✅ Tracked (HT/FT): **{ht_team}/{ft_team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_double_chance(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending double chance: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_double_chance(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_double_chance(
    channel: discord.abc.Messageable, team: str, covered: tuple,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """Soccer Double Chance picks - covers two of the three possible
    full-time outcomes (home win/draw/away win) in one pick - see
    doublechancetracker.py/scores365.grade_double_chance. team is used to
    find the match (either matchup side works, find_match_for_team just
    needs one valid name). manual/queue_on_miss - see _auto_track's/
    _auto_f5's own docstrings."""
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "soccer", **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-double-chance: couldn't reach 365scores for '%s': %s", team, e)
        if manual:
            botlog.event(f"❌ Not tracked (double chance): **{team}** — couldn't reach 365scores: {e}")
        return
    pick_label = doublechancetracker.pick_label(covered)
    if not result:
        payload = {
            "channel_id": channel.id, "team": team, "covered": list(covered),
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("double_chance", payload):
            pendingauto.queue("double_chance", payload, _resolve_pending_double_chance)
            log.info("Auto-double-chance: no match found for '%s', queuing retry", team)
            botlog.event(f"⏳ Queued (double chance): **{pick_label}** — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-double-chance: no match found for '%s'", team)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (double chance): **{pick_label}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if doublechancetracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped (double chance): **{pick_label}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await doublechancetracker.build_embed(game, sport_id, team, covered)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=doublechancetracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    doublechancetracker.register_message(message.id, channel.id, game_id, None)
    await _safe_add_trash_reaction(message)

    doublechancetracker.start_tracking(message, sport_id, game, channel.id, None, team, covered, section, label, origin_channel_id)
    log.info("Auto-tracked double-chance pick '%s' -> game %s", pick_label, game_id)
    botlog.event(f"✅ Tracked (double chance): **{pick_label}** — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_playerprops(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending playerprops: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_playerprops(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_playerprops(
    channel: discord.abc.Messageable, sport_value: str, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """Mirrors /playerprops' core logic for an auto-detected pick. manual/
    queue_on_miss - see _auto_track's/_auto_f5's own docstrings. Only the
    event lookup (not the player lookup - a bad name won't fix itself on
    retry) gets queued on a miss."""
    if sport_value == "kbo":
        # ESPN has no KBO league at all (only MLB) - confirmed live, a KBO
        # prop for a former-MLB player used to silently match that
        # player's old MLB athlete record and "track" against the wrong
        # team/game entirely. picks.py tags a KBO prop's sport as "kbo"
        # (distinct from a real MLB prop's "baseball") specifically so this
        # can route it to koreabaseball.py's own dedicated source instead -
        # see _PROP_SPORT_OVERRIDE's own comment for the full story, and
        # koreabaseball.py's module docstring for that source itself.
        if direction is None or line is None:
            botlog.event(f"❌ Not tracked (KBO prop): **{player}** {stat} — no line to grade against")
            return
        return await _auto_kboprop(channel, player, stat, direction, line, section, label, origin_channel_id, manual=manual)
    stat_key = espn.STAT_CATALOG.get(sport_value, {}).get(stat)
    if not stat_key:
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} ({sport_value}) — unknown stat for this sport")
        return
    try:
        entity = await asyncio.to_thread(espn.find_player, player, sport_value)
    except espn.EspnError as e:
        log.info("Auto-playerprops: couldn't reach ESPN for '%s': %s", player, e)
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — couldn't reach ESPN: {e}")
        return
    if not entity and sport_value == "basketball":
        # A generic "Basketball"/"NBA" header is sometimes used for a WNBA
        # player too (confirmed live - real picks for both Kamilla Cardoso
        # and Allisha Gray silently failed this way, since the search above
        # only ever hits the NBA endpoint for a bare "basketball" sport
        # value) - retry against WNBA before giving up, rather than relying
        # on the source to tag it correctly.
        try:
            entity = await asyncio.to_thread(espn.find_player, player, "wnba")
        except espn.EspnError as e:
            log.info("Auto-playerprops: couldn't reach ESPN for '%s': %s", player, e)
            botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — couldn't reach ESPN: {e}")
            return
        if entity:
            sport_value = "wnba"
            # Fixes tracking (ESPN calls below now correctly hit the WNBA
            # endpoint), but /summary groups purely by this literal header
            # text (see dailylog.record_pick) - left uncorrected, a pick
            # that tracked and graded fine under WNBA data would still
            # file under "NBA" in the report. Confirmed live: Shakira
            # Austin and Alyssa Thomas picks both graded correctly but
            # showed up under an "NBA" section instead of "WNBA".
            if section:
                suffix = " Props" if section.lower().endswith("props") else ""
                section = espn.SPORT_DISPLAY_LABELS.get("wnba", "WNBA") + suffix
    if not entity:
        log.info("Auto-playerprops: no player found for '%s' (%s)", player, sport_value)
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} ({sport_value}) — player not found on ESPN")
        return

    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    event_id = await asyncio.to_thread(espn.find_current_event_id, sport_value, entity["team_id"], **find_kwargs)
    if not event_id:
        payload = {
            "channel_id": channel.id, "sport_value": sport_value, "player": player, "stat": stat,
            "direction": direction, "line": line, "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("playerprops", payload):
            pendingauto.queue("playerprops", payload, _resolve_pending_playerprops)
            log.info("Auto-playerprops: no current/upcoming match found for '%s', queuing retry", player)
            botlog.event(f"⏳ Queued (prop): **{player}** {stat} — no current/upcoming match found on ESPN yet, will retry automatically")
            return "queued"
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — no current/upcoming match found on ESPN")
        return
    event = await asyncio.to_thread(espn.get_event, sport_value, event_id)
    if not event:
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — couldn't fetch match data from ESPN")
        return
    if proptracker.is_tracked(channel.id, event_id, entity["id"], stat_key, direction, line):
        botlog.event(f"⏭️ Skipped (prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return "skipped"

    current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity["id"], stat_key)
    embed, file = await proptracker.build_embed(
        entity["name"], entity["id"], entity["photo_url"], sport_value, stat, current_value, is_home, team, event,
        direction, line, entity["team_name"],
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=proptracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    proptracker.register_message(message.id, channel.id, event_id, entity["id"], stat_key, None, direction, line)
    await _safe_add_trash_reaction(message)

    proptracker.start_tracking(
        message, channel.id, event_id, entity["id"], entity["team_id"], entity["photo_url"],
        sport_value, stat_key, stat, entity["name"], None, direction, line, entity["team_name"],
        section, label, origin_channel_id, game_date=espn.eastern_date_str(event),
    )
    log.info("Auto-tracked player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (prop): **{player}** {stat} ({sport_value}) in <#{channel.id}>")
    return message.id


async def _resolve_pending_tennis_playerprops(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending tennis playerprops: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_tennis_playerprops(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_tennis_playerprops(
    channel: discord.abc.Messageable, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """Tennis-only equivalent of _auto_playerprops, backed by 365scores
    instead of ESPN (which doesn't support tennis at all) - see
    tennispropstracker.py. A tennis player is its own "competitor" in
    365scores' data, found via find_match_for_team same as every other
    365scores-backed tennis tracker (F5/1st-set/moneyline). manual/
    queue_on_miss - see _auto_track's/_auto_f5's own docstrings."""
    stat_name = scores365.TENNIS_STAT_CATALOG.get(stat)
    if not stat_name:
        botlog.event(f"❌ Not tracked (tennis prop): **{player}** {stat} — unknown stat")
        return
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, player, "tennis", **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-tennis-playerprops: couldn't reach 365scores for '%s': %s", player, e)
        if manual:
            botlog.event(f"❌ Not tracked (tennis prop): **{player}** {stat} — couldn't reach 365scores: {e}")
        return
    if not result:
        payload = {
            "channel_id": channel.id, "player": player, "stat": stat, "direction": direction, "line": line,
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("tennis_playerprops", payload):
            pendingauto.queue("tennis_playerprops", payload, _resolve_pending_tennis_playerprops)
            log.info("Auto-tennis-playerprops: no match found for '%s', queuing retry", player)
            botlog.event(f"⏳ Queued (tennis prop): **{player}** {stat} — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-tennis-playerprops: no match found for '%s'", player)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (tennis prop): **{player}** {stat} — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    if scores365.names_match(home_competitor.get("name", ""), player):
        competitor_id, resolved_name = home_competitor["id"], home_competitor.get("name", player)
    else:
        competitor_id, resolved_name = away_competitor["id"], away_competitor.get("name", player)
    if tennispropstracker.is_tracked(channel.id, game_id, competitor_id, stat_name, direction, line):
        botlog.event(f"⏭️ Skipped (tennis prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await tennispropstracker.build_embed(game, sport_id, competitor_id, resolved_name, stat, stat_name, direction, line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=tennispropstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    tennispropstracker.register_message(message.id, channel.id, game_id, competitor_id, stat_name, None, direction, line)
    await _safe_add_trash_reaction(message)

    tennispropstracker.start_tracking(
        message, sport_id, game_id, channel.id, competitor_id, stat_name, stat, resolved_name, None, direction, line,
        section, label, origin_channel_id, tournament=scores365.tournament_name(game),
        game_date=scores365.eastern_date_str(scores365.start_epoch(game)),
    )
    log.info("Auto-tracked tennis player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (tennis prop): **{player}** {stat} in <#{channel.id}>")
    return message.id


async def _resolve_soccer_psf_match(game: dict, stat_name: str) -> tuple[Optional[str], Optional[dict]]:
    """Resolves + fetches the matching playerstats.football fixture for a
    stat backed by that source (see playerstatsfootball.py) - a no-op
    (None, None) for the original scores365-backed stats, which don't need
    it. Returns (fixture_path, psf_match) so the caller can persist the
    path for soccerpropstracker._track_loop to keep polling directly on
    every later cycle, without re-resolving from scratch each time."""
    if stat_name not in playerstatsfootball.STAT_CATALOG:
        return None, None
    home = (game.get("homeCompetitor") or {}).get("name", "")
    away = (game.get("awayCompetitor") or {}).get("name", "")
    kickoff = scores365.start_epoch(game)
    fixture_path = await asyncio.to_thread(playerstatsfootball.find_fixture, home, away, kickoff)
    if not fixture_path:
        return None, None
    html = await asyncio.to_thread(playerstatsfootball.fetch_path, fixture_path)
    return fixture_path, (playerstatsfootball.parse_match(html) if html else None)


async def _complete_soccer_prop_post(
    channel: discord.abc.Messageable, player: str, stat: str, stat_name: str,
    direction: Optional[float], line: Optional[float],
    game: dict, member_id, member_competitor_id, resolved_name: str, photo_url: Optional[str],
    psf_match: Optional[dict], fixture_path: Optional[str],
    section: Optional[str], label: Optional[str], origin_channel_id: Optional[int],
):
    """Shared tail once a soccer prop's game+player (and, if this stat
    needs it, its playerstats.football fixture) are all resolved - posts
    the card and starts tracking. Split out from _complete_soccer_prop_track
    so a pendingsoccerprops PSF-fixture retry (see
    _resolve_pending_soccer_psf_fixture) can reuse it without duplicating
    the posting logic."""
    game_id = game["id"]
    embed, file = await soccerpropstracker.build_embed(
        game, member_id, member_competitor_id, resolved_name, photo_url, stat, stat_name, direction, line,
        psf_match=psf_match,
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=soccerpropstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    soccerpropstracker.register_message(message.id, channel.id, game_id, member_id, stat_name, None, direction, line)
    await _safe_add_trash_reaction(message)

    soccerpropstracker.start_tracking(
        message, game_id, channel.id, member_id, member_competitor_id, stat_name, photo_url,
        stat, resolved_name, None, direction, line, fixture_path, section, label, origin_channel_id,
        tournament=scores365.tournament_name(game),
        game_date=scores365.eastern_date_str(scores365.start_epoch(game)),
    )
    log.info("Auto-tracked soccer player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (soccer prop): **{player}** {stat} in <#{channel.id}>")
    return message.id


async def _resolve_pending_soccer_psf_fixture(entry: dict) -> bool:
    """resolve callback for pendingsoccerprops when the player was already
    found on 365scores but this stat's playerstats.football fixture wasn't
    (see _complete_soccer_prop_track) - re-fetches the game fresh on every
    retry (so the eventual card starts from current data, not a stale
    snapshot from when this was first queued) and re-attempts just the
    fixture resolution, not the whole player search again."""
    try:
        channel = client.get_channel(entry["channel_id"]) or await client.fetch_channel(entry["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending soccer PSF fixture: couldn't resolve channel %s: %s", entry["channel_id"], e)
        return False
    game = await asyncio.to_thread(scores365.soccer_game_detail, entry["game_id"])
    if not game:
        return False
    fixture_path, psf_match = await _resolve_soccer_psf_match(game, entry["stat_name"])
    if not fixture_path:
        return False
    await _complete_soccer_prop_post(
        channel, entry["player"], entry["stat"], entry["stat_name"], entry["direction"], entry["line"],
        game, entry["member_id"], entry["member_competitor_id"], entry["resolved_name"], entry["photo_url"],
        psf_match, fixture_path, entry["section"], entry["label"], entry["origin_channel_id"],
    )
    return True


async def _complete_soccer_prop_track(
    channel: discord.abc.Messageable, player: str, stat: str, stat_name: str,
    direction: Optional[float], line: Optional[float], result: tuple,
    section: Optional[str], label: Optional[str], origin_channel_id: Optional[int],
):
    """Shared tail of a successful scores365.find_soccer_player() lookup -
    called both right after a fresh auto-track attempt finds a match
    immediately, and later from a pendingsoccerprops retry once one shows up.
    Keeping this split out means the retry path never has to duplicate (or
    drift from) the actual posting/tracking logic below.

    If this stat additionally needs playerstats.football (see
    _resolve_soccer_psf_match) and that lookup fails, queues a SEPARATE
    retry scoped to just that lookup instead of failing the pick outright -
    the player's already found, no need to re-search 365scores too.
    Confirmed live: a real fixture (Philadelphia Union vs Santos Laguna)
    simply didn't have a page on that source yet hours before kickoff,
    the same "not published yet" shape as the outer player-search queue,
    not a wrong-guess bug - retrying is the right fix, not giving up."""
    game, member = result
    game_id, member_id, member_competitor_id = game["id"], member["id"], member.get("competitorId")
    resolved_name = member.get("name", player)
    photo_url = scores365.athlete_photo_url(member)
    if soccerpropstracker.is_tracked(channel.id, game_id, member_id, stat_name, direction, line):
        botlog.event(f"⏭️ Skipped (soccer prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return "skipped"
    if pendingsoccerprops.is_queued(channel.id, player, stat, stat_name, direction, line):
        botlog.event(f"⏭️ Skipped (soccer prop): **{player}** {stat} — already queued, waiting for its match to be found")
        return "skipped"

    fixture_path, psf_match = await _resolve_soccer_psf_match(game, stat_name)
    if stat_name in playerstatsfootball.STAT_CATALOG and not fixture_path:
        pendingsoccerprops.queue(
            channel.id, player, stat, stat_name, direction, line, section, label, origin_channel_id,
            _resolve_pending_soccer_psf_fixture,
            queued_detail="Queued - waiting for our extended stats source to publish this match",
            extra={
                "game_id": game_id, "member_id": member_id, "member_competitor_id": member_competitor_id,
                "resolved_name": resolved_name, "photo_url": photo_url,
            },
        )
        botlog.event(
            f"⏳ Queued (soccer prop): **{player}** {stat} — match found, but not yet on our extended "
            f"stats source, will retry automatically"
        )
        return "queued"
    return await _complete_soccer_prop_post(
        channel, player, stat, stat_name, direction, line,
        game, member_id, member_competitor_id, resolved_name, photo_url,
        psf_match, fixture_path, section, label, origin_channel_id,
    )


async def _resolve_pending_soccer_prop(entry: dict) -> bool:
    """resolve callback for pendingsoccerprops - retried every
    RETRY_INTERVAL_SECONDS until it returns True (found and tracked) or the
    entry's max wait elapses. Re-fetches the channel by id since a resumed
    entry (after a bot restart) only has the persisted channel_id, not a
    live channel object."""
    try:
        channel = client.get_channel(entry["channel_id"]) or await client.fetch_channel(entry["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending soccer prop: couldn't resolve channel %s: %s", entry["channel_id"], e)
        return False
    try:
        result = await asyncio.to_thread(scores365.find_soccer_player, entry["player"])
    except scores365.ScoresError as e:
        log.info("Pending soccer prop retry: couldn't reach 365scores for '%s': %s", entry["player"], e)
        return False
    if not result:
        return False
    await _complete_soccer_prop_track(
        channel, entry["player"], entry["stat"], entry["stat_name"], entry["direction"], entry["line"],
        result, entry["section"], entry["label"], entry["origin_channel_id"],
    )
    return True


async def _auto_soccer_playerprops(
    channel: discord.abc.Messageable, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """Soccer-only equivalent of _auto_playerprops, backed by 365scores
    instead of ESPN (which doesn't support soccer at all) - see
    soccerpropstracker.py. Unlike tennis (where the player IS the
    "competitor"), a soccer player has to be found via their match's own
    roster (scores365.find_soccer_player), since 365scores' bulk game list
    only carries club names."""
    if stat in scores365.SOCCER_STAT_CATALOG or stat in playerstatsfootball.STAT_CATALOG:
        stat_name = stat
    else:
        stat_name = None
    if not stat_name:
        botlog.event(f"❌ Not tracked (soccer prop): **{player}** {stat} — unknown stat")
        return
    if pendingsoccerprops.is_queued(channel.id, player, stat, stat_name, direction, line):
        botlog.event(f"⏭️ Skipped (soccer prop): **{player}** {stat} — already queued, waiting for its match to be found")
        return "skipped"
    try:
        result = await asyncio.to_thread(scores365.find_soccer_player, player)
    except scores365.ScoresError as e:
        log.info("Auto-soccer-playerprops: couldn't reach 365scores for '%s': %s", player, e)
        botlog.event(f"❌ Not tracked (soccer prop): **{player}** {stat} — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-soccer-playerprops: no player found for '%s', queuing retry", player)
        pendingsoccerprops.queue(
            channel.id, player, stat, stat_name, direction, line, section, label, origin_channel_id,
            _resolve_pending_soccer_prop,
        )
        botlog.event(
            f"⏳ Queued (soccer prop): **{player}** {stat} — not in a live/imminent match yet, "
            f"will retry automatically as kickoff nears"
        )
        return "queued"
    return await _complete_soccer_prop_track(channel, player, stat, stat_name, direction, line, result, section, label, origin_channel_id)


async def _resolve_pending_inning_runs(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending inning-runs: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_inning_runs(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_inning_runs(
    channel: discord.abc.Messageable, team: str, pick_type: str, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True, sport: str = "baseball",
):
    """YRFI/NRFI picks (and the general "1st Inning Total Runs Over/Under N"
    market they're a special case of - see inningtracker.py). sport is
    "baseball" for plain MLB (ESPN-backed, below) or "kbo"/"npb" for a
    league ESPN doesn't carry at all - routed to inningtotaltracker.py's
    365scores-backed grading instead (see that module's own docstring).
    manual/queue_on_miss - see _auto_track's/_auto_f5's own docstrings."""
    if sport != "baseball":
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "baseball")
        if not result:
            payload = {
                "channel_id": channel.id, "team": team, "pick_type": pick_type, "line": line,
                "section": section, "label": label, "origin_channel_id": origin_channel_id, "sport": sport,
            }
            if not manual and queue_on_miss and not pendingauto.is_queued("inning_runs", payload):
                pendingauto.queue("inning_runs", payload, _resolve_pending_inning_runs)
                log.info("Auto-inning-runs: no match found for '%s', queuing retry", team)
                botlog.event(f"⏳ Queued ({pick_type}): **{team}** — no match found yet, will retry automatically")
                return "queued"
            if manual:
                botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — no match found on 365scores")
            return
        game, sport_id = result
        game_id = game["id"]
        if inningtotaltracker.is_tracked(channel.id, game_id, pick_type, line):
            botlog.event(f"⏭️ Skipped ({pick_type}): **{team}** — already being tracked in <#{channel.id}>")
            return "skipped"

        embed, file = await inningtotaltracker.build_embed(game, sport_id, pick_type, line)
        message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
        embed.set_footer(text=inningtotaltracker._footer_text(message.id))
        await throttle.run(channel.id, lambda: message.edit(embed=embed))
        inningtotaltracker.register_message(message.id, channel.id, game_id, pick_type, line, None)
        await _safe_add_trash_reaction(message)

        inningtotaltracker.start_tracking(
            message, sport_id, game, channel.id, None, pick_type, line,
            section=section, label=label, origin_channel_id=origin_channel_id,
        )
        log.info("Auto-tracked inning-runs pick '%s' (%s) -> game %s", team, pick_type, game_id)
        botlog.event(f"✅ Tracked ({pick_type}): **{team}** — game `{game_id}` in <#{channel.id}>")
        return message.id

    try:
        entity = await asyncio.to_thread(espn.find_team, team, "baseball")
    except espn.EspnError as e:
        log.info("Auto-inning-runs: couldn't reach ESPN for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — couldn't reach ESPN: {e}")
        return
    if not entity:
        log.info("Auto-inning-runs: no team found for '%s'", team)
        botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — team not found on ESPN")
        return

    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    event_id = await asyncio.to_thread(espn.find_current_event_id, "baseball", entity["id"], **find_kwargs)
    if not event_id:
        payload = {
            "channel_id": channel.id, "team": team, "pick_type": pick_type, "line": line,
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("inning_runs", payload):
            pendingauto.queue("inning_runs", payload, _resolve_pending_inning_runs)
            log.info("Auto-inning-runs: no match found for '%s', queuing retry", team)
            botlog.event(f"⏳ Queued ({pick_type}): **{team}** — no current/upcoming match found on ESPN yet, will retry automatically")
            return "queued"
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — no current/upcoming match found on ESPN")
        return
    event = await asyncio.to_thread(espn.get_event, "baseball", event_id)
    if not event:
        botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — couldn't fetch match data from ESPN")
        return
    if inningtracker.is_tracked(channel.id, event_id, pick_type, line):
        botlog.event(f"⏭️ Skipped ({pick_type}): **{team}** — already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await inningtracker.build_embed(event, pick_type, line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=inningtracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    inningtracker.register_message(message.id, channel.id, event_id, pick_type, line, None)
    await _safe_add_trash_reaction(message)

    inningtracker.start_tracking(
        message, channel.id, event_id, pick_type, entity["id"], None, line=line,
        section=section, label=label, origin_channel_id=origin_channel_id,
        game_date=espn.eastern_date_str(event),
    )
    log.info("Auto-tracked inning-runs pick '%s' (%s) -> event %s", team, pick_type, event_id)
    botlog.event(f"✅ Tracked ({pick_type}): **{team}** — event `{event_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_inning1_result(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending 1st-inning-result: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_inning1_result(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_inning1_result(
    channel: discord.abc.Messageable, team: str, pick: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True,
):
    """1st Inning Result (3-way: team or Draw) picks settle after the 1st
    inning, not the whole game - see inning1tracker.py. Backed by 365scores
    (like f5tracker.py), not ESPN - always baseball. manual/queue_on_miss -
    see _auto_track's/_auto_f5's own docstrings."""
    find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "baseball", **find_kwargs)
    except scores365.ScoresError as e:
        log.info("Auto-1st-inning-result: couldn't reach 365scores for '%s': %s", team, e)
        if manual:
            botlog.event(f"❌ Not tracked (1st inning result): **{team}** — couldn't reach 365scores: {e}")
        return
    if not result:
        payload = {
            "channel_id": channel.id, "team": team, "pick": pick,
            "section": section, "label": label, "origin_channel_id": origin_channel_id,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("inning1_result", payload):
            pendingauto.queue("inning1_result", payload, _resolve_pending_inning1_result)
            log.info("Auto-1st-inning-result: no match found for '%s', queuing retry", team)
            botlog.event(f"⏳ Queued (1st inning result): **{team}** — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-1st-inning-result: no match found for '%s'", team)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min).
        if manual:
            botlog.event(f"❌ Not tracked (1st inning result): **{team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if inning1tracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped (1st inning result): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await inning1tracker.build_embed(game, sport_id, team, pick)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=inning1tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    inning1tracker.register_message(message.id, channel.id, game_id, None)
    await _safe_add_trash_reaction(message)

    inning1tracker.start_tracking(
        message, sport_id, game, channel.id, None, team, pick, section, label, origin_channel_id,
    )
    log.info("Auto-tracked 1st-inning-result pick '%s' (%s) -> game %s", team, pick, game_id)
    botlog.event(f"✅ Tracked (1st inning result): **{team}** ({pick}) — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _resolve_pending_tennis_market(payload: dict) -> bool:
    try:
        channel = client.get_channel(payload["channel_id"]) or await client.fetch_channel(payload["channel_id"])
    except discord.HTTPException as e:
        log.warning("Pending tennis market: couldn't resolve channel %s: %s", payload["channel_id"], e)
        return False
    result = await _auto_tennis_market(channel, **{k: v for k, v in payload.items() if k != "channel_id"}, queue_on_miss=False)
    return result is not None


async def _auto_tennis_market(
    channel: discord.abc.Messageable, team: str, market: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False, queue_on_miss: bool = True, sport: str = "tennis", game_id: Optional[str] = None,
):
    """Tennis "extra market" picks (1st Set ML/Total Games, Match Total
    Games, Win a Set) all settle on some part of the match rather than the
    whole thing finishing normally - see settracker.py. Backed by 365scores
    (like f5tracker.py), not ESPN. team is either the named player
    (set1_moneyline/win_a_set) or just one of the two matchup sides used to
    look the match up (set1_total_games/match_total_games, no specific team
    being graded). manual/queue_on_miss - see _auto_track's/_auto_f5's own
    docstrings.

    sport defaults to "tennis" but also covers volleyball's own
    set1_point_handicap market (see settracker.py) - settracker's markets
    are generic enough (sport_id already threaded through for rendering)
    that this dispatcher didn't need a second copy for the one volleyball
    market that also fits this "settles on part of the match" shape.

    game_id (also /tracktoday-only) - see _auto_track's own docstring for
    why this exists. Confirmed live: a real "Interrupted" (rain delay)
    tennis match sat unfindable by team-name search even though
    map_status_type already reads it as "inprogress" - some other filter
    in find_match_for_team's own candidate scoring still excluded it, and
    the per-game detail call resolved it immediately."""
    if game_id is not None:
        game = await asyncio.to_thread(scores365._get_game_detail, game_id)
        if not game:
            botlog.event(f"❌ Not tracked ({market}): **{team}** — game `{game_id}` not found on 365scores")
            return
        result = (game, game.get("sportId"))
    else:
        find_kwargs = {"days_ahead": 0, "days_back": 1, "allow_finished": True} if manual else {}
        try:
            result = await asyncio.to_thread(scores365.find_match_for_team, team, sport, **find_kwargs)
        except scores365.ScoresError as e:
            log.info("Auto-tennis-market (%s): couldn't reach 365scores for '%s': %s", market, team, e)
            if manual:
                botlog.event(f"❌ Not tracked ({market}): **{team}** — couldn't reach 365scores: {e}")
            return
    if not result:
        payload = {
            "channel_id": channel.id, "team": team, "market": market, "direction": direction, "line": line,
            "section": section, "label": label, "origin_channel_id": origin_channel_id, "sport": sport,
        }
        if not manual and queue_on_miss and not pendingauto.is_queued("tennis_market", payload):
            pendingauto.queue("tennis_market", payload, _resolve_pending_tennis_market)
            log.info("Auto-tennis-market (%s): no match found for '%s', queuing retry", market, team)
            botlog.event(f"⏳ Queued ({market}): **{team}** — no match found yet, will retry automatically")
            return "queued"
        log.info("Auto-tennis-market (%s): no match found for '%s'", market, team)
        # Non-manual miss here is a still-silently-retrying pendingauto
        # entry, not a real give-up - see _auto_f5's identical fix for why
        # this matters (was spamming the log channel every 30min - this is
        # the exact call site behind the live "Puerto Rico" spam).
        if manual:
            botlog.event(f"❌ Not tracked ({market}): **{team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if settracker.is_tracked(channel.id, game_id, market, team):
        botlog.event(f"⏭️ Skipped ({market}): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await settracker.build_embed(game, sport_id, market, team, direction, line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=settracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    settracker.register_message(message.id, channel.id, game_id, market, team, None)
    await _safe_add_trash_reaction(message)

    settracker.start_tracking(
        message, sport_id, game, channel.id, market, None, team, direction, line, section, label, origin_channel_id,
    )
    log.info("Auto-tracked tennis-market (%s) pick '%s' -> game %s", market, team, game_id)
    botlog.event(f"✅ Tracked ({market}): **{team}** — game `{game_id}` in <#{channel.id}>")
    return message.id


async def _auto_ufc(
    channel: discord.abc.Messageable, fighter: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """UFC picks - fight moneyline or round total - settle once the bout
    itself finishes, not tied to any wider game clock. Backed by
    espn_ufc.py (365scores has no MMA coverage at all). fighter is only
    used to look the bout up; round-total picks aren't graded on either
    fighter specifically (see ufctracker.py's combined-total-style mode)."""
    category_label = "UFC round total" if total_direction else "UFC"
    try:
        result = await asyncio.to_thread(espn_ufc.find_ufc_fight, fighter)
    except espn_ufc.EspnUfcError as e:
        log.info("Auto-UFC: couldn't reach ESPN for '%s': %s", fighter, e)
        botlog.event(f"❌ Not tracked ({category_label}): **{fighter}** — couldn't reach ESPN: {e}")
        return
    if not result:
        log.info("Auto-UFC: no bout found for '%s'", fighter)
        botlog.event(f"❌ Not tracked ({category_label}): **{fighter}** — no bout found")
        return
    event, competition, fighter_competitor, league_slug = result
    competition_id = competition["id"]
    fighter_id = None if total_direction else fighter_competitor["id"]
    fighter_name = None if total_direction else fighter_competitor["athlete"]["displayName"]
    if ufctracker.is_tracked(channel.id, competition_id, fighter_id, total_direction, total_line):
        botlog.event(f"⏭️ Skipped ({category_label}): **{fighter}** — bout `{competition_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await ufctracker.build_embed(competition, league_slug, event["name"], fighter_id, fighter_name, total_direction, total_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=ufctracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    ufctracker.register_message(message.id, channel.id, competition_id, None, fighter_id, total_direction, total_line)
    await _safe_add_trash_reaction(message)

    ufctracker.start_tracking(
        message, channel.id, league_slug, event["id"], competition_id, competition["date"], None, event["name"],
        fighter_id, fighter_name, total_direction, total_line, section, label, origin_channel_id,
    )
    log.info("Auto-tracked UFC pick '%s' -> bout %s", fighter, competition_id)
    botlog.event(f"✅ Tracked ({category_label}): **{fighter}** — bout `{competition_id}` in <#{channel.id}>")
    return message.id


async def _auto_boxing(
    channel: discord.abc.Messageable, fighter: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """Boxing moneyline picks - settle once the fight itself finishes.
    Backed by boxing.py (BoxingScene.com - neither 365scores nor ESPN's
    public API cover boxing at all)."""
    try:
        result = await asyncio.to_thread(boxing.find_boxing_fight, fighter)
    except boxing.BoxingError as e:
        log.info("Auto-boxing: couldn't reach BoxingScene for '%s': %s", fighter, e)
        botlog.event(f"❌ Not tracked (Boxing): **{fighter}** — couldn't reach BoxingScene: {e}")
        return
    if not result:
        log.info("Auto-boxing: no fight found for '%s'", fighter)
        botlog.event(f"❌ Not tracked (Boxing): **{fighter}** — no fight found")
        return
    fight_id = result["fight_id"]
    fighter_id = result["fighter1_id"] if esports.names_match(result["fighter1_name"], fighter) else result["fighter2_id"]
    fighter_name = result["fighter1_name"] if fighter_id == result["fighter1_id"] else result["fighter2_name"]
    if boxingtracker.is_tracked(channel.id, fight_id, fighter_id):
        botlog.event(f"⏭️ Skipped (Boxing): **{fighter}** — fight `{fight_id}` already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await boxingtracker.build_embed(result, fighter_id, fighter_name)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=boxingtracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    boxingtracker.register_message(message.id, channel.id, fight_id, fighter_id, None)
    await _safe_add_trash_reaction(message)

    boxingtracker.start_tracking(
        message, channel.id, fight_id, fighter_id, fighter_name, None, result.get("event_name") or "",
        section, label, origin_channel_id, game_date=scores365.eastern_date_str(boxing.start_epoch(result)),
    )
    log.info("Auto-tracked boxing pick '%s' -> fight %s", fighter, fight_id)
    botlog.event(f"✅ Tracked (Boxing): **{fighter}** — fight `{fight_id}` in <#{channel.id}>")
    return message.id


async def _auto_kboprop(
    channel: discord.abc.Messageable, player: str, stat: str, direction: str, line: float,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
    manual: bool = False,
):
    """KBO player prop picks - backed by koreabaseball.py (the league's own
    official site), since ESPN (proptracker.py's only source) has no KBO
    league at all. See koreabaseball.py's/picks.py's own docstrings for the
    full story of why this needed its own source instead of reusing
    proptracker.py's ESPN-backed path.

    manual=True is /tracktoday's own mode: unlike auto-track (which always
    targets today's KST date and waits/polls for that game log row to
    appear), this checks for an already-posted row under today's date
    first, then yesterday's, reporting a miss immediately rather than
    polling - same "today or yesterday, whichever is most recent, no
    further back" bound as every other manual-tracked sport."""
    try:
        player_entry = await asyncio.to_thread(koreabaseball.find_player, player)
    except koreabaseball.KboError as e:
        log.info("Auto-KBO-prop: couldn't reach koreabaseball.com for '%s': %s", player, e)
        botlog.event(f"❌ Not tracked (KBO prop): **{player}** {stat} — couldn't reach koreabaseball.com: {e}")
        return
    if not player_entry:
        log.info("Auto-KBO-prop: no player found for '%s'", player)
        botlog.event(f"❌ Not tracked (KBO prop): **{player}** {stat} — player not found on koreabaseball.com")
        return
    if not koreabaseball.stat_supported(stat, player_entry["is_pitcher"]):
        botlog.event(f"❌ Not tracked (KBO prop): **{player}** {stat} — unsupported stat for a {'pitcher' if player_entry['is_pitcher'] else 'hitter'}")
        return

    pcode = player_entry["pcode"]
    target_date = koreabaseball.today_kst_mmdd()
    if manual:
        row = await asyncio.to_thread(koreabaseball.find_game_row, pcode, player_entry["is_pitcher"], target_date)
        if not row:
            yesterday = koreabaseball.yesterday_kst_mmdd()
            row = await asyncio.to_thread(koreabaseball.find_game_row, pcode, player_entry["is_pitcher"], yesterday)
            if row:
                target_date = yesterday
        if not row:
            log.info("Manual KBO prop: no game found for '%s' today or yesterday", player)
            botlog.event(f"❌ Not tracked (KBO prop): **{player}** {stat} — no game found today or yesterday (manual /tracktoday)")
            return
    if kboproptracker.is_tracked(channel.id, pcode, stat, direction, line, target_date):
        botlog.event(f"⏭️ Skipped (KBO prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file = await kboproptracker.build_embed(
        pcode, player_entry["name"], player_entry["team"], player_entry["is_pitcher"], stat, direction, line,
        row=None, target_date=target_date,
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=kboproptracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    kboproptracker.register_message(message.id, channel.id, pcode, stat, direction, line, target_date, None)
    await _safe_add_trash_reaction(message)

    kboproptracker.start_tracking(
        message, channel.id, pcode, stat, direction, line, target_date, player_entry["name"],
        player_entry["team"], player_entry["is_pitcher"], None, section, label, origin_channel_id,
    )
    log.info("Auto-tracked KBO prop pick '%s' -> pcode %s", player, pcode)
    botlog.event(f"✅ Tracked (KBO prop): **{player}** {stat} — pcode `{pcode}` in <#{channel.id}>")
    return message.id


async def _auto_esports(
    channel: discord.abc.Messageable, sport: str, team_a: str, team_b: str, market: str,
    picked_team: Optional[str] = None, direction: Optional[str] = None, line: Optional[float] = None,
    map_number: Optional[int] = None, picked_maps: Optional[int] = None, other_maps: Optional[int] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """Dota 2 / CS2 picks - six markets, all settling on the overall series
    or one specific map within it - see esports.py/esportstracker.py.
    Unlike every other sport in this bot, both team_a and team_b (not just
    one) are needed to resolve the match at all - hawk.live/GosuGamers have
    no "find any match for this one team" lookup the way 365scores/ESPN do."""
    category_label = f"esports {market}"
    series_data = await asyncio.to_thread(esports.get_series, sport, team_a, team_b)
    if not series_data:
        log.info("Auto-esports (%s): no match found for '%s v %s'", market, team_a, team_b)
        botlog.event(f"❌ Not tracked ({category_label}): **{team_a} v {team_b}** — no match found")
        return
    if esportstracker.is_tracked(channel.id, sport, team_a, team_b, market):
        botlog.event(f"⏭️ Skipped ({category_label}): **{team_a} v {team_b}** — already being tracked in <#{channel.id}>")
        return "skipped"

    embed, file, _early_result = await esportstracker.build_embed(
        series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=esportstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    esportstracker.register_message(message.id, channel.id, sport, team_a, team_b, market, None)
    await _safe_add_trash_reaction(message)

    esportstracker.start_tracking(
        message, sport, team_a, team_b, channel.id, market, None,
        picked_team, direction, line, map_number, picked_maps, other_maps, section, label, origin_channel_id,
        tournament=series_data.get("tournament"),
        game_date=scores365.eastern_date_str(esports.start_epoch(series_data)),
    )
    log.info("Auto-tracked esports (%s) pick '%s v %s' -> %s", market, team_a, team_b, sport)
    botlog.event(f"✅ Tracked ({category_label}): **{team_a} v {team_b}** in <#{channel.id}>")
    return message.id


def _forwarded_content_and_attachments(message: discord.Message) -> tuple[str, list[discord.Attachment]]:
    """Discord's own "Forward" message feature carries the original
    message's text/images in message.message_snapshots, NOT
    message.content/message.attachments on the forwarding message itself
    (both empty there) - confirmed live, a forwarded GreenFox picks image
    was completely invisible to this whole pipeline, logged as "parsed
    0/0 line(s)" with no error at all, since nothing ever even looked at
    the snapshot. Combines the forwarding message's own content/
    attachments (a caption or extra image the forwarder personally added,
    if any - rare, but not prevented) with every snapshot's own."""
    content = message.content
    attachments = list(message.attachments)
    for snapshot in message.message_snapshots:
        if snapshot.content:
            content = f"{content}\n{snapshot.content}" if content else snapshot.content
        attachments.extend(snapshot.attachments)
    return content, attachments


async def _extract_image_picks_text(message: discord.Message, attachments: list[discord.Attachment]) -> str:
    """Reads every image attachment via image_picks (Claude vision) and
    joins their transcribed pick lines together - None/empty results (no
    ANTHROPIC_API_KEY configured, no image attachments, or the model
    found nothing readable) all collapse to "" so callers can just treat
    this as an optional text suffix, never an error to handle specially.
    Multiple images (confirmed GreenFox posts these as a single slate
    graphic, but nothing stops a multi-image message) are each read
    independently and combined - a line from one image ending up on the
    same message as a line from another is no different from two lines
    in the same block of text. attachments comes from
    _forwarded_content_and_attachments, not message.attachments directly
    - see that function's own docstring for why."""
    images = [a for a in attachments if (a.content_type or "").startswith("image/")]
    if not images:
        return ""
    texts = []
    failures = 0
    for attachment in images:
        try:
            image_bytes = await attachment.read()
        except discord.HTTPException as e:
            log.warning("Failed to download image attachment %s: %s", attachment.filename, e)
            failures += 1
            botlog.event(f"❌ Couldn't download image attachment **{attachment.filename}** from **{message.author}** in <#{message.channel.id}>: {e}")
            continue
        text = await image_picks.extract_picks_text(image_bytes, attachment.content_type)
        if text is None:
            failures += 1
            botlog.event(
                f"❌ Image picks: couldn't read **{attachment.filename}** from **{message.author}** in <#{message.channel.id}> "
                f"(ANTHROPIC_API_KEY not configured, or the API call failed - see server logs)"
            )
        elif text:
            texts.append(text)
    if texts:
        line_count = sum(len(t.splitlines()) for t in texts)
        log.info("Image picks: transcribed %d line(s) from %d image(s)", line_count, len(texts))
        transcript = "\n".join(texts)
        botlog.event(
            f"🖼️ Image picks from **{message.author}** in <#{message.channel.id}>: "
            f"transcribed {line_count} line(s) from {len(texts)} image(s)\n```\n{transcript[:1700]}\n```"
        )
    elif not failures:
        botlog.event(f"🖼️ Image picks from **{message.author}** in <#{message.channel.id}>: found nothing readable in {len(images)} image(s)")
    return "\n".join(texts)


@client.event
async def on_message(message: discord.Message):
    target_channel_id = config.PICKS_CHANNEL_MAP.get(message.channel.id)
    if target_channel_id is None or message.author.id == client.user.id:
        return
    if message.id in _processed_message_ids:
        log.warning("Ignoring duplicate on_message delivery for message %s in channel %s", message.id, message.channel.id)
        return
    _processed_message_ids.add(message.id)

    content, attachments = _forwarded_content_and_attachments(message)
    image_text = await _extract_image_picks_text(message, attachments)
    if image_text:
        content = f"{content}\n{image_text}" if content else image_text

    log.info("Picks channel message received: %r", content)
    parsed = picks.parse_picks_message(content)
    log.info("Parsed %d pick(s) from that message", len(parsed))
    line_count = len([ln for ln in content.splitlines() if ln.strip()])
    botlog.event(
        f"📥 Picks message from **{message.author}** in <#{message.channel.id}>: "
        f"parsed {len(parsed)}/{line_count} line(s)"
    )

    try:
        target_channel = client.get_channel(target_channel_id) or await client.fetch_channel(target_channel_id)
    except discord.HTTPException as e:
        log.warning("Auto-track: couldn't reach scores channel %s: %s", target_channel_id, e)
        botlog.event(f"❌ Couldn't reach target scores channel `{target_channel_id}`: {e}")
        return

    not_tracked_lines = []
    tracked_count = queued_count = skipped_count = not_tracked_count = 0
    for pick in parsed:
        # section/label are the verbatim picks-channel header ("MLB", "WNBA",
        # ...) and raw pick line text (see picks.py's parse_picks_message) -
        # threaded into every start_tracking call below purely so /summary
        # can later report on this pick; None for anything picks.py couldn't
        # attribute to a header (dailylog.record_pick no-ops in that case).
        # origin_channel_id is this picks-SOURCE channel itself (not
        # target_channel, which is where the card actually gets posted) -
        # lets /summary group/route reports by config.SUMMARY_ROUTES
        # regardless of which target channel a pick ends up tracked into.
        raw_line = pick.get("raw")
        # label (what /summary and dailylog actually display) is the raw
        # line with every trailing "(Bookmaker odds)"/"(Alt Line)"
        # annotation stripped - raw_line itself stays untouched for
        # _tracked_lines below, which needs the full text (odds included)
        # so on_message_edit still treats a price-only change as a
        # different line (see its own docstring).
        label = picks.clean_label(raw_line) if raw_line else raw_line
        card_id = await _dispatch_pick(target_channel, pick, pick.get("section"), label, message.channel.id)
        # Counted by card_id's type alone, independent of raw_line - keeps
        # tracked_count + queued_count + skipped_count + not_tracked_count
        # always equal to len(parsed) exactly (raw_line is only needed for
        # this loop's own _tracked_lines/not_tracked_lines bookkeeping,
        # which a handful of pick kinds don't fill in - see picks.py).
        if isinstance(card_id, int):
            tracked_count += 1
            if raw_line:
                _tracked_lines.setdefault(message.id, {})[raw_line] = card_id
        elif card_id == "queued":
            queued_count += 1
        elif card_id == "skipped":
            skipped_count += 1
        else:
            # Genuinely never tracked (no match found and no retry queue
            # for this pick kind, unknown stat, ...) - see _dispatch_pick's
            # own docstring for the full breakdown of what falls here.
            not_tracked_count += 1
            if raw_line:
                not_tracked_lines.append(raw_line)
    botlog.event(
        f"📊 {message.author}'s picks message in <#{message.channel.id}>: "
        f"✅ {tracked_count} tracked, ⏳ {queued_count} queued, "
        f"⏭️ {skipped_count} skipped, ❌ {not_tracked_count} not tracked "
        f"(of {len(parsed)} parsed)"
    )
    await _report_not_tracked_lines(message, not_tracked_lines)


async def _report_not_tracked_lines(message: discord.Message, raw_lines: list[str]):
    """Posts one consolidated botlog recap of every picks-message line that
    parsed into a recognized pick but never ended up tracked (no match
    found, unknown stat, already tracked, queued for retry, ...) - each
    individual _auto_* function already logs its own specific reason, but
    this lets a reviewer see the exact raw source lines to check in one
    place instead of piecing them together from scattered log entries.
    Doesn't cover a line that failed to parse into a pick at all
    (unrecognized wording, a plain section sub-header, ...) - that never
    reaches the caller's `parsed` list in the first place."""
    if not raw_lines:
        return
    header = (
        f"📋 {len(raw_lines)} line(s) from **{message.author}**'s picks message in "
        f"<#{message.channel.id}> didn't end up tracked (see the reasons logged above) — raw source text:"
    )
    chunk = header
    for i, line in enumerate(raw_lines, 1):
        entry = f"{i}. {line}"
        if len(chunk) + len(entry) + 1 > 1900:
            botlog.event(chunk)
            chunk = entry
        else:
            chunk += "\n" + entry
    botlog.event(chunk)


async def _dispatch_pick(
    target_channel: discord.abc.Messageable, pick: dict,
    section: Optional[str], label: Optional[str], origin_channel_id: Optional[int], manual: bool = False,
    game_id: Optional[str] = None,
) -> Optional[int | str]:
    """Routes one already-parsed pick to its tracker, mirroring exactly
    which _auto_* function on_message would have called - shared with
    on_message_edit so a newly-added line (from editing an existing picks
    message) gets tracked through the identical path a brand new message
    would use. Returns the posted card's message id (each _auto_* function
    returns this on success now, purely so on_message/on_message_edit can
    remember "this raw line -> this card" for later - see _tracked_lines).
    Returns the literal string "skipped" if the pick is a duplicate of one
    already being tracked, or "queued" if it's waiting on pendingtrack/
    pendingsoccerprops for its match to become findable - neither is a card
    id, but neither is an outright failure either, so on_message's tally
    (and its not-tracked recap, see _report_not_tracked_lines) can tell
    them apart from a genuine miss. None means it genuinely never got
    tracked (no match found and no retry queue for this pick kind, unknown
    stat, couldn't reach the data source, ...).

    manual=True is /tracktoday's own one-shot mode - see _auto_track's own
    docstring for what that changes. Not accepted for soccer_playerprops
    (see _auto_soccer_playerprops' own narrower live+imminent-only
    architecture) - /tracktoday rejects that combination before ever
    reaching here.

    game_id (also /tracktoday-only, and only wired to the three "track"-
    family kinds below) - see _auto_track's own docstring."""
    try:
        if pick["kind"] == "track":
            return await _auto_track(target_channel, pick["sport"], pick["team"], section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id)
        elif pick["kind"] == "total":
            return await _auto_track(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "team_total":
            return await _auto_track(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], pick["team"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "f5_moneyline":
            return await _auto_f5(target_channel, pick["sport"], pick["team"], section=section, label=label, origin_channel_id=origin_channel_id, manual=manual)
        elif pick["kind"] == "f5_total":
            return await _auto_f5(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "f5_combined_total":
            return await _auto_f5(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], combined=True,
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "f5_handicap":
            return await _auto_f5(
                target_channel, pick["sport"], pick["team"], handicap_line=pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "1h_total":
            return await _auto_1h_total(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "1h_combined_total":
            return await _auto_1h_total(
                target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], combined=True,
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "ht_ft":
            return await _auto_ht_ft(
                target_channel, pick["sport"], pick["ht_team"], pick["ft_team"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "double_chance":
            return await _auto_double_chance(
                target_channel, pick["team"], pick["covered"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "inning_runs":
            return await _auto_inning_runs(
                target_channel, pick["team"], pick["pick_type"], line=pick.get("line"),
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
                sport=pick.get("sport", "baseball"),
            )
        elif pick["kind"] == "inning1_result":
            return await _auto_inning1_result(target_channel, pick["team"], pick["pick"], section=section, label=label, origin_channel_id=origin_channel_id, manual=manual)
        elif pick["kind"] == "set1_moneyline":
            return await _auto_tennis_market(target_channel, pick["team"], "set1_moneyline", section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id)
        elif pick["kind"] == "tennis_set1_total_games":
            return await _auto_tennis_market(
                target_channel, pick["team"], "set1_total_games", pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "tennis_match_total_games":
            return await _auto_tennis_market(
                target_channel, pick["team"], "match_total_games", pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "tennis_player_total_games":
            return await _auto_tennis_market(
                target_channel, pick["team"], "player_total_games", pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "tennis_win_a_set":
            return await _auto_tennis_market(
                target_channel, pick["team"], "win_a_set", pick["direction"], section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "tennis_games_handicap":
            return await _auto_tennis_market(
                target_channel, pick["team"], "games_handicap", None, pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "tennis_sets_handicap":
            return await _auto_tennis_market(
                target_channel, pick["team"], "sets_handicap", None, pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, game_id=game_id,
            )
        elif pick["kind"] == "volleyball_set1_handicap":
            return await _auto_tennis_market(
                target_channel, pick["team"], "set1_point_handicap", None, pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, sport="volleyball", game_id=game_id,
            )
        elif pick["kind"] == "volleyball_match_point_handicap":
            return await _auto_tennis_market(
                target_channel, pick["team"], "match_point_handicap", None, pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, sport="volleyball", game_id=game_id,
            )
        elif pick["kind"] == "volleyball_match_point_total":
            return await _auto_tennis_market(
                target_channel, pick["team"], "match_point_total", pick["direction"], pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual, sport="volleyball", game_id=game_id,
            )
        elif pick["kind"] == "tennis_playerprops":
            return await _auto_tennis_playerprops(
                target_channel, pick["player"], pick["stat"], pick.get("direction"), pick.get("line"),
                section=section, label=label, origin_channel_id=origin_channel_id, manual=manual,
            )
        elif pick["kind"] == "soccer_playerprops":
            if manual:
                botlog.event(f"❌ Not tracked: **{pick['player']}** {pick['stat']} — /tracktoday doesn't support soccer player props yet")
                return None
            return await _auto_soccer_playerprops(
                target_channel, pick["player"], pick["stat"], pick.get("direction"), pick.get("line"),
                section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "ufc_moneyline":
            return await _auto_ufc(target_channel, pick["team"], section=section, label=label, origin_channel_id=origin_channel_id)
        elif pick["kind"] == "ufc_round_total":
            return await _auto_ufc(
                target_channel, pick["team"], pick["direction"], pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "boxing_moneyline":
            return await _auto_boxing(target_channel, pick["team"], section=section, label=label, origin_channel_id=origin_channel_id)
        elif pick["kind"] == "esports_match_winner":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "match_winner",
                picked_team=pick["team"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_map_handicap":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_handicap",
                picked_team=pick["team"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_total_maps":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "total_maps",
                direction=pick["direction"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_map_winner":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_winner",
                picked_team=pick["team"], map_number=pick["map_number"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_match_and_map_winner":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "match_and_map_winner",
                picked_team=pick["team"], map_number=pick["map_number"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_win_at_least_one_map":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "win_at_least_one_map",
                picked_team=pick["team"], direction=pick["direction"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_correct_score":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "correct_score",
                picked_team=pick["team"], picked_maps=pick["picked_maps"], other_maps=pick["other_maps"],
                section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_total_kills":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "total_kills",
                direction=pick["direction"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_team_total_kills":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "team_total_kills",
                picked_team=pick["team"], direction=pick["direction"], line=pick["line"],
                section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_map_kills_handicap":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_kills_handicap",
                picked_team=pick["team"], line=pick["line"], map_number=pick["map_number"],
                section=section, label=label, origin_channel_id=origin_channel_id,
            )
        elif pick["kind"] == "esports_map_total_kills":
            return await _auto_esports(
                target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_total_kills",
                direction=pick["direction"], line=pick["line"], map_number=pick["map_number"],
                section=section, label=label, origin_channel_id=origin_channel_id,
            )
        else:
            return await _auto_playerprops(
                target_channel, pick["sport"], pick["player"], pick["stat"],
                pick.get("direction"), pick.get("line"), section=section, label=label, origin_channel_id=origin_channel_id,
                manual=manual,
            )
    except Exception as e:
        log.warning("Failed to auto-track pick %s: %s", pick, e)
        botlog.event(f"❌ Not tracked: pick `{pick}` — unexpected error: {e}")
        return None


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Confirmed live: a picks-channel message got edited after some of its
    lines were already tracked (a provider corrected/retracted a pick) -
    the bot had no way to notice, so the retracted pick just kept tracking
    to completion anyway, invisibly. Diffs the edit's raw pick lines
    against what _tracked_lines remembers tracking from this message's
    previous version:
      - a line present before but gone now -> untrack it (see
        _stop_tracking_by_card_message).
      - a line that's new -> track it via the exact same dispatch a fresh
        message would use (_dispatch_pick).
      - a line whose text changed even slightly (different odds, a
        flipped YRFI/NRFI call, etc.) is NOT specially diffed word-by-word
        - it's simply absent from one side and present in the other, so it
          falls out as an untrack-old + track-new pair automatically.
      - a line that's byte-identical in both versions is left alone.
    """
    target_channel_id = config.PICKS_CHANNEL_MAP.get(before.channel.id)
    if target_channel_id is None or after.author.id == client.user.id:
        return

    old_lines = _tracked_lines.get(before.id)
    if not old_lines:
        # Nothing was tracked from this message's previous version (never
        # parsed anything, or the bot restarted since - _tracked_lines is
        # in-memory only) - no baseline to diff this edit against.
        return

    try:
        target_channel = client.get_channel(target_channel_id) or await client.fetch_channel(target_channel_id)
    except discord.HTTPException as e:
        log.warning("Picks message edit: couldn't reach scores channel %s: %s", target_channel_id, e)
        botlog.event(f"❌ Couldn't reach target scores channel `{target_channel_id}` to process a picks message edit: {e}")
        return

    new_parsed = picks.parse_picks_message(after.content)
    new_raw_to_pick = {p["raw"]: p for p in new_parsed if p.get("raw")}

    removed_lines = [line for line in old_lines if line not in new_raw_to_pick]
    added_lines = [line for line in new_raw_to_pick if line not in old_lines]

    if not removed_lines and not added_lines:
        return  # every previously-tracked line is still there, word-for-word

    botlog.event(
        f"✏️ Picks message edited by **{after.author}** in <#{before.channel.id}>: "
        f"{len(removed_lines)} pick line(s) removed, {len(added_lines)} added"
    )

    for line in removed_lines:
        card_id = old_lines.pop(line)
        kind = _stop_tracking_by_card_message(card_id)
        if kind:
            botlog.event(f"🗑️ Untracked (message edit removed this pick, {kind}): \"{line}\" in <#{before.channel.id}>")
        else:
            botlog.event(f"⚠️ Message edit removed a pick line, but its card was already gone (nothing to untrack): \"{line}\" in <#{before.channel.id}>")

    for line in added_lines:
        pick = new_raw_to_pick[line]
        card_id = await _dispatch_pick(target_channel, pick, pick.get("section"), picks.clean_label(line), before.channel.id)
        if isinstance(card_id, int):
            old_lines[line] = card_id

    if old_lines:
        _tracked_lines[before.id] = old_lines
    else:
        _tracked_lines.pop(before.id, None)


def _channel_allowed(interaction: discord.Interaction) -> bool:
    return config.ALLOWED_CHANNEL_IDS is None or interaction.channel_id in config.ALLOWED_CHANNEL_IDS


async def _reject_wrong_channel(interaction: discord.Interaction):
    # Deliberately doesn't list config.ALLOWED_CHANNEL_IDS - same reasoning
    # as _reject_summary_wrong_channel: whoever runs a command in the wrong
    # place isn't necessarily someone who should see the full list of every
    # channel this bot operates in.
    await interaction.response.send_message("Unable to use this command in this channel.", ephemeral=True)


def _summary_allowed(interaction: discord.Interaction) -> bool:
    """/summary is restricted to server admins plus config.SUMMARY_ALLOWED_USER_IDS
    - a fixed allowlist rather than Discord's own per-command permission
    system (@app_commands.default_permissions), since that would need a
    server admin in each individual server to grant the override through
    Discord's own UI, which isn't an option for a user who isn't an admin
    there in the first place."""
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    return bool(is_admin) or interaction.user.id in config.SUMMARY_ALLOWED_USER_IDS


def _log_command(interaction: discord.Interaction, **params):
    detail = ", ".join(f"{k}={v}" for k, v in params.items() if v is not None)
    botlog.event(
        f"⌨️ **{interaction.user}** used `/{interaction.command.name}` in <#{interaction.channel_id}>"
        + (f" — {detail}" if detail else "")
    )


async def _find_match_or_reply(interaction: discord.Interaction, team: str, sport: Optional[str], ephemeral: bool = False):
    await interaction.response.defer(ephemeral=ephemeral)
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport)
    except scores365.ScoresError as e:
        await interaction.followup.send(f"Couldn't reach 365scores: {e}", ephemeral=True)
        return None

    if not result:
        where = f" in {sport}" if sport else ""
        await interaction.followup.send(
            f"No live or scheduled-today match found for **{team}**{where}.", ephemeral=True
        )
        return None

    return result  # (game, sport_id)


@tree.command(name="score", description="Get a team's current match score")
@app_commands.describe(sport="Sport to search in", team="Team name, e.g. Arsenal")
@app_commands.choices(sport=SPORT_CHOICES)
async def score(interaction: discord.Interaction, sport: app_commands.Choice[str], team: str):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, sport=sport.name, team=team)
    result = await _find_match_or_reply(interaction, team, sport.value, ephemeral=True)
    if not result:
        return
    game, sport_id = result
    embed, file = await tracker.build_embed(game, sport_id)
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


@tree.command(name="track", description="Post a live score that auto-updates until the match ends")
@app_commands.describe(sport="Sport to search in", team="Team name, e.g. Arsenal")
@app_commands.choices(sport=SPORT_CHOICES)
async def track(interaction: discord.Interaction, sport: app_commands.Choice[str], team: str):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, sport=sport.name, team=team)
    result = await _find_match_or_reply(interaction, team, sport.value)
    if not result:
        return
    game, sport_id = result

    game_id = game["id"]
    if tracker.is_tracked(interaction.channel_id, game_id):
        await interaction.followup.send("That match is already being tracked in this channel.", ephemeral=True)
        return

    embed, file = await tracker.build_embed(game, sport_id, team)
    message = await interaction.followup.send(embed=embed, file=file, wait=True)

    # interaction followup messages are bound to a webhook token that expires
    # after ~15 minutes; re-fetch as a plain channel message so edits keep
    # working for the entire tracking duration.
    message = await interaction.channel.fetch_message(message.id)
    embed.set_footer(text=tracker._footer_text(message.id))
    await throttle.run(interaction.channel_id, lambda: message.edit(embed=embed))
    tracker.register_message(message.id, interaction.channel_id, game_id, interaction.user.id)
    await _safe_add_trash_reaction(message)

    if scores365.is_finished(game):
        return  # Nothing to track, match is already over.

    tracker.start_tracking(message, sport_id, game, interaction.channel_id, interaction.user.id, team)
    botlog.event(f"✅ Tracked (manual): **{team}** ({sport.name}) — game `{game_id}` in <#{interaction.channel_id}>, by **{interaction.user}**")


# Distinct from SPORT_CHOICES (/track's own, coarser dropdown) - this needs
# to line up with picks.py's actual "[Category]" bracket tags (see
# picks._SPORT_MAP) so parse_pick_line can reuse its exact same parser
# below, rather than /track's collapsed "baseball"/"basketball" values that
# can't distinguish MLB from KBO or NBA from WNBA.
_TRACKTODAY_SPORT_CHOICES = [
    app_commands.Choice(name="MLB", value="MLB"),
    app_commands.Choice(name="KBO", value="KBO"),
    app_commands.Choice(name="YRFI/NRFI (MLB 1st inning)", value="YRFI/NRFI"),
    app_commands.Choice(name="NBA", value="NBA"),
    app_commands.Choice(name="WNBA", value="WNBA"),
    app_commands.Choice(name="NFL", value="NFL"),
    app_commands.Choice(name="NHL", value="NHL"),
    app_commands.Choice(name="Soccer", value="Soccer"),
    app_commands.Choice(name="Tennis", value="Tennis"),
    app_commands.Choice(name="Rugby", value="Rugby"),
    app_commands.Choice(name="Volleyball", value="Volleyball"),
    app_commands.Choice(name="UFC/MMA", value="UFC"),
    app_commands.Choice(name="Boxing", value="Boxing"),
    app_commands.Choice(name="Dota 2", value="Dota 2"),
    app_commands.Choice(name="CS2", value="CS2"),
]


@tree.command(
    name="tracktoday",
    description="Manually track a pick against today's or yesterday's match, even an already-finished one",
)
@app_commands.describe(
    sport="Sport/league the pick is for",
    pick='The pick itself, e.g. "Los Angeles ML" or "Fernando Tatis Jr. Over 0.5 Total Bases"',
    game_id='Optional: 365scores game id (from the match URL\'s "#id=...") to track directly, '
            "bypassing team-name search - not supported for player props or ESPN-backed markets",
)
@app_commands.choices(sport=_TRACKTODAY_SPORT_CHOICES)
async def tracktoday(interaction: discord.Interaction, sport: app_commands.Choice[str], pick: str, game_id: Optional[str] = None):
    """Unlike every _auto_* pick this bot detects on its own (which only
    ever attach to a live or not-yet-started match - see find_match_for_
    team/find_current_event_id's own docstrings), this deliberately also
    accepts an already-finished match from today or yesterday, for a pick
    the user wants tracked/graded after the fact. Reuses picks.py's exact
    parser by reconstructing the same "[Category] description" line format
    every picks-channel message already uses - see picks.parse_pick_line.

    game_id is the escape hatch for 365scores' own bulk-list outages (see
    _auto_track's own docstring) - team-name search is unusable while that
    list is broken, but a game id copied from 365scores' own match page
    still resolves fine via the per-game detail call."""
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, sport=sport.name, pick=pick, game_id=game_id)

    parsed = picks.parse_pick_line(f"[{sport.value}] {pick.strip()}")
    if not parsed:
        await interaction.response.send_message(
            f"Couldn't understand that {sport.name} pick. Try wording it the same way a picks-channel "
            f'message would, e.g. "Los Angeles ML", "Tampa Bay Rays First 5 Innings ML", or '
            f'"Fernando Tatis Jr. Over 0.5 Total Bases".',
            ephemeral=True,
        )
        return
    if parsed["kind"] == "soccer_playerprops":
        await interaction.response.send_message(
            "/tracktoday doesn't support soccer player props yet - try /playerprops once the match is live instead.",
            ephemeral=True,
        )
        return
    _GAME_ID_SUPPORTED_KINDS = (
        "track", "total", "team_total", "set1_moneyline", "tennis_set1_total_games", "tennis_match_total_games",
        "tennis_player_total_games", "tennis_win_a_set", "tennis_games_handicap", "tennis_sets_handicap",
        "volleyball_set1_handicap", "volleyball_match_point_handicap", "volleyball_match_point_total",
    )
    if game_id is not None and parsed["kind"] not in _GAME_ID_SUPPORTED_KINDS:
        await interaction.response.send_message(
            "game_id isn't supported for this pick type yet.", ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    result = await _dispatch_pick(
        interaction.channel, parsed, section=None, label=picks.clean_label(pick.strip()), origin_channel_id=interaction.channel_id, manual=True,
        game_id=game_id,
    )
    if result is None:
        await interaction.followup.send(
            f"No {sport.name} match found for that pick within today or yesterday.", ephemeral=True,
        )
    elif result in ("skipped", "queued"):
        await interaction.followup.send("That pick is already being tracked in this channel.", ephemeral=True)
    else:
        await interaction.followup.send(f"Tracked in <#{interaction.channel_id}>.", ephemeral=True)
        botlog.event(f"✅ Tracked (manual /tracktoday): **{pick.strip()}** ({sport.name}) in <#{interaction.channel_id}>, by **{interaction.user}**")


async def stat_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    sport = getattr(interaction.namespace, "sport", None)
    sport_key = sport.value if hasattr(sport, "value") else sport
    if sport_key == "tennis":
        labels = list(scores365.TENNIS_STAT_CATALOG.keys())
    elif sport_key == "soccer":
        labels = list(scores365.SOCCER_STAT_CATALOG.keys()) + list(playerstatsfootball.STAT_CATALOG.keys())
    else:
        labels = list(espn.STAT_CATALOG.get(sport_key, {}).keys())
    matches = [label for label in labels if current.lower() in label.lower()]
    return [app_commands.Choice(name=label, value=label) for label in matches[:25]]


async def _playerprops_tennis(interaction: discord.Interaction, player: str, stat: str):
    """Tennis-only equivalent of /playerprops' ESPN-backed body, using
    365scores instead (see tennispropstracker.py)."""
    stat_name = scores365.TENNIS_STAT_CATALOG.get(stat)
    if not stat_name:
        await interaction.followup.send(
            f"Unknown stat '{stat}' for Tennis - pick one from the autocomplete list.", ephemeral=True
        )
        return

    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, player, "tennis")
    except scores365.ScoresError as e:
        await interaction.followup.send(f"Couldn't reach 365scores: {e}", ephemeral=True)
        return
    if not result:
        await interaction.followup.send(f"No live or scheduled-today match found for **{player}**.", ephemeral=True)
        return
    game, sport_id = result
    game_id = game["id"]
    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    if scores365.names_match(home_competitor.get("name", ""), player):
        competitor_id, resolved_name = home_competitor["id"], home_competitor.get("name", player)
    else:
        competitor_id, resolved_name = away_competitor["id"], away_competitor.get("name", player)

    embed, file = await tennispropstracker.build_embed(game, sport_id, competitor_id, resolved_name, stat, stat_name)
    message = await interaction.followup.send(embed=embed, file=file, wait=True)

    # interaction followup messages are bound to a webhook token that expires
    # after ~15 minutes; re-fetch as a plain channel message so edits keep
    # working for the entire tracking duration.
    message = await interaction.channel.fetch_message(message.id)
    embed.set_footer(text=tennispropstracker._footer_text(message.id))
    await throttle.run(interaction.channel_id, lambda: message.edit(embed=embed))
    tennispropstracker.register_message(message.id, interaction.channel_id, game_id, competitor_id, stat_name, interaction.user.id)
    await _safe_add_trash_reaction(message)

    if not scores365.is_finished(game):
        tennispropstracker.start_tracking(
            message, sport_id, game_id, interaction.channel_id, competitor_id, stat_name, stat, resolved_name,
            interaction.user.id,
        )
    botlog.event(f"✅ Tracked (manual, tennis prop): **{player}** {stat} in <#{interaction.channel_id}>, by **{interaction.user}**")


async def _playerprops_soccer(interaction: discord.Interaction, player: str, stat: str):
    """Soccer-only equivalent of /playerprops' ESPN-backed body, using
    365scores instead (see soccerpropstracker.py)."""
    stat_name = stat if (stat in scores365.SOCCER_STAT_CATALOG or stat in playerstatsfootball.STAT_CATALOG) else None
    if not stat_name:
        await interaction.followup.send(
            f"Unknown stat '{stat}' for Soccer - pick one from the autocomplete list.", ephemeral=True
        )
        return

    try:
        result = await asyncio.to_thread(scores365.find_soccer_player, player)
    except scores365.ScoresError as e:
        await interaction.followup.send(f"Couldn't reach 365scores: {e}", ephemeral=True)
        return
    if not result:
        await interaction.followup.send(
            f"No live or imminent (within 2h) match found with a player named **{player}**.", ephemeral=True
        )
        return
    game, member = result
    game_id, member_id, member_competitor_id = game["id"], member["id"], member.get("competitorId")
    resolved_name = member.get("name", player)
    photo_url = scores365.athlete_photo_url(member)

    fixture_path, psf_match = await _resolve_soccer_psf_match(game, stat_name)
    if stat_name in playerstatsfootball.STAT_CATALOG and not fixture_path:
        await interaction.followup.send(
            f"Found **{resolved_name}**'s match, but couldn't find it on our extended stats source for {stat}.",
            ephemeral=True,
        )
        return

    embed, file = await soccerpropstracker.build_embed(
        game, member_id, member_competitor_id, resolved_name, photo_url, stat, stat_name, psf_match=psf_match,
    )
    message = await interaction.followup.send(embed=embed, file=file, wait=True)

    # interaction followup messages are bound to a webhook token that expires
    # after ~15 minutes; re-fetch as a plain channel message so edits keep
    # working for the entire tracking duration.
    message = await interaction.channel.fetch_message(message.id)
    embed.set_footer(text=soccerpropstracker._footer_text(message.id))
    await throttle.run(interaction.channel_id, lambda: message.edit(embed=embed))
    soccerpropstracker.register_message(message.id, interaction.channel_id, game_id, member_id, stat_name, interaction.user.id)
    await _safe_add_trash_reaction(message)

    if not scores365.is_finished(game):
        soccerpropstracker.start_tracking(
            message, game_id, interaction.channel_id, member_id, member_competitor_id, stat_name, photo_url,
            stat, resolved_name, interaction.user.id, fixture_path=fixture_path,
        )
    botlog.event(f"✅ Tracked (manual, soccer prop): **{player}** {stat} in <#{interaction.channel_id}>, by **{interaction.user}**")


@tree.command(name="playerprops", description="Track a player's live stat, e.g. Points, Earned Runs, Aces")
@app_commands.describe(
    sport="Sport to search in",
    player="Player name, e.g. Jameson Taillon",
    stat="Stat to track",
)
@app_commands.choices(sport=SPORT_CHOICES)
@app_commands.autocomplete(stat=stat_autocomplete)
async def playerprops(interaction: discord.Interaction, sport: app_commands.Choice[str], player: str, stat: str):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, sport=sport.name, player=player, stat=stat)
    await interaction.response.defer()

    if sport.value == "tennis":
        await _playerprops_tennis(interaction, player, stat)
        return

    if sport.value == "soccer":
        await _playerprops_soccer(interaction, player, stat)
        return

    if sport.value not in espn.SPORT_PATHS:
        await interaction.followup.send(
            f"{sport.name} isn't supported for /playerprops yet - only Baseball, Basketball, Hockey, NFL, Tennis, and Soccer for now.",
            ephemeral=True,
        )
        return

    stat_key = espn.STAT_CATALOG.get(sport.value, {}).get(stat)
    if not stat_key:
        await interaction.followup.send(
            f"Unknown stat '{stat}' for {sport.name} - pick one from the autocomplete list.", ephemeral=True
        )
        return

    try:
        entity = await asyncio.to_thread(espn.find_player, player, sport.value)
    except espn.EspnError as e:
        await interaction.followup.send(f"Couldn't reach ESPN: {e}", ephemeral=True)
        return
    if not entity:
        await interaction.followup.send(f"Couldn't find a {sport.name} player named **{player}**.", ephemeral=True)
        return

    event_id = await asyncio.to_thread(espn.find_current_event_id, sport.value, entity["team_id"])
    if not event_id:
        await interaction.followup.send(f"No live or recent match found for **{entity['name']}**.", ephemeral=True)
        return
    event = await asyncio.to_thread(espn.get_event, sport.value, event_id)
    if not event:
        await interaction.followup.send(f"Couldn't fetch match data for **{entity['name']}**.", ephemeral=True)
        return

    current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity["id"], stat_key)
    embed, file = await proptracker.build_embed(
        entity["name"], entity["id"], entity["photo_url"], sport.value, stat, current_value, is_home, team, event,
        known_team_name=entity["team_name"],
    )
    message = await interaction.followup.send(embed=embed, file=file, wait=True)

    # interaction followup messages are bound to a webhook token that expires
    # after ~15 minutes; re-fetch as a plain channel message so edits keep
    # working for the entire tracking duration.
    message = await interaction.channel.fetch_message(message.id)
    embed.set_footer(text=proptracker._footer_text(message.id))
    await throttle.run(interaction.channel_id, lambda: message.edit(embed=embed))
    proptracker.register_message(message.id, interaction.channel_id, event_id, entity["id"], stat_key, interaction.user.id)
    await _safe_add_trash_reaction(message)

    if not espn.is_finished(event):
        proptracker.start_tracking(
            message, interaction.channel_id, event_id, entity["id"], entity["team_id"], entity["photo_url"],
            sport.value, stat_key, stat, entity["name"], interaction.user.id,
            known_team_name=entity["team_name"],
        )
    botlog.event(f"✅ Tracked (manual, prop): **{player}** {stat} ({sport.name}) in <#{interaction.channel_id}>, by **{interaction.user}**")


def _untrack_one(channel_id: int, game_id: str, player: Optional[str]) -> list[str]:
    """Stops every tracker (match/total/F5/prop/1st-inning/1st-set) matching
    this one game_id in this channel. Returns what was actually stopped, if
    anything."""
    stopped = []
    for entry in tracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if tracker.stop_tracking(
            channel_id, entry["game_id"], entry.get("picked_team"), entry.get("team_total"),
            entry.get("total_direction"), entry.get("total_line"),
        ):
            stopped.append("moneyline/total pick")

    for entry in f5tracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if f5tracker.stop_tracking(
            channel_id, entry["game_id"], entry.get("picked_team"), entry.get("total_direction"),
            entry.get("total_line"), entry.get("handicap_line"),
        ):
            stopped.append("F5 pick")

    if inning1tracker.stop_tracking(channel_id, game_id):
        stopped.append("1st inning result pick")

    if doublechancetracker.stop_tracking(channel_id, game_id):
        stopped.append("double chance pick")

    for entry in settracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and entry.get("team") and player.lower() not in entry["team"].lower():
            continue
        if settracker.stop_tracking(channel_id, entry["game_id"], entry["market"], entry.get("team")):
            stopped.append(f"{entry['market']} pick")

    for entry in ufctracker.list_tracked_details(channel_id):
        if str(entry["competition_id"]) != str(game_id):
            continue
        if ufctracker.stop_tracking(
            channel_id, entry["competition_id"], entry.get("fighter_id"),
            entry.get("total_direction"), entry.get("total_line"),
        ):
            stopped.append("UFC pick")

    for entry in boxingtracker.list_tracked_details(channel_id):
        if str(entry["fight_id"]) != str(game_id):
            continue
        if boxingtracker.stop_tracking(channel_id, entry["fight_id"], entry["fighter_id"]):
            stopped.append("Boxing pick")

    for entry in proptracker.list_tracked_details(channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        stat_key = tuple(entry["stat_key"])
        if proptracker.stop_tracking(
            channel_id, entry["event_id"], entry["entity_id"], stat_key, entry.get("direction"), entry.get("line"),
        ):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

    for entry in inningtracker.list_tracked_details(channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if inningtracker.stop_tracking(channel_id, entry["event_id"], entry["pick_type"], entry.get("line")):
            stopped.append(entry["pick_type"])

    for entry in inningtotaltracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if inningtotaltracker.stop_tracking(channel_id, entry["game_id"], entry["pick_type"], entry.get("line")):
            stopped.append(entry["pick_type"])

    for entry in tennispropstracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        if tennispropstracker.stop_tracking(
            channel_id, entry["game_id"], entry["competitor_id"], entry["stat_name"], entry.get("direction"), entry.get("line"),
        ):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

    for entry in soccerpropstracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        if soccerpropstracker.stop_tracking(
            channel_id, entry["game_id"], entry["member_id"], entry["stat_name"], entry.get("direction"), entry.get("line"),
        ):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

    # No numeric game_id exists for esports (hawk.live/GosuGamers have none
    # to give one) - matched against the "TeamA v TeamB" id_label shown by
    # /tracked instead (see _gather_tracked_items), substring/case-
    # insensitive same as the player-name matching above.
    for entry in esportstracker.list_tracked_details(channel_id):
        id_label = f"{entry['team_a']} v {entry['team_b']}"
        if game_id.lower() not in id_label.lower():
            continue
        if player and entry.get("picked_team") and player.lower() not in entry["picked_team"].lower():
            continue
        if esportstracker.stop_tracking(channel_id, entry["sport"], entry["team_a"], entry["team_b"], entry["market"]):
            stopped.append(f"{entry['market']} pick")

    for entry in htfttracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["ht_team"].lower() and player.lower() not in entry["ft_team"].lower():
            continue
        if htfttracker.stop_tracking(channel_id, entry["game_id"], entry["ht_team"], entry["ft_team"]):
            stopped.append("HT/FT pick")

    return stopped


def _posted_ts(message_id: int) -> int:
    """Discord message IDs are snowflakes that already encode their creation
    time - no need to persist a separate 'tracked since' timestamp anywhere,
    every tracker already stores message_id."""
    return int(discord.utils.snowflake_time(message_id).timestamp())


class _UntrackSelect(discord.ui.Select):
    """One dropdown covering up to 25 tracked items - see UntrackView for how
    more than 25 are split across multiple dropdowns (a Select's own option
    list is capped at 25 by Discord)."""

    def __init__(self, indexed_items: list[tuple[int, dict]]):
        options = [
            discord.SelectOption(
                label=item["label"][:100],
                value=str(i),
                description=(f"ID {item['id_label']} • posted "
                             f"{discord.utils.snowflake_time(item['message_id']).strftime('%b %d, %I:%M %p UTC')}")[:100],
            )
            for i, item in indexed_items
        ]
        super().__init__(placeholder="Select tracked pick(s) to untrack...", min_values=1, max_values=len(options), options=options)
        self._by_value = {str(i): item for i, item in indexed_items}

    async def callback(self, interaction: discord.Interaction):
        lines = []
        for value in self.values:
            item = self._by_value[value]
            stopped = item["stop"]()
            lines.append(f"{'🗑️' if stopped else '⚠️'} {item['label']} — {'untracked' if stopped else 'already gone'}")
            if stopped:
                botlog.event(f"🗑️ Untracked (manual, /tracked dropdown): {item['label']} — by **{interaction.user}**")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class UntrackView(discord.ui.View):
    """Lets /tracked's ephemeral listing be untracked by picking from a
    dropdown instead of copy-pasting game IDs into /untrack. Chunks into
    multiple dropdowns (a View allows up to 5 components) if there are more
    than 25 tracked items in the channel."""

    def __init__(self, items: list[dict]):
        super().__init__(timeout=300)
        indexed = list(enumerate(items))
        for start in range(0, min(len(indexed), 125), 25):
            self.add_item(_UntrackSelect(indexed[start:start + 25]))


async def _gather_tracked_items(channel_id: int) -> list[dict]:
    """One entry per active tracker (match/total/F5/prop/1st-inning), each
    with a display label and a zero-arg 'stop' callable - shared by /tracked's
    text listing and its untrack dropdown so the two never drift apart."""
    items = []

    for entry in tracker.list_tracked_details(channel_id):
        game = await asyncio.to_thread(scores365.get_live_update, entry["sport_id"], entry["game_id"])
        if game:
            home = (game.get("homeCompetitor") or {}).get("name", "?")
            away = (game.get("awayCompetitor") or {}).get("name", "?")
            matchup = f"{home} vs {away}"
        else:
            matchup = "(couldn't fetch match info)"
        if entry.get("picked_team"):
            pick_suffix = f" — {entry['picked_team']} ML"
        elif entry.get("team_total") and entry.get("total_direction") and entry.get("total_line") is not None:
            pick_suffix = f" — {entry['team_total']} {entry['total_direction'].title()} {entry['total_line']:g}"
        elif entry.get("total_direction") and entry.get("total_line") is not None:
            pick_suffix = f" — {entry['total_direction'].title()} {entry['total_line']:g}"
        else:
            pick_suffix = ""
        items.append({
            "kind": "match", "label": f"{matchup}{pick_suffix}", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], pt=entry.get("picked_team"), tt=entry.get("team_total"),
                td=entry.get("total_direction"), tl=entry.get("total_line"): tracker.stop_tracking(cid, gid, pt, tt, td, tl),
        })

    for entry in proptracker.list_tracked_details(channel_id):
        items.append({
            "kind": "prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["event_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, eid=entry["event_id"], enid=entry["entity_id"], sk=tuple(entry["stat_key"]),
                d=entry.get("direction"), l=entry.get("line"): proptracker.stop_tracking(cid, eid, enid, sk, d, l),
        })

    for entry in inningtracker.list_tracked_details(channel_id):
        items.append({
            "kind": "inning", "label": inningtracker._pick_label(entry["pick_type"], entry.get("line")), "id_label": entry["event_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, eid=entry["event_id"], pt=entry["pick_type"], ln=entry.get("line"):
                inningtracker.stop_tracking(cid, eid, pt, ln),
        })

    for entry in inningtotaltracker.list_tracked_details(channel_id):
        items.append({
            "kind": "inning_total", "label": inningtotaltracker._pick_label(entry["pick_type"], entry.get("line")), "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], pt=entry["pick_type"], ln=entry.get("line"):
                inningtotaltracker.stop_tracking(cid, gid, pt, ln),
        })

    for entry in f5tracker.list_tracked_details(channel_id):
        if entry.get("picked_team") and entry.get("handicap_line") is not None:
            pick_label = f"{entry['picked_team']} F5 {entry['handicap_line']:+g}"
        elif entry.get("picked_team") and entry.get("total_direction") and entry.get("total_line") is not None:
            pick_label = f"{entry['picked_team']} F5 {entry['total_direction'].title()} {entry['total_line']:g}"
        elif entry.get("total_direction") and entry.get("total_line") is not None:
            pick_label = f"F5 {entry['total_direction'].title()} {entry['total_line']:g}"
        else:
            pick_label = f"{entry['picked_team']} F5 ML"
        items.append({
            "kind": "f5", "label": pick_label, "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], pt=entry.get("picked_team"), td=entry.get("total_direction"),
                tl=entry.get("total_line"), hl=entry.get("handicap_line"): f5tracker.stop_tracking(cid, gid, pt, td, tl, hl),
        })

    for entry in inning1tracker.list_tracked_details(channel_id):
        pick_label = "Draw" if entry["pick"].upper() == "DRAW" else entry["pick"]
        items.append({
            "kind": "inning1", "label": f"1st Inning: {pick_label}", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"]: inning1tracker.stop_tracking(cid, gid),
        })

    for entry in doublechancetracker.list_tracked_details(channel_id):
        items.append({
            "kind": "double_chance", "label": f"Double Chance: {doublechancetracker.pick_label(tuple(entry['covered']))}",
            "id_label": entry["game_id"], "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"]: doublechancetracker.stop_tracking(cid, gid),
        })

    for entry in settracker.list_tracked_details(channel_id):
        label = settracker.pick_label(entry["market"], entry.get("team"), entry.get("direction"), entry.get("line"))
        items.append({
            "kind": "set1", "label": label, "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], m=entry["market"], t=entry.get("team"): settracker.stop_tracking(cid, gid, m, t),
        })

    for entry in tennispropstracker.list_tracked_details(channel_id):
        items.append({
            "kind": "tennis_prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], comp=entry["competitor_id"], sn=entry["stat_name"],
                d=entry.get("direction"), l=entry.get("line"): tennispropstracker.stop_tracking(cid, gid, comp, sn, d, l),
        })

    for entry in soccerpropstracker.list_tracked_details(channel_id):
        items.append({
            "kind": "soccer_prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], mid=entry["member_id"], sn=entry["stat_name"],
                d=entry.get("direction"), l=entry.get("line"): soccerpropstracker.stop_tracking(cid, gid, mid, sn, d, l),
        })

    for entry in ufctracker.list_tracked_details(channel_id):
        if entry.get("fighter_name"):
            pick_label = f"{entry['fighter_name']} ML"
        elif entry.get("total_direction") and entry.get("total_line") is not None:
            pick_label = f"Fight {entry['total_direction'].title()} {entry['total_line']:g} Rounds"
        else:
            pick_label = "UFC pick"
        items.append({
            "kind": "ufc", "label": pick_label, "id_label": entry["competition_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, compid=entry["competition_id"], fid=entry.get("fighter_id"),
                td=entry.get("total_direction"), tl=entry.get("total_line"): ufctracker.stop_tracking(cid, compid, fid, td, tl),
        })

    for entry in boxingtracker.list_tracked_details(channel_id):
        items.append({
            "kind": "boxing", "label": f"{entry['fighter_name']} ML", "id_label": entry["fight_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, fid=entry["fight_id"], fighter=entry["fighter_id"]:
                boxingtracker.stop_tracking(cid, fid, fighter),
        })

    for entry in kboproptracker.list_tracked_details(channel_id):
        items.append({
            "kind": "kbo_prop",
            "label": f"{entry['player_name']} {entry['direction'].title()} {entry['line']:g} {entry['stat_label']}",
            "id_label": entry["pcode"], "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, pc=entry["pcode"], sl=entry["stat_label"], d=entry["direction"],
                l=entry["line"], td=entry["target_date"]: kboproptracker.stop_tracking(cid, pc, sl, d, l, td),
        })

    for entry in esportstracker.list_tracked_details(channel_id):
        label = esportstracker.pick_label(
            entry["market"], entry.get("picked_team"), entry.get("direction"), entry.get("line"),
            entry.get("map_number"), entry.get("picked_maps"), entry.get("other_maps"),
        )
        items.append({
            "kind": "esports", "label": f"{entry['team_a']} v {entry['team_b']} — {label}",
            "id_label": f"{entry['team_a']} v {entry['team_b']}",
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, sp=entry["sport"], ta=entry["team_a"], tb=entry["team_b"], m=entry["market"]:
                esportstracker.stop_tracking(cid, sp, ta, tb, m),
        })

    for entry in htfttracker.list_tracked_details(channel_id):
        ht_team, ft_team = entry["ht_team"], entry["ft_team"]
        pick_label = f"{ht_team} Halftime/Fulltime" if ht_team == ft_team else f"{ht_team}/{ft_team} Halftime/Fulltime"
        items.append({
            "kind": "htft", "label": pick_label, "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], ht=ht_team, ft=ft_team: htfttracker.stop_tracking(cid, gid, ht, ft),
        })

    return items


@tree.command(name="untrack", description="Stop auto-updating one or more tracked matches/player props in this channel")
@app_commands.describe(
    game_id="Game ID(s) shown by /tracked - separate multiple with commas or spaces",
    player="Player/team name - disambiguates among active trackers under game_id, "
           "or (with game_id omitted) cancels a still-QUEUED pick that hasn't found its match yet",
)
async def untrack(interaction: discord.Interaction, game_id: Optional[str] = None, player: Optional[str] = None):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, game_id=game_id, player=player)

    if not game_id and not player:
        await interaction.response.send_message("Provide a game ID, a player/team name, or both.", ephemeral=True)
        return

    lines = []
    if not game_id:
        # No game_id at all - nothing actively tracked to look up, so
        # player is instead a filter over pendingauto's still-queued picks
        # (never found a match yet, so they have no game_id to give).
        matches = pendingauto.find_matching(interaction.channel_id, player)
        if not matches:
            await interaction.response.send_message(f"No queued pick matching **{player}** in this channel.", ephemeral=True)
            return
        cancelled = []
        for entry_id, entry in matches:
            if pendingauto.cancel(entry_id):
                cancelled.append(pendingauto.display_name(entry["payload"]))
        botlog.event(f"🗑️ Cancelled {len(cancelled)} queued pick(s) (manual /untrack): {', '.join(cancelled)} — by **{interaction.user}**")
        await interaction.response.send_message(f"Cancelled {len(cancelled)} queued pick(s): {', '.join(cancelled)}", ephemeral=True)
        return

    game_ids = [gid for gid in re.split(r"[,\s]+", game_id.strip()) if gid]
    for gid in game_ids:
        stopped = _untrack_one(interaction.channel_id, gid, player)
        if stopped:
            lines.append(f"`{gid}` — stopped: {', '.join(stopped)}")
            botlog.event(f"🗑️ Untracked (manual /untrack): `{gid}` — {', '.join(stopped)} — by **{interaction.user}**")
        else:
            lines.append(f"`{gid}` — nothing found")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


_SECTION_TITLES = {
    "match": "Tracked matches",
    "prop": "Tracked player props",
    "inning": "Tracked 1st-inning picks",
    "f5": "Tracked F5 (1st 5 innings) picks",
    "inning1": "Tracked 1st inning result picks",
    "set1": "Tracked tennis extra-market picks",
    "tennis_prop": "Tracked tennis player props",
    "soccer_prop": "Tracked soccer player props",
    "ufc": "Tracked UFC picks",
    "esports": "Tracked Dota 2 / CS2 picks",
}


@tree.command(name="tracked", description="List matches and player props currently being tracked in this channel")
async def tracked(interaction: discord.Interaction):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)
    items = await _gather_tracked_items(interaction.channel_id)
    if not items:
        await interaction.followup.send("Nothing is being tracked in this channel.", ephemeral=True)
        return

    sections = []
    for kind, title in _SECTION_TITLES.items():
        lines = [
            f"- `{item['id_label']}` — {item['label']} • posted <t:{_posted_ts(item['message_id'])}:R>"
            for item in items if item["kind"] == kind
        ]
        if lines:
            sections.append(f"**{title}:**\n" + "\n".join(lines))

    view = UntrackView(items)
    await interaction.followup.send("\n\n".join(sections), view=view, ephemeral=True)


class _PendingDeleteSelect(discord.ui.Select):
    """One dropdown covering up to 25 queued cards - see PendingDeleteView
    for how more than 25 are split across multiple dropdowns."""

    def __init__(self, indexed_entries: list[tuple[int, dict]]):
        options = [
            discord.SelectOption(
                label=(entry.get("label") or "(no description)").replace("\n", " • ")[:100],
                value=str(i),
                description=f"deletes <t:{int(entry['delete_at'])}:R>"[:100],
            )
            for i, entry in indexed_entries
        ]
        super().__init__(placeholder="Select card(s) to delete now...", min_values=1, max_values=len(options), options=options)
        self._by_value = {str(i): entry for i, entry in indexed_entries}

    async def callback(self, interaction: discord.Interaction):
        lines = []
        for value in self.values:
            entry = self._by_value[value]
            label = (entry.get("label") or "(no description)").replace("\n", " • ")
            ok = await pendingdelete.delete_now(interaction.client, entry)
            lines.append(f"{'🗑️' if ok else '⚠️'} {label} — {'deleted' if ok else 'already gone'}")
            if ok:
                botlog.event(f"🗑️ Deleted now (manual, /pending): {label} in <#{entry['channel_id']}> — by **{interaction.user}**")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class PendingDeleteView(discord.ui.View):
    """Lets /pending's listing delete a card immediately instead of waiting
    out its timer. Chunks into multiple dropdowns (a View allows up to 5
    components) if there are more than 25 queued cards."""

    def __init__(self, entries: list[dict]):
        super().__init__(timeout=300)
        indexed = list(enumerate(entries))
        for start in range(0, min(len(indexed), 125), 25):
            self.add_item(_PendingDeleteSelect(indexed[start:start + 25]))


@tree.command(name="pending", description="List cards waiting out their post-result delete timer (only usable in the logs channel)")
async def pending(interaction: discord.Interaction):
    if interaction.channel_id != botlog.LOG_CHANNEL_ID:
        await interaction.response.send_message(f"This command only works in <#{botlog.LOG_CHANNEL_ID}>.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    entries = pendingdelete.list_pending()
    if not entries:
        await interaction.followup.send("Nothing is currently waiting to be deleted.", ephemeral=True)
        return

    entries.sort(key=lambda e: e["delete_at"])
    lines = [
        f"- {(entry.get('label') or '(no description)').replace(chr(10), ' • ')} "
        f"— <#{entry['channel_id']}> — deletes <t:{int(entry['delete_at'])}:R>"
        for entry in entries
    ]
    view = PendingDeleteView(entries)
    await interaction.followup.send("\n".join(lines), view=view, ephemeral=True)


_PARLAY_ACTION_CHOICES = [
    app_commands.Choice(name="Create", value="create"),
    app_commands.Choice(name="Add legs", value="add"),
    app_commands.Choice(name="Remove legs", value="remove"),
    app_commands.Choice(name="Resolve legs", value="resolve"),
    app_commands.Choice(name="Delete", value="delete"),
    app_commands.Choice(name="List", value="list"),
]
_PARLAY_RESULT_CHOICES = [
    app_commands.Choice(name="Won", value="won"),
    app_commands.Choice(name="Lost", value="lost"),
    app_commands.Choice(name="Push", value="push"),
    app_commands.Choice(name="Void", value="void"),
]


@tree.command(name="parlay", description="Manually manage a parlay group by pasting each leg's card ID from its footer")
@app_commands.describe(
    action="What to do",
    identifier=f"Parlay name, max {parlaytracker.MAX_IDENTIFIER_LENGTH} characters (used for create/add/remove/resolve)",
    ids="Comma-separated card IDs from each card's footer (used for add/remove/resolve)",
    result="What each leg resulted in - only used for Resolve legs, e.g. when a leg's own tracker can't finish grading it",
)
@app_commands.choices(action=_PARLAY_ACTION_CHOICES, result=_PARLAY_RESULT_CHOICES)
async def parlay(
    interaction: discord.Interaction, action: app_commands.Choice[str],
    identifier: Optional[str] = None, ids: Optional[str] = None, result: Optional[app_commands.Choice[str]] = None,
):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    # Every group lookup is an exact-match dict key (_key(channel_id,
    # identifier) in parlaytracker.py) - untrimmed, incidental leading/
    # trailing whitespace (easy to introduce copy-pasting an identifier
    # from a prior reply, or just a stray space while typing) would
    # silently create/look up a DIFFERENT group than the one intended,
    # with no error to explain why. Stripped once here so every downstream
    # action (create/add/remove/resolve/delete/list) sees the same
    # normalized identifier.
    if identifier is not None:
        identifier = identifier.strip()
    _log_command(interaction, action=action.name, identifier=identifier, ids=ids, result=result.name if result else None)
    await interaction.response.defer(ephemeral=True)

    if action.value == "list":
        groups = parlaytracker.list_groups(interaction.channel_id)
        if not groups:
            await interaction.followup.send("No active parlays in this channel.", ephemeral=True)
            return
        lines = [
            f"- **{g['identifier']}** — {len(g.get('legs', {}))} leg(s), "
            f"{g['resolved_legs']}/{g['total_legs'] + g['voided']} resolved"
            for g in groups
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    if not identifier:
        await interaction.followup.send("`identifier` is required for this action.", ephemeral=True)
        return

    if action.value == "create":
        error = await parlaytracker.create_group(interaction.channel_id, identifier)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        await interaction.followup.send(
            f"Created parlay **{identifier}**. Add legs with "
            f"`/parlay action:Add legs identifier:{identifier} ids:<card id>, <card id>, ...`",
            ephemeral=True,
        )
        botlog.event(f"🎟️ Parlay **{identifier}** created in <#{interaction.channel_id}> by **{interaction.user}**")
        return

    if action.value == "delete":
        summary = await parlaytracker.delete_group(interaction.channel_id, identifier)
        botlog.event(f"🎟️ Parlay **{identifier}** (Delete) in <#{interaction.channel_id}>: {summary} — by **{interaction.user}**")
        await interaction.followup.send(summary, ephemeral=True)
        return

    if not ids:
        await interaction.followup.send("`ids` (comma-separated card IDs) is required for this action.", ephemeral=True)
        return
    raw_ids = [part.strip() for part in ids.split(",") if part.strip()]
    message_ids: list[int] = []
    invalid = []
    for raw in raw_ids:
        try:
            message_ids.append(int(raw))
        except ValueError:
            invalid.append(raw)
    if invalid:
        await interaction.followup.send(f"Not a valid card ID (must be numeric): {', '.join(invalid)}", ephemeral=True)
        return
    if not message_ids:
        await interaction.followup.send("No valid card IDs given.", ephemeral=True)
        return

    if action.value == "resolve":
        if not result:
            await interaction.followup.send("`result` is required for this action.", ephemeral=True)
            return
        summary = await parlaytracker.set_leg_result(interaction.channel, interaction.channel_id, identifier, message_ids, result.value)
    elif action.value == "add":
        summary = await parlaytracker.add_legs(interaction.channel, interaction.channel_id, identifier, message_ids)
    else:  # remove
        summary = await parlaytracker.remove_legs(interaction.channel, interaction.channel_id, identifier, message_ids)
    botlog.event(f"🎟️ Parlay **{identifier}** ({action.name}) in <#{interaction.channel_id}>: {summary} — by **{interaction.user}**")
    await interaction.followup.send(summary, ephemeral=True)


def _summary_route(interaction_channel_id: int) -> Optional[config.SummaryRoute]:
    """/summary only runs in a channel configured as an invoke channel in
    config.SUMMARY_ROUTES; the returned route carries both every picks-source
    channel whose dailylog entries get combined into the report (.origins)
    and where the report actually gets posted (.post_channel_id - usually
    the same invoke channel, but can be a different shared channel). None
    means this channel isn't a configured invoke channel at all, regardless
    of ALLOWED_CHANNEL_ID - /summary has its own, separate channel
    restriction from every other command."""
    return config.SUMMARY_ROUTES.get(interaction_channel_id)


async def _reject_summary_wrong_channel(interaction: discord.Interaction):
    # Deliberately doesn't list config.SUMMARY_ROUTES' channels - whoever
    # runs this command in the wrong place isn't necessarily someone who
    # should even know which other channels this bot operates in (e.g. a
    # picks-source admin with no visibility into the destination channels).
    await interaction.response.send_message("Unable to use this command in this channel.", ephemeral=True)


def _summary_status_line(entry: dict) -> str:
    """Never blank, per the report's whole point: a resolved pick gets its
    win/loss/push/void mark; anything still pending gets a neutral mark plus
    whatever live detail its tracker last reported (LIVE/Not Started/an
    actual Postponed), so a reader can see *why* it has no result yet
    instead of the line just vanishing or looking unfinished. Distinct from
    the "⏸️ Postponed" branch below, which is a real postponement (rain
    delay, etc.) reported mid-tracking - a pick that just hasn't kicked off
    yet reads as "Not Started" instead, not conflated with an actual
    postponement."""
    if dailylog.is_final(entry["status"]):
        line = f"{dailylog.result_mark(entry['status'])} {entry['label']}"
        if entry["status"] == "push":
            line += " — Push - Tie"
        elif entry["status"] == "void" and entry["detail"].startswith("VOID - "):
            # A bare "Voided" with no explanation was confirmed live to be
            # confusing - postponed, interrupted, cancelled, rescheduled,
            # and manually-untracked picks all used to collapse into the
            # exact same mark with no way to tell them apart (see
            # dailylog.record_result's reason param).
            line += f" — Void - {entry['detail'][len('VOID - '):]}"
        return line
    detail = entry["detail"]
    if detail.startswith("LIVE"):
        mark = "🟡"
    elif detail.startswith("⏸️"):
        mark, detail = "⏸️", detail[2:].strip()
    elif detail.startswith("NOT STARTED"):
        mark, detail = "⏸️", "Not Started" + detail[len("NOT STARTED"):]
    else:
        mark = "⏳"
    return f"{mark} {entry['label']} — {detail}"


def _win_rate_line(picks_list: list[dict]) -> str:
    """Won/lost decisions only - push, void (which also covers postponed
    and interrupted-never-resumed picks, see dailylog.record_result call
    sites) and anything still pending don't count as either a win or a
    loss, so they're excluded from both the numerator and denominator."""
    won = sum(1 for e in picks_list if e["status"] == "won")
    lost = sum(1 for e in picks_list if e["status"] == "lost")
    decided = won + lost
    if decided == 0:
        return "**Win Rate:** —"
    return f"**Win Rate:** {won}-{lost} ({won / decided:.1%})"


def _normalize_summary_section(section: str) -> str:
    """Folds any "<Sport> Props" picks-channel header into its plain
    "<Sport>" counterpart (e.g. "WNBA Props" -> "WNBA", "MLB Props" -> "MLB")
    so props and non-props picks for the same sport land in one section
    instead of two - section text is whatever GreenFox's header literally
    says, not a fixed enum, so this is a suffix rule rather than a lookup
    table of every sport."""
    if section.endswith(" Props") and len(section) > len(" Props"):
        return section[: -len(" Props")]
    return section


_EMBED_DESCRIPTION_LIMIT = 4096


def _pack_summary_blocks_into_embeds(date_str: str, blocks: list[str]) -> list[discord.Embed]:
    """Packs description blocks (one per section, plus the win-rate line
    always last) into as few embeds as fit under Discord's 4096-char
    description limit each, splitting between blocks first and only
    falling back to splitting within a block (by line) if a single block
    alone doesn't fit. A big slate used to just get hard-sliced to 4096
    chars and silently lose whatever came after - including the Win Rate
    line, always appended last, the single most load-bearing line in the
    whole report."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= _EMBED_DESCRIPTION_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= _EMBED_DESCRIPTION_LIMIT:
            current = block
            continue
        # A single section's own block is bigger than the whole limit -
        # split it line by line instead (still never truncates content).
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= _EMBED_DESCRIPTION_LIMIT:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line
    if current:
        chunks.append(current)

    embeds = []
    for i, description in enumerate(chunks):
        title = f"Summary Report ({date_str})" if i == 0 else f"Summary Report ({date_str}) (cont'd)"
        embeds.append(discord.Embed(title=title, description=description, color=0x2B2D31))
    return embeds


def _build_summary_embeds(date_str: str, picks_list: list[dict]) -> list[discord.Embed]:
    sections: dict[str, list[dict]] = {}
    for entry in picks_list:
        # Group by each tracker's own canonical sport label (dailylog.
        # record_pick's "sport" param) when present - independent of
        # whatever raw header text the picks-source message used, so the
        # same market (e.g. YRFI/NRFI) always lands in one section even
        # when different providers label it differently ("YRFI/NRFI Slate"
        # vs. a plain "MLB" header). Falls back to the old text-based
        # normalization for entries logged before this field existed.
        section = entry.get("sport") or _normalize_summary_section(entry["section"])
        sections.setdefault(section, []).append(entry)

    blocks = []
    for section, entries in sections.items():
        lines = [_summary_status_line(e) for e in entries]
        blocks.append(f"**{section}**\n" + "\n".join(lines))
    blocks.append(_win_rate_line(picks_list))

    return _pack_summary_blocks_into_embeds(date_str, blocks)


class SummaryPostView(discord.ui.View):
    """Lets /summary's ephemeral preview actually get posted, or dropped,
    without a second command invocation. Re-reads dailylog at click time
    (not the preview-time snapshot) since a pending pick can resolve in the
    time between preview and click - stale data here could show a result
    that's since gone final. Posting (or reposting) a date is entirely the
    caller's call - nothing here prevents publishing the same date twice."""

    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, date_strs: list[str], requester_id: int):
        super().__init__(timeout=900)
        self.origin_ids = origin_ids
        self.post_channel_id = post_channel_id
        self.date_strs = date_strs
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /summary can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        embeds = []
        posted_dates = []
        for date_str in self.date_strs:
            picks_list = dailylog.picks_for_date(self.origin_ids, date_str)
            if not picks_list:
                continue
            embeds.extend(_build_summary_embeds(date_str, picks_list))
            posted_dates.append(date_str)
        if not embeds:
            await interaction.response.edit_message(
                content="Nothing to post — no picks logged for the selected date(s).", embed=None, view=None,
            )
            self.stop()
            return
        target = client.get_channel(self.post_channel_id) or await client.fetch_channel(self.post_channel_id)
        # Discord caps a single message at 10 embeds - a big multi-date (or
        # single very long) report can now produce more than that (see
        # _build_summary_embeds), so send in batches of 10 rather than
        # truncating and dropping content the same way the old hard slice
        # to 4096 chars used to.
        for i in range(0, len(embeds), 10):
            await target.send(embeds=embeds[i : i + 10])
        dates_label = ", ".join(posted_dates)
        botlog.event(
            f"📋 Summary report ({dates_label}) posted to <#{self.post_channel_id}> "
            f"(previewed in <#{interaction.channel_id}>) by **{interaction.user}**"
        )
        await interaction.response.edit_message(content="Posted.", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing posted.", embed=None, view=None)
        self.stop()


async def _send_summary_preview(
    interaction: discord.Interaction, origin_ids: tuple[int, ...], post_channel_id: int, date_strs: list[str], *, edit: bool,
):
    """Renders the date-picker's selection (one or more dates) as a
    preview - re-reads dailylog at click time (not the picker's own
    snapshot) since a pending pick can resolve, or someone else can
    post/re-preview the same date, in the time between opening the picker
    and selecting a date."""
    embeds = [e for d in date_strs if (picks := dailylog.picks_for_date(origin_ids, d)) for e in _build_summary_embeds(d, picks)]
    if not embeds:
        content, view = f"No picks logged for {', '.join(date_strs)}.", None
    else:
        content = (
            "Preview only - not posted yet. Click below to publish it."
            if post_channel_id == interaction.channel_id
            else f"Preview only - not posted yet. Click below to publish it to <#{post_channel_id}>."
        )
        if len(embeds) > 10:
            # A single interaction response can only carry 10 embeds - the
            # actual post (SummaryPostView.post) isn't limited this way, it
            # batches into multiple messages, so this only trims the
            # preview itself, never what actually gets posted.
            content += f" (preview truncated to 10 of {len(embeds)} embeds - the full report will still post in full)"
            embeds = embeds[:10]
        view = SummaryPostView(origin_ids, post_channel_id, date_strs, interaction.user.id)
    if edit:
        await interaction.response.edit_message(content=content, embeds=embeds, view=view)
    else:
        await interaction.followup.send(content=content, embeds=embeds, view=view, ephemeral=True)


class _SummaryDateSelect(discord.ui.Select):
    """One option per date that has any picks logged for this route
    (including already-posted dates - reposting is always available, it's
    just up to whoever runs the command) - the closest Discord components
    get to an actual calendar widget (there's no native date-picker
    component for bots). Multiple dates can be selected at once (up to
    Discord's 10-embeds-per-message cap); the resulting preview/post covers
    all of them together."""

    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, dates: list[str], requester_id: int):
        options = [
            discord.SelectOption(
                label=d, description=f"{len(dailylog.picks_for_date(origin_ids, d))} pick(s) logged",
            )
            for d in dates
        ]
        super().__init__(
            placeholder="Pick one or more dates to preview...", min_values=1, max_values=min(len(dates), 10), options=options,
        )
        self.origin_ids = origin_ids
        self.post_channel_id = post_channel_id
        self.dates = dates
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /summary can use this.", ephemeral=True)
            return
        selected = [d for d in self.dates if d in self.values]
        await _send_summary_preview(interaction, self.origin_ids, self.post_channel_id, selected, edit=True)


class SummaryDatePickView(discord.ui.View):
    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, dates: list[str], requester_id: int):
        super().__init__(timeout=300)
        self.add_item(_SummaryDateSelect(origin_ids, post_channel_id, dates, requester_id))


@tree.command(name="summary", description="Preview an end-of-day picks report for a date, then optionally post it")
async def summary(interaction: discord.Interaction):
    route = _summary_route(interaction.channel_id)
    if not route:
        await _reject_summary_wrong_channel(interaction)
        return
    if not _summary_allowed(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    dates = dailylog.available_dates(route.origins, limit=25)
    if not dates:
        await interaction.followup.send("No picks logged for any date yet.", ephemeral=True)
        return
    view = SummaryDatePickView(route.origins, route.post_channel_id, dates, interaction.user.id)
    await interaction.followup.send("Pick a date to preview:", view=view, ephemeral=True)


_MASTERPARLAY_STATUS_LABEL = {"won": f"{dailylog.WINMARK} Won", "lost": f"{dailylog.LOSSMARK} Lost", "pending": "⏳ Pending"}


class _MasterParlaySelect(discord.ui.Select):
    """Which of the slip's parlays actually get published - GreenFox
    routinely posts several parlays in one slip, and not every one of
    them is meant to go in the archive. Every option starts checked
    (default=True) so the existing "publish everything" behavior is just
    "leave it as-is and click Publish"; deselecting narrows it down.
    Labeled with each parlay's current outcome so that's visible without
    having to scroll back up through the preview embeds."""

    def __init__(self, parlays: list[dict]):
        options = [
            discord.SelectOption(
                label=p["name"][:100],
                # A "RECOMMENDED ..." parlay never states its own odds
                # (see masterparlay's module docstring) - no dangling
                # "• " left behind when there's nothing to show after it.
                description=(
                    f"{_MASTERPARLAY_STATUS_LABEL.get(p['status'], p['status'])}" + (f" • {p['odds']}" if p["odds"] else "")
                )[:100],
                value=str(i),
                default=True,
            )
            for i, p in enumerate(parlays)
        ]
        super().__init__(placeholder="Select which parlays to publish...", min_values=0, max_values=len(parlays), options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_indices = {int(v) for v in self.values}
        await interaction.response.defer()


class MasterParlayPublishView(discord.ui.View):
    """Lets /premiumparlay's ephemeral preview actually get published, or
    dropped. Re-fetches the source slip and re-resolves every leg fresh at
    click time (not the preview-time snapshot) - same reasoning as
    SummaryPostView/WinLossGraphPostView: a leg can resolve in the time
    between preview and click, and stale data here could publish a result
    that's since gone final wrong."""

    def __init__(self, source_message_ids: list[int], parlays: list[dict], requester_id: int):
        super().__init__(timeout=900)
        self.source_message_ids = source_message_ids
        self.requester_id = requester_id
        self.parlay_names = [p["name"] for p in parlays]
        self.selected_indices = set(range(len(parlays)))
        if parlays:
            self.add_item(_MasterParlaySelect(parlays))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /premiumparlay can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, row=1)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not self.selected_indices:
            await interaction.edit_original_response(content="No parlays selected - nothing to publish.", embeds=[], view=None)
            self.stop()
            return
        try:
            # A slip can span multiple messages (see find_latest_slip) -
            # re-fetch every piece fresh, same reasoning as the single-
            # message case, just for each one.
            sources = [await interaction.channel.fetch_message(mid) for mid in self.source_message_ids]
        except discord.HTTPException:
            await interaction.edit_original_response(content="Couldn't re-fetch the original slip - part of it may have been deleted.", embeds=[], view=None)
            self.stop()
            return
        # Matched by name rather than index, so a selection still lands on
        # the right parlay even if re-resolving nets a different ordering.
        selected_names = {self.parlay_names[i] for i in self.selected_indices if i < len(self.parlay_names)}
        date_str = masterparlay.slip_date_str(sources)
        embeds = await masterparlay.build_report(
            masterparlay.combine_slip_text(sources), date_str, only_names=selected_names,
            reference_date=datetime.date.fromisoformat(date_str),
        )
        if not embeds:
            await interaction.edit_original_response(content="Nothing to publish - the selected parlays are no longer in the slip.", embeds=[], view=None)
            self.stop()
            return
        target = client.get_channel(masterparlay.PUBLISH_CHANNEL_ID) or await client.fetch_channel(masterparlay.PUBLISH_CHANNEL_ID)
        for i in range(0, len(embeds), 10):
            await target.send(embeds=embeds[i : i + 10])
        botlog.event(
            f"🎟️ Master parlay report ({date_str}, {len(embeds)} parlay(s)) published to <#{masterparlay.PUBLISH_CHANNEL_ID}> "
            f"(previewed in <#{interaction.channel_id}>) by **{interaction.user}**"
        )
        await interaction.edit_original_response(content=f"Published {len(embeds)} parlay(s) to the {date_str} archive.", embeds=[], view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing published.", embeds=[], view=None)
        self.stop()

    @discord.ui.button(label="View Past Dates", style=discord.ButtonStyle.secondary, row=1)
    async def view_past_dates(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        archive = client.get_channel(masterparlay.PUBLISH_CHANNEL_ID) or await client.fetch_channel(masterparlay.PUBLISH_CHANNEL_ID)
        dates = await masterparlay.find_archived_dates(archive)
        if not dates:
            await interaction.edit_original_response(content="No archived parlay reports found yet.", embeds=[], view=None)
            self.stop()
            return
        view = MasterParlayArchiveDateView(dates, interaction.user.id)
        await interaction.edit_original_response(content="Pick a date to view archived parlay reports:", embeds=[], view=view)
        self.stop()


class _MasterParlayArchiveDateSelect(discord.ui.Select):
    """Mirrors _SummaryDateSelect - one option per date that has at least
    one archived parlay report, read back from the archive channel's
    footer tags (see masterparlay.find_archived_dates) rather than a
    separate index, since published reports are already the permanent
    record."""

    def __init__(self, dates: list[str], requester_id: int):
        options = [discord.SelectOption(label=d) for d in dates]
        super().__init__(placeholder="Pick a date to view...", min_values=1, max_values=1, options=options)
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /premiumparlay can use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        archive = client.get_channel(masterparlay.PUBLISH_CHANNEL_ID) or await client.fetch_channel(masterparlay.PUBLISH_CHANNEL_ID)
        date_str = self.values[0]
        embeds = await masterparlay.find_archived_report(archive, date_str)
        if not embeds:
            await interaction.edit_original_response(content=f"No archived parlay reports found for {date_str} anymore.", embeds=[], view=None)
            return
        truncated = ""
        if len(embeds) > 10:
            truncated = f" (showing 10 of {len(embeds)})"
        await interaction.edit_original_response(content=f"Archived parlay reports for {date_str}.{truncated}", embeds=embeds[:10], view=None)


class MasterParlayArchiveDateView(discord.ui.View):
    def __init__(self, dates: list[str], requester_id: int):
        super().__init__(timeout=300)
        self.add_item(_MasterParlayArchiveDateSelect(dates, requester_id))


@tree.command(name="premiumparlay", description="Preview the latest MASTER PARLAYS slip graded against tracked legs, then optionally publish it")
async def premiumparlay(interaction: discord.Interaction):
    if interaction.channel_id != masterparlay.PARLAY_SLIP_CHANNEL_ID:
        await interaction.response.send_message("This command only works in the parlay slip channel.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    messages = await masterparlay.find_latest_slip(interaction.channel)
    if not messages:
        await interaction.followup.send("No MASTER PARLAYS slip found in recent channel history.", ephemeral=True)
        return
    reference_date = datetime.date.fromisoformat(masterparlay.slip_date_str(messages))
    parlays = await masterparlay.resolve_parlays(masterparlay.combine_slip_text(messages), reference_date)
    if not parlays:
        await interaction.followup.send("Found a slip but couldn't parse any parlays from it.", ephemeral=True)
        return
    embeds = [masterparlay.build_parlay_embed(p["name"], p["odds"], p["legs"]) for p in parlays]
    truncated = ""
    if len(embeds) > 10:
        truncated = f" (preview truncated to 10 of {len(embeds)} - the selection below still covers all of them)"
    view = MasterParlayPublishView([m.id for m in messages], parlays, interaction.user.id)
    await interaction.followup.send(
        content=f"Preview only - not posted yet. Pick which parlays to publish to <#{masterparlay.PUBLISH_CHANNEL_ID}>, then click Publish.{truncated}",
        embeds=embeds[:10], view=view, ephemeral=True,
    )


def _build_winlossgraph_embed_and_file(image_bytes: bytes) -> tuple[discord.Embed, discord.File]:
    file = discord.File(io.BytesIO(image_bytes), filename="winlossgraph.png")
    embed = discord.Embed(color=0x2B2D31)
    embed.set_image(url="attachment://winlossgraph.png")
    return embed, file


class WinLossGraphPostView(discord.ui.View):
    """Same preview-then-post pattern as SummaryPostView - re-renders the
    chart at click time (not the preview-time snapshot) since a pending
    pick can resolve in the time between preview and click."""

    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, year_month: str, requester_id: int):
        super().__init__(timeout=900)
        self.origin_ids = origin_ids
        self.post_channel_id = post_channel_id
        self.year_month = year_month
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /winlossgraph can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = dailylog.daily_win_loss(self.origin_ids, self.year_month)
        if not rows:
            await interaction.response.edit_message(
                content="Nothing to post — no decided picks logged for that month.", embed=None, attachments=[], view=None,
            )
            self.stop()
            return
        image_bytes = await asyncio.to_thread(winlossgraph.render_month_chart, self.year_month, rows)
        embed, file = _build_winlossgraph_embed_and_file(image_bytes)
        target = client.get_channel(self.post_channel_id) or await client.fetch_channel(self.post_channel_id)
        await target.send(embed=embed, file=file)
        botlog.event(
            f"📊 Win/loss graph ({self.year_month}) posted to <#{self.post_channel_id}> "
            f"(previewed in <#{interaction.channel_id}>) by **{interaction.user}**"
        )
        await interaction.response.edit_message(content="Posted.", embed=None, attachments=[], view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing posted.", embed=None, attachments=[], view=None)
        self.stop()


async def _send_winlossgraph_preview(
    interaction: discord.Interaction, origin_ids: tuple[int, ...], post_channel_id: int, year_month: str, *, edit: bool,
):
    rows = dailylog.daily_win_loss(origin_ids, year_month)
    if not rows:
        content, embed, file, view = f"No decided picks logged for {year_month}.", None, None, None
    else:
        image_bytes = await asyncio.to_thread(winlossgraph.render_month_chart, year_month, rows)
        embed, file = _build_winlossgraph_embed_and_file(image_bytes)
        content = (
            "Preview only - not posted yet. Click below to publish it."
            if post_channel_id == interaction.channel_id
            else f"Preview only - not posted yet. Click below to publish it to <#{post_channel_id}>."
        )
        view = WinLossGraphPostView(origin_ids, post_channel_id, year_month, interaction.user.id)
    if edit:
        await interaction.response.edit_message(content=content, embed=embed, attachments=[file] if file else [], view=view)
    else:
        await interaction.followup.send(content=content, embed=embed, file=file, view=view, ephemeral=True)


class _WinLossGraphMonthSelect(discord.ui.Select):
    """One option per month with at least one decided (won/lost) pick
    logged for this route - the same "reachable dropdown" idea as
    _SummaryDateSelect, just grouped by month instead of by day."""

    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, months: list[str], requester_id: int):
        options = [
            discord.SelectOption(
                label=m, description=f"{len(dailylog.daily_win_loss(origin_ids, m))} day(s) with a decided pick",
            )
            for m in months
        ]
        super().__init__(placeholder="Pick a month to preview...", min_values=1, max_values=1, options=options)
        self.origin_ids = origin_ids
        self.post_channel_id = post_channel_id
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /winlossgraph can use this.", ephemeral=True)
            return
        await _send_winlossgraph_preview(interaction, self.origin_ids, self.post_channel_id, self.values[0], edit=True)


class WinLossGraphMonthPickView(discord.ui.View):
    def __init__(self, origin_ids: tuple[int, ...], post_channel_id: int, months: list[str], requester_id: int):
        super().__init__(timeout=300)
        self.add_item(_WinLossGraphMonthSelect(origin_ids, post_channel_id, months, requester_id))


@tree.command(name="winlossgraph", description="Preview a monthly win/loss rate chart, then optionally post it")
async def winlossgraph_command(interaction: discord.Interaction):
    route = _summary_route(interaction.channel_id)
    if not route:
        await _reject_summary_wrong_channel(interaction)
        return
    if not _summary_allowed(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    months = dailylog.available_months(route.origins, limit=12)
    if not months:
        await interaction.followup.send("No decided picks logged for any month yet.", ephemeral=True)
        return
    view = WinLossGraphMonthPickView(route.origins, route.post_channel_id, months, interaction.user.id)
    await interaction.followup.send("Pick a month to preview:", view=view, ephemeral=True)


_PERFORMANCE_ALL_TIME = "All-time"  # not a real "YYYY-MM" value - handled specially wherever a period is passed around


class _PerformanceRoute(NamedTuple):
    score_channels: tuple[int, ...]
    post_channel_id: int


# Per-invoke-channel /performance routing - same "keep entirely separate
# Discord servers/clients from ever mixing into the same report" reasoning
# as config.SUMMARY_ROUTES, just keyed to which scores channels count
# instead of which picks-source channels do (see dailylog.
# PERFORMANCE_CHANNEL_IDS' own docstring for why /performance filters by
# `channel_id` at all). Confirmed live this matters for real: one of the
# original two scores channels below sits in the very same Discord guild
# as a channel in this override table, so without an explicit route here,
# nothing would stop two unrelated clients' numbers from showing up in the
# same chart depending on which channel the command happened to be run
# from. An invoke channel not listed here falls back to the original
# hardcoded scope (dailylog.PERFORMANCE_CHANNEL_IDS / _PERFORMANCE_DEFAULT_
# POST_CHANNEL_ID) rather than being blocked outright - /performance still
# has no ALLOWED_CHANNEL_ID-style restriction on where it can run.
_PERFORMANCE_DEFAULT_POST_CHANNEL_ID = 1538638629889380412
_PERFORMANCE_ROUTE_OVERRIDES: dict[int, _PerformanceRoute] = {
    1421347964831399988: _PerformanceRoute((1537081802764845178,), 1537081802764845178),
    1535311599403798528: _PerformanceRoute((1537081802764845178,), 1537081802764845178),
}


def _performance_route(interaction_channel_id: int) -> _PerformanceRoute:
    return _PERFORMANCE_ROUTE_OVERRIDES.get(
        interaction_channel_id,
        _PerformanceRoute(dailylog.PERFORMANCE_CHANNEL_IDS, _PERFORMANCE_DEFAULT_POST_CHANNEL_ID),
    )


def _performance_title(period: str) -> str:
    if period == _PERFORMANCE_ALL_TIME:
        return "Win Rate - By Sports — All-Time"
    return f"Win Rate - By Sports — {winlossgraph._month_title(period)}"


def _build_performance_embed_and_file(image_bytes: bytes) -> tuple[discord.Embed, discord.File]:
    file = discord.File(io.BytesIO(image_bytes), filename="performance.png")
    embed = discord.Embed(color=0x2B2D31)
    embed.set_image(url="attachment://performance.png")
    return embed, file


def _last_performance_post_key(channel_id: int, period: str) -> str:
    return f"{channel_id}:{period}"


def _get_last_performance_post(channel_id: int, period: str) -> Optional[int]:
    return state.load_last_performance_post().get(_last_performance_post_key(channel_id, period))


def _set_last_performance_post(channel_id: int, period: str, message_id: int):
    data = state.load_last_performance_post()
    data[_last_performance_post_key(channel_id, period)] = message_id
    state.save_last_performance_post(data)


class PerformancePostView(discord.ui.View):
    """Same preview-then-post pattern as WinLossGraphPostView - re-renders
    at click time (not the preview-time snapshot) since a pending pick can
    resolve in the time between preview and click. Always posts to this
    route's own post_channel_id, resolved once (via _performance_route) at
    the moment /performance was originally invoked - not re-resolved from
    wherever this button happens to be clicked, though in practice that's
    always the same ephemeral message anyway.

    Tracks the message id of the most recent post per (post_channel_id,
    period) in state.last_performance_post - "Replace last post" only ever
    replaces a post for the SAME period (e.g. the same month, or all-time)
    currently being previewed, never a different one that happens to be
    more recent in that channel."""

    def __init__(self, period: str, route: _PerformanceRoute, requester_id: int):
        super().__init__(timeout=900)
        self.period = period
        self.route = route
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /performance can use this.", ephemeral=True)
            return False
        return True

    async def _render(self) -> Optional[tuple[discord.Embed, discord.File]]:
        year_month = None if self.period == _PERFORMANCE_ALL_TIME else self.period
        data = dailylog.sport_tournament_win_loss(year_month, self.route.score_channels)
        if not data:
            return None
        image_bytes = await asyncio.to_thread(performance.render_chart, _performance_title(self.period), data)
        return _build_performance_embed_and_file(image_bytes)

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        rendered = await self._render()
        if not rendered:
            await interaction.response.edit_message(
                content="Nothing to post — no decided picks logged for that period.", embed=None, attachments=[], view=None,
            )
            self.stop()
            return
        embed, file = rendered
        target = client.get_channel(self.route.post_channel_id) or await client.fetch_channel(self.route.post_channel_id)
        message = await target.send(embed=embed, file=file)
        _set_last_performance_post(self.route.post_channel_id, self.period, message.id)
        botlog.event(f"📊 Performance ({self.period}) posted to <#{self.route.post_channel_id}> by **{interaction.user}**")
        await interaction.response.edit_message(content="Posted.", embed=None, attachments=[], view=None)
        self.stop()

    @discord.ui.button(label="Replace last post", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: discord.Interaction, button: discord.ui.Button):
        rendered = await self._render()
        if not rendered:
            await interaction.response.edit_message(
                content="Nothing to post — no decided picks logged for that period.", embed=None, attachments=[], view=None,
            )
            self.stop()
            return
        embed, file = rendered
        target = client.get_channel(self.route.post_channel_id) or await client.fetch_channel(self.route.post_channel_id)
        old_message_id = _get_last_performance_post(self.route.post_channel_id, self.period)
        deleted = False
        if old_message_id:
            try:
                old_message = await target.fetch_message(old_message_id)
                await old_message.delete()
                deleted = True
            except discord.NotFound:
                pass  # already gone (manually deleted, or never actually posted) - nothing to clean up
            except discord.HTTPException as e:
                log.warning("Performance replace: couldn't delete previous post %s in %s: %s", old_message_id, self.route.post_channel_id, e)
        message = await target.send(embed=embed, file=file)
        _set_last_performance_post(self.route.post_channel_id, self.period, message.id)
        botlog.event(
            f"📊 Performance ({self.period}) posted to <#{self.route.post_channel_id}> by **{interaction.user}**"
            + (" (replaced previous post)" if deleted else " (no previous post found to replace)")
        )
        await interaction.response.edit_message(
            content="Posted (previous post replaced)." if deleted else "Posted (no previous post found to replace).",
            embed=None, attachments=[], view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing posted.", embed=None, attachments=[], view=None)
        self.stop()


async def _send_performance_preview(interaction: discord.Interaction, period: str, route: _PerformanceRoute, *, edit: bool):
    year_month = None if period == _PERFORMANCE_ALL_TIME else period
    data = dailylog.sport_tournament_win_loss(year_month, route.score_channels)
    if not data:
        content, embed, file, view = f"No decided picks logged for {period}.", None, None, None
    else:
        image_bytes = await asyncio.to_thread(performance.render_chart, _performance_title(period), data)
        embed, file = _build_performance_embed_and_file(image_bytes)
        content = f"Preview only - not posted yet. Click below to publish it to <#{route.post_channel_id}>."
        view = PerformancePostView(period, route, interaction.user.id)
    if edit:
        await interaction.response.edit_message(content=content, embed=embed, attachments=[file] if file else [], view=view)
    else:
        await interaction.followup.send(content=content, embed=embed, file=file, view=view, ephemeral=True)


class _PerformancePeriodSelect(discord.ui.Select):
    """"All-time" first, then one option per month with at least one
    decided pick logged in this route's own score_channels."""

    def __init__(self, months: list[str], route: _PerformanceRoute, requester_id: int):
        options = [discord.SelectOption(label=_PERFORMANCE_ALL_TIME, description="Every decided pick ever logged")]
        for m in months:
            decided = sum(w + l for sport_data in dailylog.sport_tournament_win_loss(m, route.score_channels).values() for w, l in sport_data.values())
            options.append(discord.SelectOption(label=m, description=f"{decided} decided pick(s)"))
        super().__init__(placeholder="Pick a period to preview...", min_values=1, max_values=1, options=options)
        self.route = route
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /performance can use this.", ephemeral=True)
            return
        await _send_performance_preview(interaction, self.values[0], self.route, edit=True)


class PerformancePeriodPickView(discord.ui.View):
    def __init__(self, months: list[str], route: _PerformanceRoute, requester_id: int):
        super().__init__(timeout=300)
        self.add_item(_PerformancePeriodSelect(months, route, requester_id))


@tree.command(name="performance", description="Preview a win-rate chart by sport and tournament, then optionally post it")
async def performance_command(interaction: discord.Interaction):
    if not _summary_allowed(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    route = _performance_route(interaction.channel_id)
    months = dailylog.available_performance_months(limit=12, score_channel_ids=route.score_channels)
    view = PerformancePeriodPickView(months, route, interaction.user.id)
    await interaction.followup.send("Pick a period to preview:", view=view, ephemeral=True)


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

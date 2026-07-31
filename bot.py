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
import logging
import re
from typing import Optional

import discord
from discord import app_commands

import botlog
import config
import espn
import espn_ufc
import f5tracker
import inning1tracker
import inningtracker
import pendingdelete
import picks
import proptracker
import scores365
import settracker
import tennispropstracker
import throttle
import tracker
import ufctracker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorebox.bot")

intents = discord.Intents.default()
intents.message_content = True  # needed to read pick messages in config.PICKS_CHANNEL_MAP
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


@client.event
async def on_ready():
    await tree.sync()
    log.info("Logged in as %s", client.user)
    await tracker.resume_all(client)
    await proptracker.resume_all(client)
    await inningtracker.resume_all(client)
    await f5tracker.resume_all(client)
    await inning1tracker.resume_all(client)
    await settracker.resume_all(client)
    await tennispropstracker.resume_all(client)
    await ufctracker.resume_all(client)
    await pendingdelete.resume_all(client)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id or str(payload.emoji) != TRASH_EMOJI:
        return

    info = tracker.get_message_owner(payload.message_id)
    kind = "track"
    if not info:
        info = proptracker.get_message_owner(payload.message_id)
        kind = "prop"
    if not info:
        info = inningtracker.get_message_owner(payload.message_id)
        kind = "inning"
    if not info:
        info = f5tracker.get_message_owner(payload.message_id)
        kind = "f5"
    if not info:
        info = inning1tracker.get_message_owner(payload.message_id)
        kind = "inning1"
    if not info:
        info = settracker.get_message_owner(payload.message_id)
        kind = "set1"
    if not info:
        info = tennispropstracker.get_message_owner(payload.message_id)
        kind = "tennis_prop"
    if not info:
        info = ufctracker.get_message_owner(payload.message_id)
        kind = "ufc"
    if not info:
        return

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

    if kind == "track":
        channel_id, game_id, _ = info
        tracker.stop_tracking(channel_id, game_id)
    elif kind == "prop":
        channel_id, event_id, entity_id, stat_key, _ = info
        proptracker.stop_tracking(channel_id, event_id, entity_id, stat_key)
    elif kind == "inning":
        channel_id, event_id, pick_type, _ = info
        inningtracker.stop_tracking(channel_id, event_id, pick_type)
    elif kind == "f5":
        channel_id, game_id, _ = info
        f5tracker.stop_tracking(channel_id, game_id)
    elif kind == "inning1":
        channel_id, game_id, _ = info
        inning1tracker.stop_tracking(channel_id, game_id)
    elif kind == "set1":
        channel_id, game_id, _ = info
        settracker.stop_tracking(channel_id, game_id)
    elif kind == "tennis_prop":
        channel_id, game_id, competitor_id, stat_name, _ = info
        tennispropstracker.stop_tracking(channel_id, game_id, competitor_id, stat_name)
    else:
        channel_id, competition_id, _ = info
        ufctracker.stop_tracking(channel_id, competition_id)

    reactor = str(payload.member) if payload.member else f"user `{payload.user_id}`"
    botlog.event(f"🗑️ Untracked (🗑️ reaction, {kind}): message `{payload.message_id}` in <#{payload.channel_id}> — by **{reactor}**")

    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("Failed to delete message via reaction: %s", e)


async def _auto_track(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None,
):
    """Mirrors /track's core logic for an auto-detected pick - posts via
    channel.send() since there's no interaction to reply to, and has no
    owner (owner_id=None means only admins can 🗑️-delete it).

    total_direction/total_line (mutually exclusive with grading on team, both
    None otherwise) is for a game-total Over/Under pick instead of a
    moneyline - team is still used to find the match either way. team_total
    additionally set means it's one side's own total instead of the combined
    score - team_total is the actual named side being graded."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value)
    except scores365.ScoresError as e:
        log.info("Auto-track: couldn't reach 365scores for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked: **{team}** ({sport_value}) — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-track: no match found for '%s' (%s)", team, sport_value)
        botlog.event(f"❌ Not tracked: **{team}** ({sport_value}) — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if tracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped: **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return

    picked_team = team if total_direction is None and team_total is None else None
    embed, file = await tracker.build_embed(game, sport_id, picked_team, total_direction, total_line, team_total)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        botlog.event(f"⏭️ Not tracked: **{team}** — game `{game_id}` already finished, posted final score only")
        return
    tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, team_total
    )
    log.info("Auto-tracked pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked: **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")


async def _auto_f5(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    combined: bool = False, handicap_line: Optional[float] = None,
):
    """F5 (First 5 Innings) picks - moneyline, team total, combined total, or
    handicap/run-line - settle after the 5th inning, not the whole game - see
    f5tracker.py. combined=True means total_direction/total_line grade both
    sides' F5 runs summed together, not team's own - team is still used to
    find the match either way."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value)
    except scores365.ScoresError as e:
        log.info("Auto-F5: couldn't reach 365scores for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked (F5): **{team}** ({sport_value}) — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-F5: no match found for '%s' (%s)", team, sport_value)
        botlog.event(f"❌ Not tracked (F5): **{team}** ({sport_value}) — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if f5tracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped (F5): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return

    picked_team = None if combined else team
    embed, file = await f5tracker.build_embed(game, sport_id, picked_team, total_direction, total_line, handicap_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    f5tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if await asyncio.to_thread(scores365.innings_breakdown, game_id, f5tracker.THROUGH_INNING) is not None:
        botlog.event(f"⏭️ Not tracked (F5): **{team}** — game `{game_id}` F5 already decided, posted final score only")
        return  # F5 was already decided by the time this pick was posted
    f5tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, handicap_line
    )
    log.info("Auto-tracked F5 pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked (F5): **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")


async def _auto_playerprops(
    channel: discord.abc.Messageable, sport_value: str, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    """Mirrors /playerprops' core logic for an auto-detected pick."""
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
    if not entity:
        log.info("Auto-playerprops: no player found for '%s' (%s)", player, sport_value)
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} ({sport_value}) — player not found on ESPN")
        return

    event_id = await asyncio.to_thread(espn.find_current_event_id, sport_value, entity["team_id"])
    if not event_id:
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — no current/upcoming match found on ESPN")
        return
    event = await asyncio.to_thread(espn.get_event, sport_value, event_id)
    if not event:
        botlog.event(f"❌ Not tracked (prop): **{player}** {stat} — couldn't fetch match data from ESPN")
        return
    if proptracker.is_tracked(channel.id, event_id, entity["id"], stat_key):
        botlog.event(f"⏭️ Skipped (prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return

    current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity["id"], stat_key)
    embed, file = await proptracker.build_embed(
        entity["name"], entity["id"], entity["photo_url"], sport_value, stat, current_value, is_home, team, event,
        direction, line, entity["team_name"],
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    proptracker.register_message(message.id, channel.id, event_id, entity["id"], stat_key, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.is_finished(event):
        botlog.event(f"⏭️ Not tracked (prop): **{player}** {stat} — match already finished, posted final value only")
        return
    proptracker.start_tracking(
        message, channel.id, event_id, entity["id"], entity["team_id"], entity["photo_url"],
        sport_value, stat_key, stat, entity["name"], None, direction, line, entity["team_name"],
    )
    log.info("Auto-tracked player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (prop): **{player}** {stat} ({sport_value}) in <#{channel.id}>")


async def _auto_tennis_playerprops(
    channel: discord.abc.Messageable, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    """Tennis-only equivalent of _auto_playerprops, backed by 365scores
    instead of ESPN (which doesn't support tennis at all) - see
    tennispropstracker.py. A tennis player is its own "competitor" in
    365scores' data, found via find_match_for_team same as every other
    365scores-backed tennis tracker (F5/1st-set/moneyline)."""
    stat_name = scores365.TENNIS_STAT_CATALOG.get(stat)
    if not stat_name:
        botlog.event(f"❌ Not tracked (tennis prop): **{player}** {stat} — unknown stat")
        return
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, player, "tennis")
    except scores365.ScoresError as e:
        log.info("Auto-tennis-playerprops: couldn't reach 365scores for '%s': %s", player, e)
        botlog.event(f"❌ Not tracked (tennis prop): **{player}** {stat} — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-tennis-playerprops: no match found for '%s'", player)
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
    if tennispropstracker.is_tracked(channel.id, game_id, competitor_id, stat_name):
        botlog.event(f"⏭️ Skipped (tennis prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return

    embed, file = await tennispropstracker.build_embed(game, sport_id, competitor_id, resolved_name, stat, stat_name, direction, line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    tennispropstracker.register_message(message.id, channel.id, game_id, competitor_id, stat_name, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        botlog.event(f"⏭️ Not tracked (tennis prop): **{player}** {stat} — match already finished, posted final value only")
        return
    tennispropstracker.start_tracking(
        message, sport_id, game_id, channel.id, competitor_id, stat_name, stat, resolved_name, None, direction, line,
    )
    log.info("Auto-tracked tennis player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (tennis prop): **{player}** {stat} in <#{channel.id}>")


async def _auto_inning_runs(channel: discord.abc.Messageable, team: str, pick_type: str):
    """YRFI/NRFI picks settle after just the 1st inning, not the whole game
    - see inningtracker.py. Always baseball, so no sport param needed."""
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

    event_id = await asyncio.to_thread(espn.find_current_event_id, "baseball", entity["id"])
    if not event_id:
        botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — no current/upcoming match found on ESPN")
        return
    event = await asyncio.to_thread(espn.get_event, "baseball", event_id)
    if not event:
        botlog.event(f"❌ Not tracked ({pick_type}): **{team}** — couldn't fetch match data from ESPN")
        return
    if inningtracker.is_tracked(channel.id, event_id, pick_type):
        botlog.event(f"⏭️ Skipped ({pick_type}): **{team}** — already being tracked in <#{channel.id}>")
        return

    embed, file = await inningtracker.build_embed(event, pick_type)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    inningtracker.register_message(message.id, channel.id, event_id, pick_type, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.get_first_inning_breakdown(event) is not None:
        botlog.event(f"⏭️ Not tracked ({pick_type}): **{team}** — 1st inning already decided, posted final result only")
        return  # 1st inning was already decided by the time this pick was posted
    inningtracker.start_tracking(message, channel.id, event_id, pick_type, entity["id"], None)
    log.info("Auto-tracked inning-runs pick '%s' (%s) -> event %s", team, pick_type, event_id)
    botlog.event(f"✅ Tracked ({pick_type}): **{team}** — event `{event_id}` in <#{channel.id}>")


async def _auto_inning1_result(channel: discord.abc.Messageable, team: str, pick: str):
    """1st Inning Result (3-way: team or Draw) picks settle after the 1st
    inning, not the whole game - see inning1tracker.py. Backed by 365scores
    (like f5tracker.py), not ESPN - always baseball."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "baseball")
    except scores365.ScoresError as e:
        log.info("Auto-1st-inning-result: couldn't reach 365scores for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked (1st inning result): **{team}** — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-1st-inning-result: no match found for '%s'", team)
        botlog.event(f"❌ Not tracked (1st inning result): **{team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if inning1tracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped (1st inning result): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return

    embed, file = await inning1tracker.build_embed(game, sport_id, team, pick)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    inning1tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if await asyncio.to_thread(scores365.innings_breakdown, game_id, inning1tracker.THROUGH_INNING) is not None:
        botlog.event(f"⏭️ Not tracked (1st inning result): **{team}** — game `{game_id}` already decided, posted final result only")
        return  # already decided by the time this pick was posted
    inning1tracker.start_tracking(message, sport_id, game, channel.id, None, team, pick)
    log.info("Auto-tracked 1st-inning-result pick '%s' (%s) -> game %s", team, pick, game_id)
    botlog.event(f"✅ Tracked (1st inning result): **{team}** ({pick}) — game `{game_id}` in <#{channel.id}>")


async def _auto_set1(channel: discord.abc.Messageable, team: str):
    """Tennis 1st-Set moneyline picks settle after Set 1, not the whole
    match - see settracker.py. Backed by 365scores (like f5tracker.py), not
    ESPN - always tennis."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "tennis")
    except scores365.ScoresError as e:
        log.info("Auto-1st-set: couldn't reach 365scores for '%s': %s", team, e)
        botlog.event(f"❌ Not tracked (1st set): **{team}** — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-1st-set: no match found for '%s'", team)
        botlog.event(f"❌ Not tracked (1st set): **{team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if settracker.is_tracked(channel.id, game_id):
        botlog.event(f"⏭️ Skipped (1st set): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return

    embed, file = await settracker.build_embed(game, sport_id, team)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    settracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.tennis_first_set_result(game) is not None:
        botlog.event(f"⏭️ Not tracked (1st set): **{team}** — game `{game_id}` Set 1 already decided, posted final result only")
        return  # already decided by the time this pick was posted
    settracker.start_tracking(message, sport_id, game, channel.id, None, team)
    log.info("Auto-tracked 1st-set pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked (1st set): **{team}** — game `{game_id}` in <#{channel.id}>")


async def _auto_ufc(
    channel: discord.abc.Messageable, fighter: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    """UFC picks - fight moneyline or round total - settle once the bout
    itself finishes, not tied to any wider game clock. Backed by
    espn_ufc.py (365scores has no MMA coverage at all). fighter is only
    used to look the bout up; round-total picks aren't graded on either
    fighter specifically (see ufctracker.py's combined-total-style mode)."""
    label = "UFC round total" if total_direction else "UFC"
    try:
        result = await asyncio.to_thread(espn_ufc.find_ufc_fight, fighter)
    except espn_ufc.EspnUfcError as e:
        log.info("Auto-UFC: couldn't reach ESPN for '%s': %s", fighter, e)
        botlog.event(f"❌ Not tracked ({label}): **{fighter}** — couldn't reach ESPN: {e}")
        return
    if not result:
        log.info("Auto-UFC: no bout found for '%s'", fighter)
        botlog.event(f"❌ Not tracked ({label}): **{fighter}** — no bout found")
        return
    event, competition, fighter_competitor = result
    competition_id = competition["id"]
    if ufctracker.is_tracked(channel.id, competition_id):
        botlog.event(f"⏭️ Skipped ({label}): **{fighter}** — bout `{competition_id}` already being tracked in <#{channel.id}>")
        return

    fighter_id = None if total_direction else fighter_competitor["id"]
    fighter_name = None if total_direction else fighter_competitor["athlete"]["displayName"]
    embed, file = await ufctracker.build_embed(competition, event["name"], fighter_id, fighter_name, total_direction, total_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    ufctracker.register_message(message.id, channel.id, competition_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn_ufc.is_finished(competition):
        botlog.event(f"⏭️ Not tracked ({label}): **{fighter}** — bout `{competition_id}` already finished, posted final result only")
        return
    ufctracker.start_tracking(
        message, channel.id, event["id"], competition_id, competition["date"], None, event["name"],
        fighter_id, fighter_name, total_direction, total_line,
    )
    log.info("Auto-tracked UFC pick '%s' -> bout %s", fighter, competition_id)
    botlog.event(f"✅ Tracked ({label}): **{fighter}** — bout `{competition_id}` in <#{channel.id}>")


@client.event
async def on_message(message: discord.Message):
    target_channel_id = config.PICKS_CHANNEL_MAP.get(message.channel.id)
    if target_channel_id is None or message.author.id == client.user.id:
        return

    log.info("Picks channel message received: %r", message.content)
    parsed = picks.parse_picks_message(message.content)
    log.info("Parsed %d pick(s) from that message", len(parsed))
    line_count = len([ln for ln in message.content.splitlines() if ln.strip()])
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

    for pick in parsed:
        try:
            if pick["kind"] == "track":
                await _auto_track(target_channel, pick["sport"], pick["team"])
            elif pick["kind"] == "total":
                await _auto_track(target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"])
            elif pick["kind"] == "team_total":
                await _auto_track(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], pick["team"]
                )
            elif pick["kind"] == "f5_moneyline":
                await _auto_f5(target_channel, pick["sport"], pick["team"])
            elif pick["kind"] == "f5_total":
                await _auto_f5(target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"])
            elif pick["kind"] == "f5_combined_total":
                await _auto_f5(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], combined=True
                )
            elif pick["kind"] == "f5_handicap":
                await _auto_f5(target_channel, pick["sport"], pick["team"], handicap_line=pick["line"])
            elif pick["kind"] == "inning_runs":
                await _auto_inning_runs(target_channel, pick["team"], pick["pick_type"])
            elif pick["kind"] == "inning1_result":
                await _auto_inning1_result(target_channel, pick["team"], pick["pick"])
            elif pick["kind"] == "set1_moneyline":
                await _auto_set1(target_channel, pick["team"])
            elif pick["kind"] == "tennis_playerprops":
                await _auto_tennis_playerprops(
                    target_channel, pick["player"], pick["stat"], pick.get("direction"), pick.get("line")
                )
            elif pick["kind"] == "ufc_moneyline":
                await _auto_ufc(target_channel, pick["team"])
            elif pick["kind"] == "ufc_round_total":
                await _auto_ufc(target_channel, pick["team"], pick["direction"], pick["line"])
            else:
                await _auto_playerprops(
                    target_channel, pick["sport"], pick["player"], pick["stat"],
                    pick.get("direction"), pick.get("line"),
                )
        except Exception as e:
            log.warning("Failed to auto-track pick %s: %s", pick, e)
            botlog.event(f"❌ Not tracked: pick `{pick}` — unexpected error: {e}")


def _channel_allowed(interaction: discord.Interaction) -> bool:
    return config.ALLOWED_CHANNEL_IDS is None or interaction.channel_id in config.ALLOWED_CHANNEL_IDS


async def _reject_wrong_channel(interaction: discord.Interaction):
    channels = ", ".join(f"<#{cid}>" for cid in config.ALLOWED_CHANNEL_IDS)
    await interaction.response.send_message(f"This bot only works in {channels}.", ephemeral=True)


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
    tracker.register_message(message.id, interaction.channel_id, game_id, interaction.user.id)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        return  # Nothing to track, match is already over.

    tracker.start_tracking(message, sport_id, game, interaction.channel_id, interaction.user.id, team)
    botlog.event(f"✅ Tracked (manual): **{team}** ({sport.name}) — game `{game_id}` in <#{interaction.channel_id}>, by **{interaction.user}**")


async def stat_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    sport = getattr(interaction.namespace, "sport", None)
    sport_key = sport.value if hasattr(sport, "value") else sport
    labels = list(scores365.TENNIS_STAT_CATALOG.keys()) if sport_key == "tennis" else list(espn.STAT_CATALOG.get(sport_key, {}).keys())
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
    tennispropstracker.register_message(message.id, interaction.channel_id, game_id, competitor_id, stat_name, interaction.user.id)
    await message.add_reaction(TRASH_EMOJI)

    if not scores365.is_finished(game):
        tennispropstracker.start_tracking(
            message, sport_id, game_id, interaction.channel_id, competitor_id, stat_name, stat, resolved_name,
            interaction.user.id,
        )
    botlog.event(f"✅ Tracked (manual, tennis prop): **{player}** {stat} in <#{interaction.channel_id}>, by **{interaction.user}**")


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

    if sport.value not in espn.SPORT_PATHS:
        await interaction.followup.send(
            f"{sport.name} isn't supported for /playerprops yet - only Baseball, Basketball, Hockey, NFL, and Tennis for now.",
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
    proptracker.register_message(message.id, interaction.channel_id, event_id, entity["id"], stat_key, interaction.user.id)
    await message.add_reaction(TRASH_EMOJI)

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
    if tracker.stop_tracking(channel_id, game_id):
        stopped.append("moneyline/total pick")

    if f5tracker.stop_tracking(channel_id, game_id):
        stopped.append("F5 pick")

    if inning1tracker.stop_tracking(channel_id, game_id):
        stopped.append("1st inning result pick")

    if settracker.stop_tracking(channel_id, game_id):
        stopped.append("1st set pick")

    if ufctracker.stop_tracking(channel_id, game_id):
        stopped.append("UFC pick")

    for entry in proptracker.list_tracked_details(channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        stat_key = tuple(entry["stat_key"])
        if proptracker.stop_tracking(channel_id, entry["event_id"], entry["entity_id"], stat_key):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

    for entry in inningtracker.list_tracked_details(channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if inningtracker.stop_tracking(channel_id, entry["event_id"], entry["pick_type"]):
            stopped.append(entry["pick_type"])

    for entry in tennispropstracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        if tennispropstracker.stop_tracking(channel_id, entry["game_id"], entry["competitor_id"], entry["stat_name"]):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

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
            "stop": lambda cid=channel_id, gid=entry["game_id"]: tracker.stop_tracking(cid, gid),
        })

    for entry in proptracker.list_tracked_details(channel_id):
        items.append({
            "kind": "prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["event_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, eid=entry["event_id"], enid=entry["entity_id"], sk=tuple(entry["stat_key"]):
                proptracker.stop_tracking(cid, eid, enid, sk),
        })

    for entry in inningtracker.list_tracked_details(channel_id):
        items.append({
            "kind": "inning", "label": entry["pick_type"], "id_label": entry["event_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, eid=entry["event_id"], pt=entry["pick_type"]:
                inningtracker.stop_tracking(cid, eid, pt),
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
            "stop": lambda cid=channel_id, gid=entry["game_id"]: f5tracker.stop_tracking(cid, gid),
        })

    for entry in inning1tracker.list_tracked_details(channel_id):
        pick_label = "Draw" if entry["pick"].upper() == "DRAW" else entry["pick"]
        items.append({
            "kind": "inning1", "label": f"1st Inning: {pick_label}", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"]: inning1tracker.stop_tracking(cid, gid),
        })

    for entry in settracker.list_tracked_details(channel_id):
        items.append({
            "kind": "set1", "label": f"{entry['team']} 1st Set ML", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"]: settracker.stop_tracking(cid, gid),
        })

    for entry in tennispropstracker.list_tracked_details(channel_id):
        items.append({
            "kind": "tennis_prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], comp=entry["competitor_id"], sn=entry["stat_name"]:
                tennispropstracker.stop_tracking(cid, gid, comp, sn),
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
            "stop": lambda cid=channel_id, compid=entry["competition_id"]: ufctracker.stop_tracking(cid, compid),
        })

    return items


@tree.command(name="untrack", description="Stop auto-updating one or more tracked matches/player props in this channel")
@app_commands.describe(
    game_id="Game ID(s) shown by /tracked - separate multiple with commas or spaces",
    player="Player name, to target one specific player prop if a game has more than one tracked (optional)",
)
async def untrack(interaction: discord.Interaction, game_id: str, player: Optional[str] = None):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    _log_command(interaction, game_id=game_id, player=player)

    game_ids = [gid for gid in re.split(r"[,\s]+", game_id.strip()) if gid]
    if not game_ids:
        await interaction.response.send_message("No game ID given.", ephemeral=True)
        return

    lines = []
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
    "set1": "Tracked 1st set picks",
    "tennis_prop": "Tracked tennis player props",
    "ufc": "Tracked UFC picks",
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


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

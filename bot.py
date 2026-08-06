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
import dailylog
import espn
import espn_ufc
import esports
import esportstracker
import f5tracker
import inning1tracker
import inningtracker
import parlaytracker
import pendingdelete
import pendingsoccerprops
import picks
import playerstatsfootball
import proptracker
import scores365
import settracker
import soccerpropstracker
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
    await soccerpropstracker.resume_all(client)
    await ufctracker.resume_all(client)
    await esportstracker.resume_all(client)
    await parlaytracker.resume_all(client)
    await pendingdelete.resume_all(client)
    await pendingsoccerprops.resume_all(_resolve_pending_soccer_prop)


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
        info = soccerpropstracker.get_message_owner(payload.message_id)
        kind = "soccer_prop"
    if not info:
        info = esportstracker.get_message_owner(payload.message_id)
        kind = "esports"
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
        channel_id, game_id, market, team, _ = info
        settracker.stop_tracking(channel_id, game_id, market, team)
    elif kind == "tennis_prop":
        channel_id, game_id, competitor_id, stat_name, _ = info
        tennispropstracker.stop_tracking(channel_id, game_id, competitor_id, stat_name)
    elif kind == "ufc":
        channel_id, competition_id, _ = info
        ufctracker.stop_tracking(channel_id, competition_id)
    elif kind == "esports":
        channel_id, sport, team_a, team_b, market, _ = info
        esportstracker.stop_tracking(channel_id, sport, team_a, team_b, market)
    else:
        channel_id, game_id, member_id, stat_name, _ = info
        soccerpropstracker.stop_tracking(channel_id, game_id, member_id, stat_name)

    reactor = str(payload.member) if payload.member else f"user `{payload.user_id}`"
    botlog.event(f"🗑️ Untracked (🗑️ reaction, {kind}): message `{payload.message_id}` in <#{payload.channel_id}> — by **{reactor}**")

    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("Failed to delete message via reaction: %s", e)


async def _auto_track(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    team_total: Optional[str] = None, section: Optional[str] = None, label: Optional[str] = None,
    origin_channel_id: Optional[int] = None,
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
    embed.set_footer(text=tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        botlog.event(f"⏭️ Not tracked: **{team}** — game `{game_id}` already finished, posted final score only")
        return
    tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, team_total,
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked: **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")


async def _auto_f5(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
    combined: bool = False, handicap_line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
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
    embed.set_footer(text=f5tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    f5tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if await asyncio.to_thread(scores365.innings_breakdown, game_id, f5tracker.THROUGH_INNING) is not None:
        botlog.event(f"⏭️ Not tracked (F5): **{team}** — game `{game_id}` F5 already decided, posted final score only")
        return  # F5 was already decided by the time this pick was posted
    f5tracker.start_tracking(
        message, sport_id, game, channel.id, None, picked_team, total_direction, total_line, handicap_line,
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked F5 pick '%s' -> game %s", team, game_id)
    botlog.event(f"✅ Tracked (F5): **{team}** ({sport_value}) — game `{game_id}` in <#{channel.id}>")


async def _auto_playerprops(
    channel: discord.abc.Messageable, sport_value: str, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
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
    embed.set_footer(text=proptracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    proptracker.register_message(message.id, channel.id, event_id, entity["id"], stat_key, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.is_finished(event):
        botlog.event(f"⏭️ Not tracked (prop): **{player}** {stat} — match already finished, posted final value only")
        return
    proptracker.start_tracking(
        message, channel.id, event_id, entity["id"], entity["team_id"], entity["photo_url"],
        sport_value, stat_key, stat, entity["name"], None, direction, line, entity["team_name"],
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (prop): **{player}** {stat} ({sport_value}) in <#{channel.id}>")


async def _auto_tennis_playerprops(
    channel: discord.abc.Messageable, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
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
    embed.set_footer(text=tennispropstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    tennispropstracker.register_message(message.id, channel.id, game_id, competitor_id, stat_name, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        botlog.event(f"⏭️ Not tracked (tennis prop): **{player}** {stat} — match already finished, posted final value only")
        return
    tennispropstracker.start_tracking(
        message, sport_id, game_id, channel.id, competitor_id, stat_name, stat, resolved_name, None, direction, line,
        section, label, origin_channel_id,
    )
    log.info("Auto-tracked tennis player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (tennis prop): **{player}** {stat} in <#{channel.id}>")


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


async def _complete_soccer_prop_track(
    channel: discord.abc.Messageable, player: str, stat: str, stat_name: str,
    direction: Optional[float], line: Optional[float], result: tuple,
    section: Optional[str], label: Optional[str], origin_channel_id: Optional[int],
):
    """Shared tail of a successful scores365.find_soccer_player() lookup -
    called both right after a fresh auto-track attempt finds a match
    immediately, and later from a pendingsoccerprops retry once one shows up.
    Keeping this split out means the retry path never has to duplicate (or
    drift from) the actual posting/tracking logic below."""
    game, member = result
    game_id, member_id, member_competitor_id = game["id"], member["id"], member.get("competitorId")
    resolved_name = member.get("name", player)
    photo_url = scores365.athlete_photo_url(member)
    if soccerpropstracker.is_tracked(channel.id, game_id, member_id, stat_name):
        botlog.event(f"⏭️ Skipped (soccer prop): **{player}** {stat} — already being tracked in <#{channel.id}>")
        return

    fixture_path, psf_match = await _resolve_soccer_psf_match(game, stat_name)
    if stat_name in playerstatsfootball.STAT_CATALOG and not fixture_path:
        botlog.event(f"❌ Not tracked (soccer prop): **{player}** {stat} — couldn't find this match on our extended stats source")
        return

    embed, file = await soccerpropstracker.build_embed(
        game, member_id, member_competitor_id, resolved_name, photo_url, stat, stat_name, direction, line,
        psf_match=psf_match,
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=soccerpropstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    soccerpropstracker.register_message(message.id, channel.id, game_id, member_id, stat_name, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        botlog.event(f"⏭️ Not tracked (soccer prop): **{player}** {stat} — match already finished, posted final value only")
        return
    soccerpropstracker.start_tracking(
        message, game_id, channel.id, member_id, member_competitor_id, stat_name, photo_url,
        stat, resolved_name, None, direction, line, fixture_path, section, label, origin_channel_id,
    )
    log.info("Auto-tracked soccer player prop pick: %s - %s", player, stat)
    botlog.event(f"✅ Tracked (soccer prop): **{player}** {stat} in <#{channel.id}>")


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
        return
    await _complete_soccer_prop_track(channel, player, stat, stat_name, direction, line, result, section, label, origin_channel_id)


async def _auto_inning_runs(
    channel: discord.abc.Messageable, team: str, pick_type: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
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
    embed.set_footer(text=inningtracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    inningtracker.register_message(message.id, channel.id, event_id, pick_type, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.get_first_inning_breakdown(event) is not None:
        botlog.event(f"⏭️ Not tracked ({pick_type}): **{team}** — 1st inning already decided, posted final result only")
        return  # 1st inning was already decided by the time this pick was posted
    inningtracker.start_tracking(
        message, channel.id, event_id, pick_type, entity["id"], None, section, label, origin_channel_id,
    )
    log.info("Auto-tracked inning-runs pick '%s' (%s) -> event %s", team, pick_type, event_id)
    botlog.event(f"✅ Tracked ({pick_type}): **{team}** — event `{event_id}` in <#{channel.id}>")


async def _auto_inning1_result(
    channel: discord.abc.Messageable, team: str, pick: str,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
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
    embed.set_footer(text=inning1tracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    inning1tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if await asyncio.to_thread(scores365.innings_breakdown, game_id, inning1tracker.THROUGH_INNING) is not None:
        botlog.event(f"⏭️ Not tracked (1st inning result): **{team}** — game `{game_id}` already decided, posted final result only")
        return  # already decided by the time this pick was posted
    inning1tracker.start_tracking(
        message, sport_id, game, channel.id, None, team, pick, section, label, origin_channel_id,
    )
    log.info("Auto-tracked 1st-inning-result pick '%s' (%s) -> game %s", team, pick, game_id)
    botlog.event(f"✅ Tracked (1st inning result): **{team}** ({pick}) — game `{game_id}` in <#{channel.id}>")


async def _auto_tennis_market(
    channel: discord.abc.Messageable, team: str, market: str,
    direction: Optional[str] = None, line: Optional[float] = None,
    section: Optional[str] = None, label: Optional[str] = None, origin_channel_id: Optional[int] = None,
):
    """Tennis "extra market" picks (1st Set ML/Total Games, Match Total
    Games, Win a Set) all settle on some part of the match rather than the
    whole thing finishing normally - see settracker.py. Backed by 365scores
    (like f5tracker.py), not ESPN - always tennis. team is either the named
    player (set1_moneyline/win_a_set) or just one of the two matchup sides
    used to look the match up (set1_total_games/match_total_games, no
    specific team being graded)."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, "tennis")
    except scores365.ScoresError as e:
        log.info("Auto-tennis-market (%s): couldn't reach 365scores for '%s': %s", market, team, e)
        botlog.event(f"❌ Not tracked ({market}): **{team}** — couldn't reach 365scores: {e}")
        return
    if not result:
        log.info("Auto-tennis-market (%s): no match found for '%s'", market, team)
        botlog.event(f"❌ Not tracked ({market}): **{team}** — no match found")
        return
    game, sport_id = result
    game_id = game["id"]
    if settracker.is_tracked(channel.id, game_id, market, team):
        botlog.event(f"⏭️ Skipped ({market}): **{team}** — game `{game_id}` already being tracked in <#{channel.id}>")
        return

    embed, file = await settracker.build_embed(game, sport_id, market, team, direction, line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=settracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    settracker.register_message(message.id, channel.id, game_id, market, team, None)
    await message.add_reaction(TRASH_EMOJI)

    decided, _ = settracker.grade_now(game, market, team, direction, line)
    if decided:
        botlog.event(f"⏭️ Not tracked ({market}): **{team}** — game `{game_id}` already decided, posted final result only")
        return  # already decided by the time this pick was posted
    settracker.start_tracking(
        message, sport_id, game, channel.id, market, None, team, direction, line, section, label, origin_channel_id,
    )
    log.info("Auto-tracked tennis-market (%s) pick '%s' -> game %s", market, team, game_id)
    botlog.event(f"✅ Tracked ({market}): **{team}** — game `{game_id}` in <#{channel.id}>")


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
    if ufctracker.is_tracked(channel.id, competition_id):
        botlog.event(f"⏭️ Skipped ({category_label}): **{fighter}** — bout `{competition_id}` already being tracked in <#{channel.id}>")
        return

    fighter_id = None if total_direction else fighter_competitor["id"]
    fighter_name = None if total_direction else fighter_competitor["athlete"]["displayName"]
    embed, file = await ufctracker.build_embed(competition, league_slug, event["name"], fighter_id, fighter_name, total_direction, total_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=ufctracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    ufctracker.register_message(message.id, channel.id, competition_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn_ufc.is_finished(competition):
        botlog.event(f"⏭️ Not tracked ({category_label}): **{fighter}** — bout `{competition_id}` already finished, posted final result only")
        return
    ufctracker.start_tracking(
        message, channel.id, league_slug, event["id"], competition_id, competition["date"], None, event["name"],
        fighter_id, fighter_name, total_direction, total_line, section, label, origin_channel_id,
    )
    log.info("Auto-tracked UFC pick '%s' -> bout %s", fighter, competition_id)
    botlog.event(f"✅ Tracked ({category_label}): **{fighter}** — bout `{competition_id}` in <#{channel.id}>")


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
        return

    embed, file = await esportstracker.build_embed(
        series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    embed.set_footer(text=esportstracker._footer_text(message.id))
    await throttle.run(channel.id, lambda: message.edit(embed=embed))
    esportstracker.register_message(message.id, channel.id, sport, team_a, team_b, market, None)
    await message.add_reaction(TRASH_EMOJI)

    decided, _ = esportstracker.grade_now(series_data, market, picked_team, direction, line, map_number, picked_maps, other_maps)
    if decided:
        botlog.event(f"⏭️ Not tracked ({category_label}): **{team_a} v {team_b}** — already decided, posted final result only")
        return
    esportstracker.start_tracking(
        message, sport, team_a, team_b, channel.id, market, None,
        picked_team, direction, line, map_number, picked_maps, other_maps, section, label, origin_channel_id,
    )
    log.info("Auto-tracked esports (%s) pick '%s v %s' -> %s", market, team_a, team_b, sport)
    botlog.event(f"✅ Tracked ({category_label}): **{team_a} v {team_b}** in <#{channel.id}>")


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
        # section/label are the verbatim picks-channel header ("MLB", "WNBA",
        # ...) and raw pick line text (see picks.py's parse_picks_message) -
        # threaded into every start_tracking call below purely so /summary
        # can later report on this pick; None for anything picks.py couldn't
        # attribute to a header (dailylog.record_pick no-ops in that case).
        # origin_channel_id is this picks-SOURCE channel itself (not
        # target_channel, which is where the card actually gets posted) -
        # lets /summary group/route reports by config.SUMMARY_ROUTES
        # regardless of which target channel a pick ends up tracked into.
        section, label = pick.get("section"), pick.get("raw")
        origin_channel_id = message.channel.id
        try:
            if pick["kind"] == "track":
                await _auto_track(target_channel, pick["sport"], pick["team"], section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "total":
                await _auto_track(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "team_total":
                await _auto_track(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], pick["team"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "f5_moneyline":
                await _auto_f5(target_channel, pick["sport"], pick["team"], section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "f5_total":
                await _auto_f5(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "f5_combined_total":
                await _auto_f5(
                    target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"], combined=True,
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "f5_handicap":
                await _auto_f5(
                    target_channel, pick["sport"], pick["team"], handicap_line=pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "inning_runs":
                await _auto_inning_runs(target_channel, pick["team"], pick["pick_type"], section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "inning1_result":
                await _auto_inning1_result(target_channel, pick["team"], pick["pick"], section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "set1_moneyline":
                await _auto_tennis_market(target_channel, pick["team"], "set1_moneyline", section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "tennis_set1_total_games":
                await _auto_tennis_market(
                    target_channel, pick["team"], "set1_total_games", pick["direction"], pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "tennis_match_total_games":
                await _auto_tennis_market(
                    target_channel, pick["team"], "match_total_games", pick["direction"], pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "tennis_player_total_games":
                await _auto_tennis_market(
                    target_channel, pick["team"], "player_total_games", pick["direction"], pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "tennis_win_a_set":
                await _auto_tennis_market(
                    target_channel, pick["team"], "win_a_set", pick["direction"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "tennis_playerprops":
                await _auto_tennis_playerprops(
                    target_channel, pick["player"], pick["stat"], pick.get("direction"), pick.get("line"),
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "soccer_playerprops":
                await _auto_soccer_playerprops(
                    target_channel, pick["player"], pick["stat"], pick.get("direction"), pick.get("line"),
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "ufc_moneyline":
                await _auto_ufc(target_channel, pick["team"], section=section, label=label, origin_channel_id=origin_channel_id)
            elif pick["kind"] == "ufc_round_total":
                await _auto_ufc(
                    target_channel, pick["team"], pick["direction"], pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_match_winner":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "match_winner",
                    picked_team=pick["team"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_map_handicap":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_handicap",
                    picked_team=pick["team"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_total_maps":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "total_maps",
                    direction=pick["direction"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_map_winner":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "map_winner",
                    picked_team=pick["team"], map_number=pick["map_number"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_match_and_map_winner":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "match_and_map_winner",
                    picked_team=pick["team"], map_number=pick["map_number"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_win_at_least_one_map":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "win_at_least_one_map",
                    picked_team=pick["team"], direction=pick["direction"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_correct_score":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "correct_score",
                    picked_team=pick["team"], picked_maps=pick["picked_maps"], other_maps=pick["other_maps"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_total_kills":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "total_kills",
                    direction=pick["direction"], line=pick["line"], section=section, label=label, origin_channel_id=origin_channel_id,
                )
            elif pick["kind"] == "esports_team_total_kills":
                await _auto_esports(
                    target_channel, pick["sport"], pick["team_a"], pick["team_b"], "team_total_kills",
                    picked_team=pick["team"], direction=pick["direction"], line=pick["line"],
                    section=section, label=label, origin_channel_id=origin_channel_id,
                )
            else:
                await _auto_playerprops(
                    target_channel, pick["sport"], pick["player"], pick["stat"],
                    pick.get("direction"), pick.get("line"), section=section, label=label, origin_channel_id=origin_channel_id,
                )
        except Exception as e:
            log.warning("Failed to auto-track pick %s: %s", pick, e)
            botlog.event(f"❌ Not tracked: pick `{pick}` — unexpected error: {e}")


def _channel_allowed(interaction: discord.Interaction) -> bool:
    return config.ALLOWED_CHANNEL_IDS is None or interaction.channel_id in config.ALLOWED_CHANNEL_IDS


async def _reject_wrong_channel(interaction: discord.Interaction):
    channels = ", ".join(f"<#{cid}>" for cid in config.ALLOWED_CHANNEL_IDS)
    await interaction.response.send_message(f"This bot only works in {channels}.", ephemeral=True)


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
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        return  # Nothing to track, match is already over.

    tracker.start_tracking(message, sport_id, game, interaction.channel_id, interaction.user.id, team)
    botlog.event(f"✅ Tracked (manual): **{team}** ({sport.name}) — game `{game_id}` in <#{interaction.channel_id}>, by **{interaction.user}**")


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
    await message.add_reaction(TRASH_EMOJI)

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
    await message.add_reaction(TRASH_EMOJI)

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

    for entry in settracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and entry.get("team") and player.lower() not in entry["team"].lower():
            continue
        if settracker.stop_tracking(channel_id, entry["game_id"], entry["market"], entry.get("team")):
            stopped.append(f"{entry['market']} pick")

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

    for entry in soccerpropstracker.list_tracked_details(channel_id):
        if str(entry["game_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        if soccerpropstracker.stop_tracking(channel_id, entry["game_id"], entry["member_id"], entry["stat_name"]):
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
            "stop": lambda cid=channel_id, gid=entry["game_id"], comp=entry["competitor_id"], sn=entry["stat_name"]:
                tennispropstracker.stop_tracking(cid, gid, comp, sn),
        })

    for entry in soccerpropstracker.list_tracked_details(channel_id):
        items.append({
            "kind": "soccer_prop", "label": f"{entry['player_name']} ({entry['stat_label']})", "id_label": entry["game_id"],
            "message_id": entry["message_id"],
            "stop": lambda cid=channel_id, gid=entry["game_id"], mid=entry["member_id"], sn=entry["stat_name"]:
                soccerpropstracker.stop_tracking(cid, gid, mid, sn),
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


def _summary_route(interaction_channel_id: int) -> Optional[tuple[int, ...]]:
    """/summary only runs in - and only ever posts to - a destination
    channel configured in config.SUMMARY_ROUTES; the returned tuple is
    every picks-source channel whose dailylog entries get combined into
    that destination's report. None means this channel isn't a configured
    destination at all, regardless of ALLOWED_CHANNEL_ID - /summary has its
    own, separate channel restriction from every other command."""
    return config.SUMMARY_ROUTES.get(interaction_channel_id)


async def _reject_summary_wrong_channel(interaction: discord.Interaction):
    channels = ", ".join(f"<#{cid}>" for cid in config.SUMMARY_ROUTES)
    await interaction.response.send_message(
        f"/summary only works in {channels}." if channels else "/summary isn't configured for any channel yet.",
        ephemeral=True,
    )


def _summary_status_line(entry: dict) -> str:
    """Never blank, per the report's whole point: a resolved pick gets its
    win/loss/push/void mark; anything still pending gets a neutral mark plus
    whatever live detail its tracker last reported (NOT STARTED/LIVE/
    Postponed), so a reader can see *why* it has no result yet instead of
    the line just vanishing or looking unfinished."""
    if dailylog.is_final(entry["status"]):
        return f"{dailylog.result_mark(entry['status'])} {entry['label']}"
    detail = entry["detail"]
    if detail.startswith("LIVE"):
        mark = "🟡"
    elif detail.startswith("⏸️"):
        mark, detail = "⏸️", detail[2:].strip()
    else:
        mark = "⏳"
    return f"{mark} {entry['label']} — {detail}"


def _build_summary_embed(date_str: str, picks_list: list[dict]) -> discord.Embed:
    sections: dict[str, list[dict]] = {}
    for entry in picks_list:
        sections.setdefault(entry["section"], []).append(entry)

    blocks = []
    for section, entries in sections.items():
        lines = [_summary_status_line(e) for e in entries]
        blocks.append(f"**{section}**\n" + "\n".join(lines))

    return discord.Embed(
        title=f"Summary Report ({date_str})",
        description="\n\n".join(blocks)[:4096],
        color=0x2B2D31,
    )


class SummaryPostView(discord.ui.View):
    """Lets /summary's ephemeral preview actually get posted, or dropped,
    without a second command invocation. Re-reads dailylog at click time
    (not the preview-time snapshot) since a pending pick can resolve, or
    someone else can post/re-preview the same date, in the time between
    preview and click - stale data here would either double-post already-
    reported picks or show a result that's since gone final."""

    def __init__(self, origin_ids: tuple[int, ...], date_str: str, requester_id: int):
        super().__init__(timeout=900)
        self.origin_ids = origin_ids
        self.date_str = date_str
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /summary can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        picks_list = dailylog.picks_for_date(self.origin_ids, self.date_str)
        if not picks_list:
            await interaction.response.edit_message(
                content=f"Nothing left to post for **{self.date_str}** — already posted or empty.", embed=None, view=None,
            )
            self.stop()
            return
        embed = _build_summary_embed(self.date_str, picks_list)
        await interaction.channel.send(embed=embed)
        dailylog.mark_reported(self.origin_ids, self.date_str)
        botlog.event(f"📋 Summary report ({self.date_str}) posted in <#{interaction.channel_id}> by **{interaction.user}**")
        await interaction.response.edit_message(content="Posted.", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing posted.", embed=None, view=None)
        self.stop()


async def _send_summary_preview(interaction: discord.Interaction, origin_ids: tuple[int, ...], date_str: str, *, edit: bool):
    """Shared by the direct-date path and the date-picker dropdown's
    callback - the only difference is whether the response is a fresh
    followup or an edit of the picker message already on screen."""
    picks_list = dailylog.picks_for_date(origin_ids, date_str)
    if not picks_list:
        content, embed, view = f"No picks logged for **{date_str}**.", None, None
    else:
        content = "Preview only - not posted yet. Click below to publish it to this channel."
        embed = _build_summary_embed(date_str, picks_list)
        view = SummaryPostView(origin_ids, date_str, interaction.user.id)
    if edit:
        await interaction.response.edit_message(content=content, embed=embed, view=view)
    else:
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)


class _SummaryDateSelect(discord.ui.Select):
    """One option per date that still has unreported picks logged for this
    route - the closest Discord components get to an actual calendar
    widget (there's no native date-picker component for bots)."""

    def __init__(self, origin_ids: tuple[int, ...], dates: list[str], requester_id: int):
        options = [
            discord.SelectOption(
                label=d, description=f"{len(dailylog.picks_for_date(origin_ids, d))} pick(s) not yet reported",
            )
            for d in dates
        ]
        super().__init__(placeholder="Pick a date to preview...", min_values=1, max_values=1, options=options)
        self.origin_ids = origin_ids
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran /summary can use this.", ephemeral=True)
            return
        await _send_summary_preview(interaction, self.origin_ids, self.values[0], edit=True)


class SummaryDatePickView(discord.ui.View):
    def __init__(self, origin_ids: tuple[int, ...], dates: list[str], requester_id: int):
        super().__init__(timeout=300)
        self.add_item(_SummaryDateSelect(origin_ids, dates, requester_id))


@tree.command(name="summary", description="Preview an end-of-day picks report for a date, then optionally post it")
async def summary(interaction: discord.Interaction):
    origin_ids = _summary_route(interaction.channel_id)
    if not origin_ids:
        await _reject_summary_wrong_channel(interaction)
        return
    if not _summary_allowed(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    _log_command(interaction)
    await interaction.response.defer(ephemeral=True)

    dates = dailylog.available_dates(origin_ids, limit=25)
    if not dates:
        await interaction.followup.send("No picks logged for any date yet.", ephemeral=True)
        return
    view = SummaryDatePickView(origin_ids, dates, interaction.user.id)
    await interaction.followup.send("Pick a date to preview:", view=view, ephemeral=True)


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

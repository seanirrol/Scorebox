#!/usr/bin/env python3
"""
Scorebox — a Discord bot that posts live sports scores, powered by
365scores.com's public JSON API, plus live player-prop-stat tracking powered
by Sofascore.com.

Commands:
  /score sport:<pick> team:<name>                  One-off lookup of a team's live/today match.
  /track sport:<pick> team:<name>                  Posts a live-updating embed that refreshes automatically.
  /playerprops sport:<pick> player: stat:           Tracks a player's live stat (e.g. Points, Earned Runs, Aces).
  /untrack game_id:<id>                             Stops an active tracking loop in this channel.
  /tracked                                          Lists games currently being tracked in this channel.
"""

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

import config
import espn
import f5tracker
import inningtracker
import picks
import proptracker
import scores365
import throttle
import tracker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorebox.bot")

intents = discord.Intents.default()
intents.message_content = True  # needed to read pick messages in config.PICKS_CHANNEL_MAP
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
    else:
        channel_id, game_id, _ = info
        f5tracker.stop_tracking(channel_id, game_id)

    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("Failed to delete message via reaction: %s", e)


async def _auto_track(
    channel: discord.abc.Messageable, sport_value: str, team: str,
    total_direction: Optional[str] = None, total_line: Optional[float] = None,
):
    """Mirrors /track's core logic for an auto-detected pick - posts via
    channel.send() since there's no interaction to reply to, and has no
    owner (owner_id=None means only admins can 🗑️-delete it).

    total_direction/total_line (mutually exclusive with grading on team, both
    None otherwise) is for a game-total Over/Under pick instead of a
    moneyline - team is still used to find the match either way."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value)
    except scores365.ScoresError as e:
        log.info("Auto-track: couldn't reach 365scores for '%s': %s", team, e)
        return
    if not result:
        log.info("Auto-track: no match found for '%s' (%s)", team, sport_value)
        return
    game, sport_id = result
    game_id = game["id"]
    if tracker.is_tracked(channel.id, game_id):
        return

    picked_team = team if total_direction is None else None
    embed, file = await tracker.build_embed(game, sport_id, picked_team, total_direction, total_line)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        return
    tracker.start_tracking(message, sport_id, game, channel.id, None, picked_team, total_direction, total_line)
    log.info("Auto-tracked pick '%s' -> game %s", team, game_id)


async def _auto_f5(channel: discord.abc.Messageable, sport_value: str, team: str):
    """F5 (First 5 Innings) moneyline picks settle after the 5th inning, not
    the whole game - see f5tracker.py."""
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team, sport_value)
    except scores365.ScoresError as e:
        log.info("Auto-F5: couldn't reach 365scores for '%s': %s", team, e)
        return
    if not result:
        log.info("Auto-F5: no match found for '%s' (%s)", team, sport_value)
        return
    game, sport_id = result
    game_id = game["id"]
    if f5tracker.is_tracked(channel.id, game_id):
        return

    embed, file = await f5tracker.build_embed(game, sport_id, team)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    f5tracker.register_message(message.id, channel.id, game_id, None)
    await message.add_reaction(TRASH_EMOJI)

    if await asyncio.to_thread(scores365.innings_breakdown, game_id, f5tracker.THROUGH_INNING) is not None:
        return  # F5 was already decided by the time this pick was posted
    f5tracker.start_tracking(message, sport_id, game, channel.id, None, team)
    log.info("Auto-tracked F5 pick '%s' -> game %s", team, game_id)


async def _auto_playerprops(
    channel: discord.abc.Messageable, sport_value: str, player: str, stat: str,
    direction: Optional[str] = None, line: Optional[float] = None,
):
    """Mirrors /playerprops' core logic for an auto-detected pick."""
    stat_key = espn.STAT_CATALOG.get(sport_value, {}).get(stat)
    if not stat_key:
        return
    try:
        entity = await asyncio.to_thread(espn.find_player, player, sport_value)
    except espn.EspnError as e:
        log.info("Auto-playerprops: couldn't reach ESPN for '%s': %s", player, e)
        return
    if not entity:
        log.info("Auto-playerprops: no player found for '%s' (%s)", player, sport_value)
        return

    event_id = await asyncio.to_thread(espn.find_current_event_id, sport_value, entity["team_id"])
    if not event_id:
        return
    event = await asyncio.to_thread(espn.get_event, sport_value, event_id)
    if not event:
        return
    if proptracker.is_tracked(channel.id, event_id, entity["id"], stat_key):
        return

    current_value, is_home, team = await asyncio.to_thread(espn.get_stat_value, event, entity["id"], stat_key)
    embed, file = await proptracker.build_embed(
        entity["name"], entity["id"], entity["photo_url"], sport_value, stat, current_value, is_home, team, event,
        direction, line,
    )
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    proptracker.register_message(message.id, channel.id, event_id, entity["id"], stat_key, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.is_finished(event):
        return
    proptracker.start_tracking(
        message, channel.id, event_id, entity["id"], entity["team_id"], entity["photo_url"],
        sport_value, stat_key, stat, entity["name"], None, direction, line,
    )
    log.info("Auto-tracked player prop pick: %s - %s", player, stat)


async def _auto_inning_runs(channel: discord.abc.Messageable, team: str, pick_type: str):
    """YRFI/NRFI picks settle after just the 1st inning, not the whole game
    - see inningtracker.py. Always baseball, so no sport param needed."""
    try:
        entity = await asyncio.to_thread(espn.find_team, team, "baseball")
    except espn.EspnError as e:
        log.info("Auto-inning-runs: couldn't reach ESPN for '%s': %s", team, e)
        return
    if not entity:
        log.info("Auto-inning-runs: no team found for '%s'", team)
        return

    event_id = await asyncio.to_thread(espn.find_current_event_id, "baseball", entity["id"])
    if not event_id:
        return
    event = await asyncio.to_thread(espn.get_event, "baseball", event_id)
    if not event:
        return
    if inningtracker.is_tracked(channel.id, event_id, pick_type):
        return

    embed, file = await inningtracker.build_embed(event, pick_type)
    message = await throttle.run(channel.id, lambda: channel.send(embed=embed, file=file))
    inningtracker.register_message(message.id, channel.id, event_id, pick_type, None)
    await message.add_reaction(TRASH_EMOJI)

    if espn.get_first_inning_breakdown(event) is not None:
        return  # 1st inning was already decided by the time this pick was posted
    inningtracker.start_tracking(message, channel.id, event_id, pick_type, entity["id"], None)
    log.info("Auto-tracked inning-runs pick '%s' (%s) -> event %s", team, pick_type, event_id)


@client.event
async def on_message(message: discord.Message):
    target_channel_id = config.PICKS_CHANNEL_MAP.get(message.channel.id)
    if target_channel_id is None or message.author.id == client.user.id:
        return

    log.info("Picks channel message received: %r", message.content)
    parsed = picks.parse_picks_message(message.content)
    log.info("Parsed %d pick(s) from that message", len(parsed))

    try:
        target_channel = client.get_channel(target_channel_id) or await client.fetch_channel(target_channel_id)
    except discord.HTTPException as e:
        log.warning("Auto-track: couldn't reach scores channel %s: %s", target_channel_id, e)
        return

    for pick in parsed:
        try:
            if pick["kind"] == "track":
                await _auto_track(target_channel, pick["sport"], pick["team"])
            elif pick["kind"] == "total":
                await _auto_track(target_channel, pick["sport"], pick["team"], pick["direction"], pick["line"])
            elif pick["kind"] == "f5_moneyline":
                await _auto_f5(target_channel, pick["sport"], pick["team"])
            elif pick["kind"] == "inning_runs":
                await _auto_inning_runs(target_channel, pick["team"], pick["pick_type"])
            else:
                await _auto_playerprops(
                    target_channel, pick["sport"], pick["player"], pick["stat"],
                    pick.get("direction"), pick.get("line"),
                )
        except Exception as e:
            log.warning("Failed to auto-track pick %s: %s", pick, e)


def _channel_allowed(interaction: discord.Interaction) -> bool:
    return config.ALLOWED_CHANNEL_IDS is None or interaction.channel_id in config.ALLOWED_CHANNEL_IDS


async def _reject_wrong_channel(interaction: discord.Interaction):
    channels = ", ".join(f"<#{cid}>" for cid in config.ALLOWED_CHANNEL_IDS)
    await interaction.response.send_message(f"This bot only works in {channels}.", ephemeral=True)


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


async def stat_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    sport = getattr(interaction.namespace, "sport", None)
    sport_key = sport.value if hasattr(sport, "value") else sport
    labels = list(espn.STAT_CATALOG.get(sport_key, {}).keys())
    matches = [label for label in labels if current.lower() in label.lower()]
    return [app_commands.Choice(name=label, value=label) for label in matches[:25]]


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
    await interaction.response.defer()

    if sport.value not in espn.SPORT_PATHS:
        await interaction.followup.send(
            f"{sport.name} isn't supported for /playerprops yet - only Baseball, Basketball, Hockey, and NFL for now.",
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
        entity["name"], entity["id"], entity["photo_url"], sport.value, stat, current_value, is_home, team, event
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
        )


@tree.command(name="untrack", description="Stop auto-updating a tracked match or player prop in this channel")
@app_commands.describe(
    game_id="Game ID shown by /tracked",
    player="Player name, to target one specific player prop if a game has more than one tracked (optional)",
)
async def untrack(interaction: discord.Interaction, game_id: str, player: Optional[str] = None):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return

    stopped = []
    if tracker.stop_tracking(interaction.channel_id, game_id):
        stopped.append(f"game `{game_id}`")

    if f5tracker.stop_tracking(interaction.channel_id, game_id):
        stopped.append(f"F5 pick on game `{game_id}`")

    for entry in proptracker.list_tracked_details(interaction.channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if player and player.lower() not in entry["player_name"].lower():
            continue
        stat_key = tuple(entry["stat_key"])
        if proptracker.stop_tracking(interaction.channel_id, entry["event_id"], entry["entity_id"], stat_key):
            stopped.append(f"{entry['player_name']} ({entry['stat_label']})")

    for entry in inningtracker.list_tracked_details(interaction.channel_id):
        if str(entry["event_id"]) != str(game_id):
            continue
        if inningtracker.stop_tracking(interaction.channel_id, entry["event_id"], entry["pick_type"]):
            stopped.append(entry["pick_type"])

    if stopped:
        await interaction.response.send_message(f"Stopped tracking: {', '.join(stopped)}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No active tracking found for game `{game_id}`.", ephemeral=True)


@tree.command(name="tracked", description="List matches and player props currently being tracked in this channel")
async def tracked(interaction: discord.Interaction):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    match_details = tracker.list_tracked_details(interaction.channel_id)
    prop_details = proptracker.list_tracked_details(interaction.channel_id)
    inning_details = inningtracker.list_tracked_details(interaction.channel_id)
    f5_details = f5tracker.list_tracked_details(interaction.channel_id)
    if not match_details and not prop_details and not inning_details and not f5_details:
        await interaction.followup.send("Nothing is being tracked in this channel.", ephemeral=True)
        return

    sections = []

    if match_details:
        lines = []
        for entry in match_details:
            game = await asyncio.to_thread(scores365.get_live_update, entry["sport_id"], entry["game_id"])
            if game:
                home = (game.get("homeCompetitor") or {}).get("name", "?")
                away = (game.get("awayCompetitor") or {}).get("name", "?")
                label = f"{home} vs {away}"
            else:
                label = "(couldn't fetch match info)"

            # Distinguishes a plain /track (no pick) from a moneyline or
            # total pick - without this, all three looked identical here.
            if entry.get("picked_team"):
                pick_suffix = f" — {entry['picked_team']} ML"
            elif entry.get("total_direction") and entry.get("total_line") is not None:
                pick_suffix = f" — {entry['total_direction'].title()} {entry['total_line']:g}"
            else:
                pick_suffix = ""

            lines.append(f"- `{entry['game_id']}` — {label}{pick_suffix}")
        sections.append("**Tracked matches:**\n" + "\n".join(lines))

    if prop_details:
        lines = [
            f"- `{entry['event_id']}` — {entry['player_name']} ({entry['stat_label']})"
            for entry in prop_details
        ]
        sections.append("**Tracked player props:**\n" + "\n".join(lines))

    if inning_details:
        lines = [f"- `{entry['event_id']}` — {entry['pick_type']}" for entry in inning_details]
        sections.append("**Tracked 1st-inning picks:**\n" + "\n".join(lines))

    if f5_details:
        lines = [f"- `{entry['game_id']}` — {entry['picked_team']} F5 ML" for entry in f5_details]
        sections.append("**Tracked F5 (1st 5 innings) picks:**\n" + "\n".join(lines))

    await interaction.followup.send("\n\n".join(sections), ephemeral=True)


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

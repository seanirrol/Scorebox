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
import proptracker
import scores365
import tracker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorebox.bot")

intents = discord.Intents.default()
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
    else:
        channel_id, event_id, entity_id, stat_key, _ = info
        proptracker.stop_tracking(channel_id, event_id, entity_id, stat_key)

    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("Failed to delete message via reaction: %s", e)


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

    embed, file = await tracker.build_embed(game, sport_id)
    message = await interaction.followup.send(embed=embed, file=file, wait=True)

    # interaction followup messages are bound to a webhook token that expires
    # after ~15 minutes; re-fetch as a plain channel message so edits keep
    # working for the entire tracking duration.
    message = await interaction.channel.fetch_message(message.id)
    tracker.register_message(message.id, interaction.channel_id, game_id, interaction.user.id)
    await message.add_reaction(TRASH_EMOJI)

    if scores365.is_finished(game):
        return  # Nothing to track, match is already over.

    tracker.start_tracking(message, sport_id, game, interaction.channel_id, interaction.user.id)


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


@tree.command(name="untrack", description="Stop auto-updating a tracked match in this channel")
@app_commands.describe(game_id="Game ID shown by /tracked")
async def untrack(interaction: discord.Interaction, game_id: str):
    if not _channel_allowed(interaction):
        await _reject_wrong_channel(interaction)
        return
    stopped = tracker.stop_tracking(interaction.channel_id, game_id)
    if stopped:
        await interaction.response.send_message(f"Stopped tracking game `{game_id}`.", ephemeral=True)
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
    if not match_details and not prop_details:
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
                lines.append(f"- `{entry['game_id']}` — {home} vs {away}")
            else:
                lines.append(f"- `{entry['game_id']}` — (couldn't fetch match info)")
        sections.append("**Tracked matches:**\n" + "\n".join(lines))

    if prop_details:
        lines = [
            f"- `{entry['event_id']}` — {entry['player_name']} ({entry['stat_label']})"
            for entry in prop_details
        ]
        sections.append("**Tracked player props:**\n" + "\n".join(lines))

    await interaction.followup.send("\n\n".join(sections), ephemeral=True)


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Scorebox — a Discord bot that posts live sports scores, powered by
365scores.com's public JSON API.

Commands:
  /score team:<name>       One-off lookup of a team's live/today match.
  /track team:<name>       Posts a live-updating embed that refreshes automatically.
  /untrack game_id:<id>    Stops an active tracking loop in this channel.
  /tracked                 Lists games currently being tracked in this channel.
"""

import asyncio
import logging

import discord
from discord import app_commands

import config
import scores365
import tracker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorebox.bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    log.info("Logged in as %s", client.user)


async def _find_match_or_reply(interaction: discord.Interaction, team: str):
    try:
        result = await asyncio.to_thread(scores365.find_match_for_team, team)
    except scores365.ScoresError as e:
        await interaction.response.send_message(f"Couldn't reach 365scores: {e}", ephemeral=True)
        return None

    if not result:
        await interaction.response.send_message(
            f"No live or scheduled-today match found for **{team}**.", ephemeral=True
        )
        return None

    return result  # (game, sport_id)


@tree.command(name="score", description="Get a team's current match score")
@app_commands.describe(team="Team name, e.g. Arsenal")
async def score(interaction: discord.Interaction, team: str):
    result = await _find_match_or_reply(interaction, team)
    if not result:
        return
    game, _sport_id = result
    await interaction.response.send_message(embed=tracker.build_embed(game))


@tree.command(name="track", description="Post a live score that auto-updates until the match ends")
@app_commands.describe(team="Team name, e.g. Arsenal")
async def track(interaction: discord.Interaction, team: str):
    result = await _find_match_or_reply(interaction, team)
    if not result:
        return
    game, sport_id = result

    game_id = game["id"]
    if tracker.is_tracked(interaction.channel_id, game_id):
        await interaction.response.send_message("That match is already being tracked in this channel.", ephemeral=True)
        return

    await interaction.response.send_message(embed=tracker.build_embed(game))
    message = await interaction.original_response()

    if scores365.is_finished(game):
        return  # Nothing to track, match is already over.

    tracker.start_tracking(message, sport_id, game, interaction.channel_id)


@tree.command(name="untrack", description="Stop auto-updating a tracked match in this channel")
@app_commands.describe(game_id="Game ID shown by /tracked")
async def untrack(interaction: discord.Interaction, game_id: str):
    stopped = tracker.stop_tracking(interaction.channel_id, game_id)
    if stopped:
        await interaction.response.send_message(f"Stopped tracking game `{game_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No active tracking found for game `{game_id}`.", ephemeral=True)


@tree.command(name="tracked", description="List matches currently being tracked in this channel")
async def tracked(interaction: discord.Interaction):
    game_ids = tracker.list_tracked(interaction.channel_id)
    if not game_ids:
        await interaction.response.send_message("No matches are being tracked in this channel.", ephemeral=True)
        return
    listing = "\n".join(f"- `{gid}`" for gid in game_ids)
    await interaction.response.send_message(f"Tracked game IDs in this channel:\n{listing}", ephemeral=True)


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

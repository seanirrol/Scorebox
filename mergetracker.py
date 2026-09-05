#!/usr/bin/env python3
"""
Combines several already-tracked cards (tracker.py / doublechancetracker.py
/ settracker.py only, for now) that are all on the SAME underlying game
into one card - a stack of per-market pick lines up top (each showing its
own live status), with one shared live score box at the bottom, via the
/merge command (paste the comma-separated footer ids of the cards to
combine).

Unlike parlaytracker's groups, a merge group has no independent polling
loop of its own - it's purely passive, refreshed opportunistically whenever
whichever underlying leg's own tracker loop next reports in (report_leg/
handle_leg_result, called from the exact same call sites each supported
module already uses to report into parlaytracker on every poll cycle).
This avoids needing a second hibernation/timeout/finished-
detection system - the underlying legs' own loops already do all of that,
this module just needs somewhere to put the result. A merged-away leg's own
tracker loop skips building/editing its own (now-deleted) card entirely -
without that guard, a few consecutive edit failures against a deleted
message would auto-void the pick (see tracker.py's MAX_CONSECUTIVE_MISSES).

Known limitation: a merged leg's own tracker state (tracks_state.json/
double_chance_state.json) still points at its ORIGINAL (now-deleted)
Discord message id, since /merge only deletes the individual CARD, not that
tracker's own persisted resume record. A bot restart while a merge is
active will therefore fail to re-fetch that message on resume (see each
module's own resume_all) and silently drop those legs from tracking - the
merged card itself survives, just stops updating for the dropped legs. Same
outcome as any other tracked message getting deleted out from under the bot
externally; not solved here. A real fix would need each module's resume_all
to consult merged_into and resume without needing a real message object -
out of scope for this first cut.
"""

import asyncio
import io
import logging
import time
from collections import defaultdict
from typing import Optional

import discord

import parlaytracker
import scoreimage
import scores365
import state
import throttle

log = logging.getLogger("scorebox.mergetracker")

# Serializes updates to the same merge group - two legs of the SAME group
# can report in back-to-back from independent poll loops, each awaiting a
# Discord call between its own load and save. Same race shape parlaytracker
# already hit once (see parlaytracker._persist's own docstring).
_group_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# f"{channel_id}:{module_name}:{track_key_str}" -> group key ("{channel_id}:
# {game_id}") - O(1) lookup so every supported tracker's poll cycle can
# cheaply check "am I merged?" without scanning every active group.
_leg_index: dict[str, str] = {}

_REPOST_ON_FAILURE_COOLDOWN_SECONDS = 120


def _key(channel_id: int, game_id) -> str:
    return f"{channel_id}:{game_id}"


def _leg_lookup_key(channel_id: int, module_name: str, track_key_str: str) -> str:
    return f"{channel_id}:{module_name}:{track_key_str}"


def _footer_text(message_id: Optional[int] = None) -> str:
    return f"Scorebox ({message_id}) • data via 365scores" if message_id else "Scorebox • data via 365scores"


def _persist(key: str, group: Optional[dict]):
    """Same reload-right-before-write safety as parlaytracker._persist."""
    data = state.load_merges()
    if group is None:
        data.pop(key, None)
    else:
        data[key] = group
    state.save_merges(data)


def merged_into(channel_id: int, module_name: str, track_key_str: str) -> Optional[str]:
    """Sync, fast - called every poll cycle by a supported tracker module
    right before it would otherwise build+edit its own (deleted) card."""
    return _leg_index.get(_leg_lookup_key(channel_id, module_name, track_key_str))


def _tracker_modules():
    """Lazily imported - every supported module imports this one at the top
    level to call merged_into/report_leg/handle_leg_result, so importing
    them back at module level here would be circular (same reasoning as
    parlaytracker._tracker_modules)."""
    import doublechancetracker
    import settracker
    import tracker

    return {"tracker": tracker, "doublechancetracker": doublechancetracker, "settracker": settracker}


async def build_merged_embed(
    group: dict, game: Optional[dict], sport_id: Optional[int],
) -> tuple[discord.Embed, Optional[discord.File]]:
    legs = list(group.get("legs", {}).values())
    lines = [
        f"{parlaytracker._leg_square(leg)} {leg['label']}" if leg["status"] != "pending" else leg["label"]
        for leg in legs
    ]

    if any(leg["status"] == "lost" for leg in legs):
        color_status = "lost"
    elif legs and all(leg["status"] in ("won", "push", "void") for leg in legs):
        color_status = "won" if any(leg["status"] == "won" for leg in legs) else "void"
    elif game and scores365.map_status_type(game.get("statusGroup")) == "notstarted":
        color_status = "notstarted"
    else:
        color_status = "inprogress"

    embed = discord.Embed(color=scoreimage.EMBED_COLOR[color_status], description="\n".join(lines))
    if group.get("header"):
        embed.set_author(name=group["header"])
    embed.set_footer(text=_footer_text(group.get("message_id")))
    embed.timestamp = discord.utils.utcnow()

    if not game:
        return embed, None

    home_competitor = game.get("homeCompetitor") or {}
    away_competitor = game.get("awayCompetitor") or {}
    status = scores365.map_status_type(game.get("statusGroup"))
    period_text = "" if status == "notstarted" else scores365.status_line(game, sport_id)
    live_scores = scores365.main_scores(game)
    home_cols = [scores365.fmt_score(live_scores[0])] if live_scores else ["-"]
    away_cols = [scores365.fmt_score(live_scores[1])] if live_scores else ["-"]
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
    return embed, file


async def _edit_or_repost(
    channel: discord.abc.Messageable, channel_id: int, group: dict, game: Optional[dict], sport_id: Optional[int],
) -> int:
    """Mirrors parlaytracker._post_or_edit_summary's edit-in-place-with-
    cooldown-gated-repost-fallback pattern."""
    embed, file = await build_merged_embed(group, game, sport_id)
    message_id = group["message_id"]
    try:
        message = await channel.fetch_message(message_id)
        edit_kwargs = {"embed": embed, "attachments": [file]} if file else {"embed": embed}
        await throttle.run(channel_id, lambda: message.edit(**edit_kwargs))
        return message_id
    except discord.HTTPException as e:
        log.warning("Merged card message %s gone, reposting: %s", message_id, e)
        last_attempt = group.get("last_repost_attempt", 0)
        if time.time() - last_attempt < _REPOST_ON_FAILURE_COOLDOWN_SECONDS:
            return message_id
        group["last_repost_attempt"] = time.time()
    try:
        send_kwargs = {"embed": embed, "file": file} if file else {"embed": embed}
        new_message = await throttle.run(channel_id, lambda: channel.send(**send_kwargs))
    except discord.HTTPException as e:
        log.warning("Failed to repost merged card: %s", e)
        return message_id
    return new_message.id


async def report_leg(
    channel: discord.abc.Messageable, channel_id: int, group_key: str,
    module_name: str, track_key_str: str, detail: str, game: dict, sport_id: int,
):
    """Called every poll cycle by a merged-away leg instead of building/
    editing its own card. detail is a short human-readable status string
    (e.g. "LIVE, 2nd Half (67:00)") - the leg's own pick-description label
    was already captured once at merge time and doesn't need refreshing."""
    leg_id = f"{module_name}:{track_key_str}"
    async with _group_locks[group_key]:
        data = state.load_merges()
        group = data.get(group_key)
        if group is None:
            return  # deleted concurrently, or already fully resolved
        legs = group.get("legs", {})
        if leg_id not in legs or legs[leg_id]["status"] != "pending":
            return  # already terminal - don't downgrade back to pending
        legs[leg_id]["detail"] = detail
        group["message_id"] = await _edit_or_repost(channel, channel_id, group, game, sport_id)
        _persist(group_key, group)


async def handle_leg_result(
    channel: discord.abc.Messageable, channel_id: int, group_key: str,
    module_name: str, track_key_str: str, result: str,
):
    """Called once when a merged-away leg finishes grading. Once every leg
    in the group has a terminal result, the card stays in the channel as
    the final record (same philosophy as a parlay summary card) and the
    group stops being tracked."""
    if result not in ("won", "lost", "push", "void"):
        return
    leg_id = f"{module_name}:{track_key_str}"
    async with _group_locks[group_key]:
        data = state.load_merges()
        group = data.get(group_key)
        if group is None:
            return
        legs = group.get("legs", {})
        if leg_id not in legs:
            return
        legs[leg_id]["status"] = result

        game = await asyncio.to_thread(scores365._get_game_detail, group["game_id"])
        sport_id = game.get("sportId") if game else None
        group["message_id"] = await _edit_or_repost(channel, channel_id, group, game, sport_id)

        if all(leg["status"] != "pending" for leg in legs.values()):
            for lid in legs:
                _leg_index.pop(f"{channel_id}:{lid}", None)
            _persist(group_key, None)
        else:
            _persist(group_key, group)


async def create_merge(channel: discord.abc.Messageable, channel_id: int, message_ids: list[int]) -> str:
    """The /merge command's entry point - validates every pasted card id
    resolves to a currently-tracked pick on the SAME game (just a different
    market), then replaces them with one combined card."""
    if len(message_ids) < 2:
        return "Need at least 2 card IDs to merge."

    mods = _tracker_modules()
    resolved: list[tuple[str, str, int, object]] = []  # (module_name, key, message_id, game_id)
    not_found, wrong_channel = [], []
    game_ids = set()

    for mid in message_ids:
        found = None
        owner = mods["tracker"].get_message_owner(mid)
        if owner:
            owner_channel_id, game_id, picked_team, team_total, total_direction, total_line, _owner_id = owner
            key = mods["tracker"].track_key(owner_channel_id, game_id, picked_team, team_total, total_direction, total_line)
            found = ("tracker", key, owner_channel_id, game_id)
        else:
            owner = mods["doublechancetracker"].get_message_owner(mid)
            if owner:
                owner_channel_id, game_id, _owner_id = owner
                key = mods["doublechancetracker"].track_key(owner_channel_id, game_id)
                found = ("doublechancetracker", key, owner_channel_id, game_id)

        if found is None:
            owner = mods["settracker"].get_message_owner(mid)
            if owner:
                owner_channel_id, game_id, market, team, _owner_id = owner
                key = mods["settracker"].track_key(owner_channel_id, game_id, market, team)
                found = ("settracker", key, owner_channel_id, game_id)

        if found is None:
            not_found.append(str(mid))
            continue
        module_name, key, owner_channel_id, game_id = found
        if owner_channel_id != channel_id:
            wrong_channel.append(str(mid))
            continue
        game_ids.add(game_id)
        resolved.append((module_name, key, mid, game_id))

    if not_found:
        return f"Not currently tracked (or not a supported market for merging yet): {', '.join(not_found)}"
    if wrong_channel:
        return f"Not from this channel: {', '.join(wrong_channel)}"
    if len(game_ids) > 1:
        return "Those cards aren't all the same match - /merge only combines different markets on one game."
    if len({(m, k) for m, k, _mid, _gid in resolved}) < 2:
        return "Need at least 2 distinct picks to merge (not the same market twice)."

    game_id = next(iter(game_ids))
    group_key = _key(channel_id, game_id)
    if group_key in state.load_merges():
        return f"Game `{game_id}` already has a merged card in this channel."

    legs: dict[str, dict] = {}
    header = None
    for module_name, key, mid, _gid in resolved:
        try:
            target_message = await channel.fetch_message(mid)
        except discord.HTTPException:
            return f"Couldn't fetch card `{mid}` - it may have already been deleted."
        if header is None and target_message.embeds and target_message.embeds[0].author:
            header = target_message.embeds[0].author.name
        label = (target_message.embeds[0].description or "?").splitlines()[0] if target_message.embeds else "?"
        legs[f"{module_name}:{key}"] = {"label": label, "status": "pending", "detail": "Pending"}

    game = await asyncio.to_thread(scores365._get_game_detail, game_id)
    sport_id = game.get("sportId") if game else None

    group = {"channel_id": channel_id, "game_id": game_id, "message_id": None, "header": header, "legs": legs}
    embed, file = await build_merged_embed(group, game, sport_id)
    try:
        send_kwargs = {"embed": embed, "file": file} if file else {"embed": embed}
        new_message = await throttle.run(channel_id, lambda: channel.send(**send_kwargs))
    except discord.HTTPException as e:
        log.warning("Failed to post merged card: %s", e)
        return "Failed to post the combined card - see server logs."

    group["message_id"] = new_message.id
    embed.set_footer(text=_footer_text(new_message.id))
    try:
        await throttle.run(channel_id, lambda: new_message.edit(embed=embed))
    except discord.HTTPException as e:
        log.warning("Failed to attach footer id to fresh merged card %s: %s", new_message.id, e)

    for module_name, key, mid, _gid in resolved:
        try:
            old_message = await channel.fetch_message(mid)
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete original card %s after merge: %s", mid, e)

    _persist(group_key, group)
    for leg_id in legs:
        _leg_index[f"{channel_id}:{leg_id}"] = group_key

    return f"Merged {len(legs)} leg(s) into one card: {new_message.jump_url}"


async def resume_all():
    """Called once from on_ready - rebuilds the in-memory leg index from
    persisted state so merged_into keeps working across a restart. Doesn't
    need a Discord client at all: a merge group has no polling loop of its
    own, and if its message has gone missing, _edit_or_repost's own
    HTTPException fallback already reposts a fresh one next time any leg
    reports in - no separate resume-time message check needed."""
    data = state.load_merges()
    for group_key, group in data.items():
        channel_id = group["channel_id"]
        for leg_id in group.get("legs", {}):
            _leg_index[f"{channel_id}:{leg_id}"] = group_key

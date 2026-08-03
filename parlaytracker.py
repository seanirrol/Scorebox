#!/usr/bin/env python3
"""
Tracks ad-hoc "parlay groups" - cards a user has manually marked as
belonging to the same parlay by reacting to each leg with the same custom
emoji while it's still hibernating. That marker survives every repost a
leg's card goes through before it's graded (kickoff bump, graded result,
voided) via the reaction-carry-forward mechanism already built into every
tracker's own _repost_final - this module is what actually does something
with it.

Not a real parlay betting engine - just a lightweight "how's my parlay
doing" live summary, with no explicit registration step. A group's total
leg count is inferred the first time ANY leg in it reports in (live or
already resolved): at that moment, every other currently-active tracked
card in the same channel is checked for the same marker emoji, and however
many are found becomes the group's locked-in total. Fewer than
MIN_PARLAY_LEGS sharing an emoji is treated as an unrelated personal
marker, not a parlay - no card gets created for it.

Once a group exists, every tracker reports into it on EVERY poll cycle (not
just once a leg is finally graded) via report_leg_progress, so the one
summary card this module maintains always shows each leg's current state -
WON/LOST/PUSH/VOID once resolved, or LIVE/NOT STARTED with live detail
while still pending. A live progress update (leg still pending) edits the
card in place; a leg actually finishing (handle_leg_result) bumps it to
the bottom of the channel instead, same "something significant happened,
resurface it" reasoning as every individual tracker's own _repost_final -
editing in place on every single live tick would be enough noise, but a
leg's match ending is worth surfacing. Either way every leg's latest known
status is shown, not just the one that changed. Individual leg cards keep
posting and updating exactly as they always have, entirely unaffected by
this.

A Push/Void leg doesn't count as a win or a loss - it's removed from the
total instead (matches how a real sportsbook recalculates a parlay around
a voided leg). A single loss ends the group's own overall verdict (shown as
LOST from then on), but the card keeps updating as any still-pending legs
finish, rather than freezing mid-parlay.
"""

import logging
from typing import Optional

import discord

import scoreimage
import state
import throttle

log = logging.getLogger("scorebox.parlaytracker")

# Same custom emoji every individual tracker already uses for a graded
# pick's own Won/Lost badge (see e.g. tracker.py's _RESULT_REACTIONS) -
# reused here so a parlay update visually matches every other result in
# the channel instead of introducing a third, different-looking pair.
_WINMARK = "<:winmark:1532115635071488221>"
_LOSSMARK = "<:lossmark:1532115600162422894>"

# A real parlay is at least 3 legs - 1-2 cards sharing an emoji is more
# likely just a personal marker unrelated to a parlay, not worth announcing.
MIN_PARLAY_LEGS = 3

_RESULT_DETAIL = {"won": "WON", "lost": "LOST", "push": "PUSH", "void": "VOID"}
# Discord embeds have no per-line/per-field text color, only one color for
# the whole card's left border - colored squares are the closest thing to
# an actual per-leg color scheme achievable here, one glance tells you
# which legs are in which state without reading the words.
_LEG_SQUARES = {"won": "🟩", "lost": "🟥", "push": "⬜", "void": "⬜"}


def _key(channel_id: int, emoji: str) -> str:
    return f"{channel_id}:{emoji}"


async def _count_group_size(channel: discord.abc.Messageable, channel_id: int, emoji: str, exclude_message_id: int) -> int:
    """How many currently-active tracked cards in this channel (besides the
    one that just reported in, already excluded from every
    list_tracked_details since it's the one calling this) also carry this
    same marker emoji right now - used only once, to lock in a fresh
    group's total leg count.

    Imports every other tracker module lazily (function-local, not at
    module level) since each of them calls back into this module's
    report_leg_progress/handle_leg_result - a module-level import here
    would be circular."""
    import esportstracker
    import f5tracker
    import inning1tracker
    import inningtracker
    import proptracker
    import settracker
    import soccerpropstracker
    import tennispropstracker
    import tracker
    import ufctracker

    tracker_modules = [
        tracker, proptracker, inningtracker, f5tracker, inning1tracker,
        settracker, tennispropstracker, soccerpropstracker, ufctracker, esportstracker,
    ]

    count = 1  # the one that just reported in
    for mod in tracker_modules:
        for entry in mod.list_tracked_details(channel_id):
            message_id = entry.get("message_id")
            if not message_id or message_id == exclude_message_id:
                continue
            try:
                message = await channel.fetch_message(message_id)
            except discord.HTTPException:
                continue
            if any(str(r.emoji) == emoji for r in message.reactions):
                count += 1
    return count


async def _ensure_group(
    data: dict, key: str, channel: discord.abc.Messageable, channel_id: int, emoji: str, exclude_message_id: int,
) -> Optional[dict]:
    """Returns the existing group record, or - the first time any leg
    reports in for this emoji - counts how many cards share it and creates
    one if that's at least MIN_PARLAY_LEGS. Returns None if there's no
    group and none should be created (not enough cards share this emoji -
    probably just an unrelated personal marker)."""
    group = data.get(key)
    if group is not None:
        return group
    total = await _count_group_size(channel, channel_id, emoji, exclude_message_id)
    if total < MIN_PARLAY_LEGS:
        return None
    return {
        "channel_id": channel_id, "emoji": emoji, "total_legs": total,
        "resolved_legs": 0, "won": 0, "voided": 0, "lost": False,
        "summary_message_id": None, "legs": {},
    }


def _leg_square(leg: dict) -> str:
    square = _LEG_SQUARES.get(leg["status"])
    if square:
        return square
    # Still pending - yellow while that leg's own match is actually live,
    # black while it hasn't started yet, distinguishable at a glance from
    # each other and from every resolved state's color.
    return "🟨" if "LIVE" in leg.get("detail", "") else "⬛"


def _summary_color_status(group: dict) -> str:
    """Maps this group's overall state onto the same won/lost/push/void/
    inprogress/notstarted palette every score card in this bot already
    uses (scoreimage.EMBED_COLOR), so the summary card's left border
    reads the same way - green once it's a confirmed win, red the moment
    it busts, purple if voided out entirely, yellow/blue while still
    live/pending."""
    effective_total = group["total_legs"]
    all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]
    if group["lost"]:
        return "lost"
    if effective_total <= 0:
        return "void"
    if all_resolved and group["won"] >= effective_total:
        return "won"
    legs = group.get("legs", {}).values()
    if any(leg["status"] == "pending" and "LIVE" in leg.get("detail", "") for leg in legs):
        return "inprogress"
    return "notstarted"


def _build_summary_embed(group: dict) -> discord.Embed:
    emoji = group["emoji"]
    effective_total = group["total_legs"]
    voided_suffix = f" ({group['voided']} Voided)" if group["voided"] else ""
    all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]

    if group["lost"]:
        subtitle = f"{_LOSSMARK} LOST{voided_suffix}"
    elif effective_total <= 0:
        subtitle = f"➖ Every leg voided, no result{voided_suffix}"
    elif all_resolved and group["won"] >= effective_total:
        subtitle = f"{_WINMARK} {group['won']}/{effective_total} wins — HIT!{voided_suffix}"
    else:
        subtitle = f"{group['resolved_legs']}/{group['total_legs'] + group['voided']} resolved{voided_suffix}"

    # One line per leg in the description rather than a field each - a
    # field's name/value always renders on two lines with no way to merge
    # them, which ate too much vertical space for a card meant to be
    # skimmed at a glance.
    leg_lines = [f"{_leg_square(leg)} {leg['label']} — {leg['detail']}" for leg in group.get("legs", {}).values()]
    embed = discord.Embed(
        title=f"Parlay {emoji}", description="\n".join([subtitle] + leg_lines),
        color=scoreimage.EMBED_COLOR[_summary_color_status(group)],
    )
    embed.timestamp = discord.utils.utcnow()
    return embed


async def _post_or_edit_summary(channel: discord.abc.Messageable, channel_id: int, group: dict) -> Optional[int]:
    """Sends the one summary card the first time a group has anything to
    show, edits it in place on every later update - returns the message id
    to persist (unchanged on a successful edit), or whatever was already
    there if even a fresh send fails."""
    embed = _build_summary_embed(group)
    message_id = group.get("summary_message_id")
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await throttle.run(channel_id, lambda: message.edit(embed=embed))
            return message_id
        except discord.HTTPException as e:
            log.warning("Parlay summary card message %s gone, reposting: %s", message_id, e)
    try:
        message = await throttle.run(channel_id, lambda: channel.send(embed=embed))
    except discord.HTTPException as e:
        log.warning("Failed to post parlay summary card: %s", e)
        return message_id
    return message.id


async def _repost_summary(channel: discord.abc.Messageable, channel_id: int, key: str, group: dict) -> Optional[int]:
    """Bumps the summary card to the bottom of the channel instead of
    editing it in place - called only once a leg's match has actually
    ended (not on every live poll tick, which would spam the channel),
    same "something significant just happened, resurface it" reasoning as
    every individual tracker's own _repost_final. Renders every leg's
    latest known status, not just the one that just resolved - each leg's
    row in group["legs"] is already kept fresh by its own tracker's
    report_leg_progress calls, so this reflects all of them, not a stale
    snapshot.

    The old message id is persisted as "pending_delete_message_id" BEFORE
    the send even starts, not just held in a local variable - a bot
    restart landing anywhere between the send succeeding and the old
    message actually being deleted would otherwise orphan it in the
    channel forever, with nothing left anywhere that remembers it needs
    cleaning up. resume_all sweeps this on startup."""
    embed = _build_summary_embed(group)
    old_message_id = group.get("summary_message_id")

    if old_message_id:
        group["pending_delete_message_id"] = old_message_id
        data = state.load_parlays()
        data[key] = group
        state.save_parlays(data)

    try:
        new_message = await throttle.run(channel_id, lambda: channel.send(embed=embed))
    except discord.HTTPException as e:
        log.warning("Failed to repost parlay summary card: %s", e)
        return old_message_id

    if old_message_id:
        try:
            old_message = await channel.fetch_message(old_message_id)
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete old parlay summary card %s: %s", old_message_id, e)
        group["pending_delete_message_id"] = None

    return new_message.id


async def resume_all(client: discord.Client):
    """Called once from on_ready - finishes cleaning up any parlay summary
    card repost that was interrupted mid-flight by a bot restart (killed
    or redeployed between sending the replacement card and deleting the
    old one)."""
    data = state.load_parlays()
    changed = False
    for key, group in list(data.items()):
        old_id = group.get("pending_delete_message_id")
        if not old_id:
            continue
        try:
            channel = await client.fetch_channel(group["channel_id"])
            message = await channel.fetch_message(old_id)
            await message.delete()
            log.info("Cleaned up an orphaned parlay summary card %s (interrupted repost) in channel %s", old_id, group["channel_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # already gone, or no longer reachable - nothing more to do
        group["pending_delete_message_id"] = None
        data[key] = group
        changed = True
    if changed:
        state.save_parlays(data)


async def report_leg_progress(
    channel: discord.abc.Messageable, channel_id: int, message: discord.Message,
    module_name: str, track_key_str: str, label: str, detail: str, marker_emojis: list,
):
    """Called every poll cycle by every tracker (not just once a leg is
    finally graded) so the summary card can show each still-pending leg's
    live status. detail is a short human-readable status string, e.g.
    "LIVE, Game 2" or "NOT STARTED - <t:1785744000:f>". No-op if the card
    carries no marker emoji at all - most tracked picks aren't part of an
    announced parlay group."""
    if not marker_emojis:
        return
    leg_id = f"{module_name}:{track_key_str}"
    for emoji_obj in marker_emojis:
        emoji = str(emoji_obj)
        key = _key(channel_id, emoji)
        data = state.load_parlays()
        group = await _ensure_group(data, key, channel, channel_id, emoji, message.id)
        if group is None:
            continue
        group.setdefault("legs", {})[leg_id] = {"label": label, "status": "pending", "detail": detail}
        group["summary_message_id"] = await _post_or_edit_summary(channel, channel_id, group)
        data[key] = group
        state.save_parlays(data)


async def handle_leg_result(
    channel: discord.abc.Messageable, channel_id: int, message: discord.Message,
    module_name: str, track_key_str: str, label: str, result: str, marker_emojis: list,
):
    """Called right after a tracked pick is finally graded (won/lost/push/
    void) - marker_emojis is whatever non-service reactions survived onto
    the final message (each tracker's _repost_final already computes this
    for its own carry-forward step, so it's passed in rather than
    re-fetched here). Does nothing if there's no marker emoji at all - most
    tracked picks aren't part of an announced parlay group."""
    if not marker_emojis or result not in ("won", "lost", "push", "void"):
        return

    leg_id = f"{module_name}:{track_key_str}"
    for emoji_obj in marker_emojis:
        emoji = str(emoji_obj)
        key = _key(channel_id, emoji)
        data = state.load_parlays()
        group = await _ensure_group(data, key, channel, channel_id, emoji, message.id)
        if group is None:
            continue

        group["resolved_legs"] += 1
        if result == "won":
            group["won"] += 1
        elif result in ("push", "void"):
            group["voided"] += 1
            group["total_legs"] = max(group["total_legs"] - 1, 0)
        elif result == "lost":
            group["lost"] = True

        group.setdefault("legs", {})[leg_id] = {"label": label, "status": result, "detail": _RESULT_DETAIL[result]}
        group["summary_message_id"] = await _repost_summary(channel, channel_id, key, group)

        all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]
        if all_resolved:
            # Every original leg has now produced a result - the group's
            # done. The summary card message itself stays in the channel
            # (not deleted) as the final record, just no longer tracked.
            data.pop(key, None)
        else:
            data[key] = group
        state.save_parlays(data)

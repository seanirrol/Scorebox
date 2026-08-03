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
while still pending. The card is a single message that gets edited in
place as things change, not reposted - individual leg cards keep posting
and updating exactly as they always have, entirely unaffected by this.

A Push/Void leg doesn't count as a win or a loss - it's removed from the
total instead (matches how a real sportsbook recalculates a parlay around
a voided leg). A single loss ends the group's own overall verdict (shown as
LOST from then on), but the card keeps updating as any still-pending legs
finish, rather than freezing mid-parlay.
"""

import logging
from typing import Optional

import discord

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
_LEG_ICONS = {"won": _WINMARK, "lost": _LOSSMARK, "push": "➖", "void": "➖"}


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


def _format_leg_line(leg: dict) -> str:
    icon = _LEG_ICONS.get(leg["status"], "⏳")
    return f"{icon} {leg['label']} — {leg['detail']}"


def _summary_text(group: dict) -> str:
    emoji = group["emoji"]
    effective_total = group["total_legs"]
    voided_suffix = f" ({group['voided']} Voided)" if group["voided"] else ""
    all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]

    if group["lost"]:
        header = f"{_LOSSMARK} Parlay {emoji} — LOST{voided_suffix}"
    elif effective_total <= 0:
        header = f"➖ Parlay {emoji} — every leg voided, no result{voided_suffix}"
    elif all_resolved and group["won"] >= effective_total:
        header = f"{_WINMARK} Parlay {emoji} — {group['won']}/{effective_total} wins — HIT!{voided_suffix}"
    else:
        header = f"Parlay {emoji} — {group['resolved_legs']}/{group['total_legs'] + group['voided']} resolved{voided_suffix}"

    lines = [header] + [_format_leg_line(leg) for leg in group.get("legs", {}).values()]
    return "\n".join(lines)


async def _post_or_edit_summary(channel: discord.abc.Messageable, channel_id: int, group: dict) -> Optional[int]:
    """Sends the one summary card the first time a group has anything to
    show, edits it in place on every later update - returns the message id
    to persist (unchanged on a successful edit), or whatever was already
    there if even a fresh send fails."""
    text = _summary_text(group)
    message_id = group.get("summary_message_id")
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await throttle.run(channel_id, lambda: message.edit(content=text))
            return message_id
        except discord.HTTPException as e:
            log.warning("Parlay summary card message %s gone, reposting: %s", message_id, e)
    try:
        message = await throttle.run(channel_id, lambda: channel.send(text))
    except discord.HTTPException as e:
        log.warning("Failed to post parlay summary card: %s", e)
        return message_id
    return message.id


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
        group["summary_message_id"] = await _post_or_edit_summary(channel, channel_id, group)

        all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]
        if all_resolved:
            # Every original leg has now produced a result - the group's
            # done. The summary card message itself stays in the channel
            # (not deleted) as the final record, just no longer tracked.
            data.pop(key, None)
        else:
            data[key] = group
        state.save_parlays(data)

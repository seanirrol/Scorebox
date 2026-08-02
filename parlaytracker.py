#!/usr/bin/env python3
"""
Tracks ad-hoc "parlay groups" - cards a user has manually marked as
belonging to the same parlay by reacting to each leg with the same custom
emoji while it's still hibernating. That marker survives every repost a
leg's card goes through before it's graded (kickoff bump, graded result,
voided) via the reaction-carry-forward mechanism already built into every
tracker's own _repost_final - this module is what actually does something
with it once a leg resolves.

Not a real parlay betting engine - just a lightweight "how's my parlay
doing" progress announcer, with no explicit registration step. A parlay's
total leg count is inferred the first time any leg in the group resolves:
at that moment, every other currently-active tracked card in the same
channel is checked for the same marker emoji, and however many are found
(including the leg that just resolved) becomes the group's locked-in total.
A Push/Void leg doesn't count as a win or a loss - it's removed from the
total instead (matches how a real sportsbook recalculates a parlay around
a voided leg), and the running "X Voided" count is folded into every
progress message. A single loss ends the group immediately; the remaining
legs still grade normally on their own, they just stop producing parlay
updates once busted.
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


def _key(channel_id: int, emoji: str) -> str:
    return f"{channel_id}:{emoji}"


async def _count_group_size(channel: discord.abc.Messageable, channel_id: int, emoji: str, exclude_message_id: int) -> int:
    """How many currently-active tracked cards in this channel (besides the
    leg that just resolved, already excluded from every list_tracked_details
    since it just finished) also carry this same marker emoji right now -
    used only once, to lock in a fresh group's total leg count.

    Imports every other tracker module lazily (function-local, not at
    module level) since each of them calls back into this module's
    handle_leg_result once a leg is graded - a module-level import here
    would be circular."""
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
        settracker, tennispropstracker, soccerpropstracker, ufctracker,
    ]

    count = 1  # the leg that just resolved
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


async def handle_leg_result(
    channel: discord.abc.Messageable, channel_id: int, message: discord.Message,
    result: str, marker_emojis: list,
):
    """Called right after a tracked pick is finally graded (won/lost/push/
    void) - marker_emojis is whatever non-service reactions survived onto
    the final message (each tracker's _repost_final already computes this
    for its own carry-forward step, so it's passed in rather than
    re-fetched here). Does nothing if there's no marker emoji at all - most
    tracked picks aren't part of an announced parlay group."""
    if not marker_emojis or result not in ("won", "lost", "push", "void"):
        return

    for emoji_obj in marker_emojis:
        emoji = str(emoji_obj)
        key = _key(channel_id, emoji)
        data = state.load_parlays()
        group = data.get(key)

        if group is None:
            total = await _count_group_size(channel, channel_id, emoji, message.id)
            if total < MIN_PARLAY_LEGS:
                # Fewer than 3 cards share this emoji - not a parlay,
                # probably just an unrelated personal marker on 1-2 cards.
                # No group is created, so a later leg with the same emoji
                # re-runs this same count (by then one fewer, since this
                # leg's own tracker has already stopped tracking it) rather
                # than reading a stale total from a group that was never
                # actually established.
                continue
            group = {
                "channel_id": channel_id, "emoji": emoji, "total_legs": total,
                "resolved_legs": 0, "won": 0, "voided": 0, "lost": False,
            }

        was_already_lost = group["lost"]

        group["resolved_legs"] += 1
        if result == "won":
            group["won"] += 1
        elif result in ("push", "void"):
            group["voided"] += 1
            group["total_legs"] = max(group["total_legs"] - 1, 0)
        elif result == "lost":
            group["lost"] = True

        effective_total = group["total_legs"]
        voided_suffix = f" ({group['voided']} Voided)" if group["voided"] else ""
        # total_legs + voided is an invariant equal to the group's original
        # leg count (each void moves one leg from total_legs into voided) -
        # comparing resolved_legs against that sum is how "every original
        # leg has now produced a result" is detected without needing to
        # separately remember the un-adjusted original count.
        all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]

        text = None
        if was_already_lost:
            pass  # parlay already busted from an earlier leg - stay quiet
        elif result == "lost":
            text = f"{_LOSSMARK} Parlay {emoji} lost{voided_suffix}"
        elif effective_total <= 0:
            text = f"➖ Parlay {emoji} — every leg voided, no result{voided_suffix}"
        elif all_resolved and group["won"] >= effective_total:
            text = f"{_WINMARK} Parlay {emoji} {group['won']}/{effective_total} wins — HIT!{voided_suffix}"
        elif result == "won":
            text = f"{_WINMARK} Parlay {emoji} {group['won']}/{effective_total} wins{voided_suffix}"

        if text:
            try:
                await throttle.run(channel_id, lambda t=text: channel.send(t))
            except discord.HTTPException as e:
                log.warning("Failed to post parlay update: %s", e)

        # Keep the record around (with lost=True) until every original leg
        # has actually resolved, even once busted - a later-resolving leg
        # still needs to see was_already_lost=True on its own turn, rather
        # than finding no record and starting a brand new group from
        # scratch (confirmed live: this was silently posting a bogus "HIT"
        # for a parlay that had already lost).
        if all_resolved:
            data.pop(key, None)
        else:
            data[key] = group
        state.save_parlays(data)

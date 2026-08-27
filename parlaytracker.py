#!/usr/bin/env python3
"""
Tracks ad-hoc "parlay groups" - a set of tracked picks the user has
explicitly grouped together via the /parlay command (create, then add each
leg by pasting its card's message ID from the footer). Membership is fully
manual now - there is no reaction-based auto-detection anymore (that
approach kept producing races and undercounts in practice: duplicate
summary cards, legs invisible for hours until their own tracker happened to
re-poll, off-by-one counts from the group-size heuristic). A leg's card
still reposts/edits exactly as it always has, entirely independent of this
module - parlaytracker only reacts to (a) explicit /parlay add/remove calls
and (b) every tracker's own report_leg_progress/handle_leg_result calls,
which now check membership via groups_for_leg instead of scanning Discord
reactions.

Not a real parlay betting engine - just a lightweight "how's my parlay
doing" live summary. Once a group exists, every tracker reports into it on
EVERY poll cycle (not just once a leg is finally graded) via
report_leg_progress, so the one summary card this module maintains always
shows each leg's current state - WON/LOST/PUSH/VOID once resolved, or
LIVE/NOT STARTED with live detail while still pending. A live progress
update (leg still pending) edits the card in place; a leg actually
finishing (handle_leg_result) bumps it to the bottom of the channel
instead, same "something significant happened, resurface it" reasoning as
every individual tracker's own _repost_final - editing in place on every
single live tick would be enough noise, but a leg's match ending is worth
surfacing. Either way every leg's latest known status is shown, not just
the one that changed.

A Push/Void leg doesn't count as a win or a loss - it's removed from the
total instead (matches how a real sportsbook recalculates a parlay around
a voided leg). A single loss ends the group's own overall verdict (shown as
LOST from then on), but the card keeps updating as any still-pending legs
finish, rather than freezing mid-parlay. Removing a leg via /parlay remove
does the same thing to the total as a void does - if that empties the
group down to zero legs, the whole group is deleted outright (its
identifier becomes free to reuse via a fresh /parlay create) rather than
left around as an empty shell.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Optional

import discord

import scoreimage
import state
import throttle

log = logging.getLogger("scorebox.parlaytracker")

# Serializes group creation/update per (channel, identifier) - a manual
# /parlay add/remove and a tracker's own report_leg_progress/
# handle_leg_result for a leg in the same group could otherwise race on the
# same group's legs dict/counters (confirmed this exact class of bug once
# already with the old reaction-based auto-detection: two concurrent calls
# each loading state before the other saved produced 3 duplicate summary
# cards for one parlay). Keyed per-group (not one global lock) so unrelated
# parlays in other channels/identifiers aren't serialized against each other.
_group_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Same custom emoji every individual tracker already uses for a graded
# pick's own Won/Lost badge (see e.g. tracker.py's _RESULT_REACTIONS) -
# reused here so a parlay update visually matches every other result in
# the channel instead of introducing a third, different-looking pair.
_WINMARK = "<:winmark:1532115635071488221>"
_LOSSMARK = "<:lossmark:1532115600162422894>"

_RESULT_DETAIL = {"won": "WON", "lost": "LOST", "push": "PUSH", "void": "VOID"}
# Discord embeds have no per-line/per-field text color, only one color for
# the whole card's left border - colored squares are the closest thing to
# an actual per-leg color scheme achievable here, one glance tells you
# which legs are in which state without reading the words.
_LEG_SQUARES = {"won": "🟩", "lost": "🟥", "push": "⬜", "void": "⬜"}

MAX_IDENTIFIER_LENGTH = 14


def _key(channel_id: int, identifier: str) -> str:
    return f"{channel_id}:{identifier}"


def _tracker_modules():
    """Lazily imports every tracker module (function-local, not at module
    level) since each of them calls back into this module's
    report_leg_progress/handle_leg_result/groups_for_leg - a module-level
    import here would be circular."""
    import boxingtracker
    import esportstracker
    import f5tracker
    import halftracker
    import htfttracker
    import inning1tracker
    import inningtracker
    import kboproptracker
    import proptracker
    import settracker
    import soccerpropstracker
    import tennispropstracker
    import tracker
    import ufctracker

    return {
        "tracker": tracker, "proptracker": proptracker, "inningtracker": inningtracker,
        "f5tracker": f5tracker, "halftracker": halftracker, "inning1tracker": inning1tracker,
        "settracker": settracker, "soccerpropstracker": soccerpropstracker,
        "tennispropstracker": tennispropstracker, "ufctracker": ufctracker, "boxingtracker": boxingtracker,
        "esportstracker": esportstracker, "kboproptracker": kboproptracker, "htfttracker": htfttracker,
    }


def resolve_leg(message_id: int) -> Optional[tuple[str, str, int]]:
    """Given a card's Discord message ID (as printed in its own footer),
    figures out which tracker module owns it and reconstructs that
    module's own track_key/prop_key string - returns
    (module_name, track_key_str, owner_channel_id), or None if no currently
    -tracked card anywhere has this message ID. Every module's own
    _message_owners tuple has channel_id as its first element, but the
    rest of the shape (and the track_key/prop_key signature needed to
    rebuild the key) differs per module - no generic shortcut is possible,
    so this is an explicit branch per module.

    Each branch's unpack must match that module's own _message_owners
    tuple shape (owner_id last, everything else in between) AND its own
    track_key/prop_key signature, in full - not just channel_id+game_id.
    Confirmed live: several of these had drifted out of sync as each
    module's own owner tuple grew extra discriminator fields (team_total,
    direction/line, handicap_line, ...) over time without this function
    being updated to match - most crashed /parlay add outright with a
    "too many values to unpack" ValueError (an unhandled exception here
    left the deferred interaction with no reply at all, so it just showed
    "thinking..." forever in Discord); the ones that didn't crash
    (proptracker/soccerpropstracker/tennispropstracker/ufctracker) instead
    silently rebuilt the WRONG key (missing discriminators default back to
    "manual"), so the leg would never actually match its tracker's live
    reports. halftracker was missing from this function entirely - a 1H
    pick's card could never be added to a parlay at all, even though
    halftracker.py already calls back into this module's own
    report_leg_progress/handle_leg_result once a leg IS in a group."""
    mods = _tracker_modules()

    owner = mods["tracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, picked_team, team_total, total_direction, total_line, _owner_id = owner
        key = mods["tracker"].track_key(channel_id, game_id, picked_team, team_total, total_direction, total_line)
        return "tracker", key, channel_id

    owner = mods["proptracker"].get_message_owner(message_id)
    if owner:
        channel_id, event_id, entity_id, stat_key, direction, line, _owner_id = owner
        key = mods["proptracker"].prop_key(channel_id, event_id, entity_id, tuple(stat_key), direction, line)
        return "proptracker", key, channel_id

    owner = mods["inningtracker"].get_message_owner(message_id)
    if owner:
        channel_id, event_id, pick_type, line, _owner_id = owner
        return "inningtracker", mods["inningtracker"].track_key(channel_id, event_id, pick_type, line), channel_id

    owner = mods["f5tracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, picked_team, total_direction, total_line, handicap_line, _owner_id = owner
        key = mods["f5tracker"].track_key(channel_id, game_id, picked_team, total_direction, total_line, handicap_line)
        return "f5tracker", key, channel_id

    owner = mods["halftracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, picked_team, total_direction, total_line, _owner_id = owner
        key = mods["halftracker"].track_key(channel_id, game_id, picked_team, total_direction, total_line)
        return "halftracker", key, channel_id

    owner = mods["inning1tracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, _owner_id = owner
        return "inning1tracker", mods["inning1tracker"].track_key(channel_id, game_id), channel_id

    owner = mods["settracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, market, team, _owner_id = owner
        return "settracker", mods["settracker"].track_key(channel_id, game_id, market, team), channel_id

    owner = mods["soccerpropstracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, member_id, stat_name, direction, line, _owner_id = owner
        key = mods["soccerpropstracker"].prop_key(channel_id, game_id, member_id, stat_name, direction, line)
        return "soccerpropstracker", key, channel_id

    owner = mods["tennispropstracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, competitor_id, stat_name, direction, line, _owner_id = owner
        key = mods["tennispropstracker"].prop_key(channel_id, game_id, competitor_id, stat_name, direction, line)
        return "tennispropstracker", key, channel_id

    owner = mods["ufctracker"].get_message_owner(message_id)
    if owner:
        channel_id, competition_id, fighter_id, total_direction, total_line, _owner_id = owner
        key = mods["ufctracker"].track_key(channel_id, competition_id, fighter_id, total_direction, total_line)
        return "ufctracker", key, channel_id

    owner = mods["boxingtracker"].get_message_owner(message_id)
    if owner:
        channel_id, fight_id, fighter_id, _owner_id = owner
        key = mods["boxingtracker"].track_key(channel_id, fight_id, fighter_id)
        return "boxingtracker", key, channel_id

    owner = mods["kboproptracker"].get_message_owner(message_id)
    if owner:
        channel_id, pcode, stat_label, direction, line, target_date, _owner_id = owner
        key = mods["kboproptracker"].track_key(channel_id, pcode, stat_label, direction, line, target_date)
        return "kboproptracker", key, channel_id

    owner = mods["esportstracker"].get_message_owner(message_id)
    if owner:
        channel_id, sport, team_a, team_b, market, _owner_id = owner
        key = mods["esportstracker"].track_key(channel_id, sport, team_a, team_b, market)
        return "esportstracker", key, channel_id

    owner = mods["htfttracker"].get_message_owner(message_id)
    if owner:
        channel_id, game_id, ht_team, ft_team, _owner_id = owner
        key = mods["htfttracker"].track_key(channel_id, game_id, ht_team, ft_team)
        return "htfttracker", key, channel_id

    return None


def groups_for_leg(channel_id: int, module_name: str, track_key_str: str) -> list[str]:
    """Which of this channel's parlay groups (by identifier) this specific
    leg currently belongs to - a plain, synchronous read of persisted state
    (no Discord call needed at all, unlike the old reaction-scanning this
    replaced), called by every tracker on every poll cycle to decide
    whether/where to report. A leg can belong to more than one parlay at
    once (matches the old system's same allowance for multiple marker
    emoji on one card).

    Deliberately skips any entry with no "identifier" key - a leftover
    group from the old reaction-based system (keyed by "emoji" instead),
    still sitting in persisted state from before this module dropped that
    mechanism entirely. Confirmed live: this crashed every tracker whose
    leg was still listed in one of those old groups on its very next poll
    (group["identifier"] raising KeyError, uncaught, silently killing the
    whole _track_loop task) - explains matches that stopped updating or
    never woke from hibernation right after this shipped."""
    leg_id = f"{module_name}:{track_key_str}"
    data = state.load_parlays()
    return [
        group["identifier"] for group in data.values()
        if "identifier" in group and group.get("channel_id") == channel_id and leg_id in group.get("legs", {})
    ]


def list_groups(channel_id: int) -> list[dict]:
    data = state.load_parlays()
    return [group for group in data.values() if "identifier" in group and group.get("channel_id") == channel_id]


async def create_group(channel_id: int, identifier: str) -> Optional[str]:
    """Returns an error string, or None on success."""
    if not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH:
        return f"Parlay name must be 1-{MAX_IDENTIFIER_LENGTH} characters."
    key = _key(channel_id, identifier)
    async with _group_locks[key]:
        data = state.load_parlays()
        if key in data:
            return f"A parlay named **{identifier}** already exists in this channel."
        data[key] = {
            "channel_id": channel_id, "identifier": identifier, "total_legs": 0,
            "resolved_legs": 0, "won": 0, "voided": 0, "lost": False,
            "summary_message_id": None, "legs": {},
        }
        state.save_parlays(data)
    return None


async def delete_group(channel_id: int, identifier: str) -> str:
    """Drops the named group outright, regardless of how many legs it still
    has - the manual counterpart to remove_legs emptying a group down to
    zero one leg at a time. Same "leave the last-posted summary card alone
    in Discord" treatment as every other retired-group path in this module;
    only the persisted tracking entry goes away, so no tracker reports into
    it again and the identifier becomes free to reuse."""
    key = _key(channel_id, identifier)
    async with _group_locks[key]:
        data = state.load_parlays()
        if key not in data:
            return f"No parlay named **{identifier}** in this channel."
        data.pop(key, None)
        state.save_parlays(data)
    return f"Deleted parlay **{identifier}**."


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
    identifier = group["identifier"]
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
        title=f"Parlay {identifier}", description="\n".join([subtitle] + leg_lines),
        color=scoreimage.EMBED_COLOR[_summary_color_status(group)],
    )
    embed.timestamp = discord.utils.utcnow()
    return embed


async def _post_or_edit_summary(channel: discord.abc.Messageable, channel_id: int, group: dict) -> Optional[int]:
    """Sends the one summary card the first time a group has anything to
    show, edits it in place on every later update - returns the message id
    to persist (unchanged on a successful edit), or whatever was already
    there if even a fresh send fails.

    Called every poll cycle by every tracker with a leg in some group (see
    report_leg_progress), so an edit failure here isn't rare over a
    multi-day slate - if the fetch above still finds the message (edit
    itself failed for some transient reason, not "message already gone"),
    the stale card is deleted after the replacement posts, same as
    _repost_summary does for its own repost. Confirmed live: previously this
    fell straight through to sending a fresh card with no delete attempt at
    all, silently orphaning one card in the channel per failed edit - a
    multi-day parlay could accumulate several of these over time."""
    embed = _build_summary_embed(group)
    message_id = group.get("summary_message_id")
    old_message: Optional[discord.Message] = None
    if message_id:
        try:
            old_message = await channel.fetch_message(message_id)
            await throttle.run(channel_id, lambda: old_message.edit(content=None, embed=embed))
            return message_id
        except discord.HTTPException as e:
            log.warning("Parlay summary card message %s gone, reposting: %s", message_id, e)
    try:
        message = await throttle.run(channel_id, lambda: channel.send(embed=embed))
    except discord.HTTPException as e:
        log.warning("Failed to post parlay summary card: %s", e)
        return message_id
    if old_message is not None:
        try:
            await old_message.delete()
        except discord.HTTPException as e:
            log.warning("Failed to delete stale parlay summary card %s: %s", message_id, e)
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
    old one), and drops any leftover group from the old reaction-based
    system (no "identifier" key - that mechanism was removed entirely).
    Those groups' own summary cards are left alone in Discord (not
    deleted, same "leave the last known record" treatment as any other
    retired group) - only the dead internal tracking entry goes away, so
    nothing (e.g. groups_for_leg) ever has to see it again."""
    data = state.load_parlays()
    changed = False
    for key, group in list(data.items()):
        if "identifier" not in group:
            data.pop(key, None)
            changed = True
            continue
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
    module_name: str, track_key_str: str, label: str, detail: str, group_ids: list,
):
    """Called every poll cycle by every tracker (not just once a leg is
    finally graded) so the summary card can show each still-pending leg's
    live status. detail is a short human-readable status string, e.g.
    "LIVE, Game 2" or "NOT STARTED - <t:1785744000:f>". group_ids comes
    from groups_for_leg - a no-op if the leg isn't currently a member of
    any parlay group."""
    if not group_ids:
        return
    leg_id = f"{module_name}:{track_key_str}"
    for identifier in group_ids:
        key = _key(channel_id, identifier)
        async with _group_locks[key]:
            data = state.load_parlays()
            group = data.get(key)
            if group is None:
                continue  # deleted (e.g. by a concurrent /parlay remove) since groups_for_leg's scan
            legs = group.setdefault("legs", {})
            if leg_id not in legs:
                # groups_for_leg reads state before this lock is taken - a
                # concurrent /parlay remove could have dropped this exact
                # leg in between. Don't resurrect it.
                continue
            previous_status = legs[leg_id].get("status")
            if previous_status in ("won", "lost", "push", "void"):
                # This leg was already counted as resolved once (most
                # commonly voided by a tracker's own MAX_CONSECUTIVE_MISSES
                # safety net after a transient data-source hiccup - see
                # scores365._get_retrying's own docstring), but a fresh
                # tracker instance is reporting live progress on the exact
                # same pick again now (e.g. manually re-/track'ed once the
                # match turned out to still be live) - undo whatever that
                # earlier terminal result already did to the group's own
                # aggregate counters before folding it back into "still
                # pending", so handle_leg_result doesn't double-count it
                # once it produces a real result again. Confirmed live: a
                # 6-leg parlay with 4 legs genuinely still pending got
                # stuck thinking only 3 more results were needed (still
                # carrying the phantom void's +1 resolved/+1 voided/-1
                # total from before), which would have finalized and
                # deleted the group's own tracking one leg early, silently
                # dropping whichever leg finished last.
                group["resolved_legs"] = max(group["resolved_legs"] - 1, 0)
                if previous_status == "won":
                    group["won"] = max(group["won"] - 1, 0)
                elif previous_status in ("push", "void"):
                    group["voided"] = max(group["voided"] - 1, 0)
                    group["total_legs"] += 1
                # "lost" is deliberately left alone - some other leg
                # entirely may be why the group is already marked lost,
                # and there's no way to tell the two apart from here.
            # Preserve message_id across the overwrite - it's how
            # set_leg_result finds this leg again if its own tracker later
            # dies before ever reaching a real result.
            message_id = legs[leg_id].get("message_id")
            legs[leg_id] = {"label": label, "status": "pending", "detail": detail, "message_id": message_id}
            group["summary_message_id"] = await _post_or_edit_summary(channel, channel_id, group)
            data[key] = group
            state.save_parlays(data)


async def handle_leg_result(
    channel: discord.abc.Messageable, channel_id: int, message: discord.Message,
    module_name: str, track_key_str: str, label: str, result: str, group_ids: list,
):
    """Called right after a tracked pick is finally graded (won/lost/push/
    void). group_ids comes from groups_for_leg, called separately right
    before this - never reuse a tracker's own _repost_final carry_emojis
    return value here, that's an unrelated reaction-carry-forward
    mechanism with nothing to do with parlay membership anymore."""
    if not group_ids or result not in ("won", "lost", "push", "void"):
        return

    leg_id = f"{module_name}:{track_key_str}"
    for identifier in group_ids:
        key = _key(channel_id, identifier)
        async with _group_locks[key]:
            data = state.load_parlays()
            group = data.get(key)
            if group is None:
                continue

            legs = group.setdefault("legs", {})
            if leg_id not in legs:
                continue  # removed out from under us - see report_leg_progress's same guard

            group["resolved_legs"] += 1
            if result == "won":
                group["won"] += 1
            elif result in ("push", "void"):
                group["voided"] += 1
                group["total_legs"] = max(group["total_legs"] - 1, 0)
            elif result == "lost":
                group["lost"] = True

            message_id = legs[leg_id].get("message_id")
            legs[leg_id] = {"label": label, "status": result, "detail": _RESULT_DETAIL[result], "message_id": message_id}
            group["summary_message_id"] = await _repost_summary(channel, channel_id, key, group)

            all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]
            if all_resolved:
                # Every leg has now produced a result - the group's done.
                # The summary card message itself stays in the channel
                # (not deleted) as the final record, just no longer
                # tracked.
                data.pop(key, None)
            else:
                data[key] = group
            state.save_parlays(data)


async def add_legs(
    channel: discord.abc.Messageable, channel_id: int, identifier: str, message_ids: list[int],
) -> str:
    """Adds each message ID (already parsed to int by the caller) as a leg
    of the named parlay - returns a human-readable summary of what
    happened for each one. Placeholder label/detail (the target card's own
    current embed description, "Pending") is used until that leg's own
    tracker naturally reports in on its next poll cycle - same latency as
    the old system had for a freshly-tagged card, not a new regression."""
    key = _key(channel_id, identifier)
    added, already_in, not_found, wrong_channel = [], [], [], []

    async with _group_locks[key]:
        data = state.load_parlays()
        group = data.get(key)
        if group is None:
            return f"No parlay named **{identifier}** in this channel. Use /parlay action:Create first."

        legs = group.setdefault("legs", {})
        for message_id in message_ids:
            resolved = resolve_leg(message_id)
            if resolved is None:
                not_found.append(str(message_id))
                continue
            module_name, track_key_str, owner_channel_id = resolved
            if owner_channel_id != channel_id:
                wrong_channel.append(str(message_id))
                continue
            leg_id = f"{module_name}:{track_key_str}"
            if leg_id in legs:
                already_in.append(str(message_id))
                continue
            try:
                target_message = await channel.fetch_message(message_id)
                label = (target_message.embeds[0].description or "?").splitlines()[0] if target_message.embeds else "?"
            except discord.HTTPException:
                label = "?"
            legs[leg_id] = {"label": label, "status": "pending", "detail": "Pending", "message_id": message_id}
            group["total_legs"] += 1
            added.append(str(message_id))

        if added:
            group["summary_message_id"] = await _post_or_edit_summary(channel, channel_id, group)
        data[key] = group
        state.save_parlays(data)

    parts = []
    if added:
        parts.append(f"Added {len(added)} leg(s): {', '.join(added)}")
    if already_in:
        parts.append(f"Already in **{identifier}**: {', '.join(already_in)}")
    if wrong_channel:
        parts.append(f"Not from this channel: {', '.join(wrong_channel)}")
    if not_found:
        parts.append(f"Not a currently-tracked card: {', '.join(not_found)}")
    return "\n".join(parts) if parts else "Nothing to add."


def _recompute_group_counts(group: dict):
    """Rebuilds won/voided/lost/resolved_legs from scratch by rescanning
    the remaining legs' status fields - safer than incrementally "undoing"
    a specific counter on removal (lost in particular is a bool that more
    than one leg could have independently set)."""
    won = voided = resolved = 0
    lost = False
    for leg in group.get("legs", {}).values():
        status = leg["status"]
        if status == "pending":
            continue
        resolved += 1
        if status == "won":
            won += 1
        elif status in ("push", "void"):
            voided += 1
        elif status == "lost":
            lost = True
    group["won"], group["voided"], group["resolved_legs"], group["lost"] = won, voided, resolved, lost
    group["total_legs"] = len(group.get("legs", {})) - voided


async def remove_legs(
    channel: discord.abc.Messageable, channel_id: int, identifier: str, message_ids: list[int],
) -> str:
    """Removes each message ID's leg from the named parlay - if that empties
    the group down to zero legs, the group is deleted entirely (its
    identifier becomes free to reuse) rather than left as an empty shell.
    The last-posted summary message is left alone in Discord (not deleted)
    as the final record, same treatment as handle_leg_result's own
    all-resolved cleanup."""
    key = _key(channel_id, identifier)
    removed, not_in_group = [], []

    async with _group_locks[key]:
        data = state.load_parlays()
        group = data.get(key)
        if group is None:
            return f"No parlay named **{identifier}** in this channel."

        legs = group.setdefault("legs", {})
        for message_id in message_ids:
            # Prefer matching against each leg's own stored message_id -
            # works even if the leg's tracker is long gone. Fall back to
            # resolve_leg only for legs added before message_id started
            # being stored on them.
            leg_id = next((lid for lid, leg in legs.items() if leg.get("message_id") == message_id), None)
            if leg_id is None:
                resolved = resolve_leg(message_id)
                leg_id = f"{resolved[0]}:{resolved[1]}" if resolved else None
            if leg_id is None or leg_id not in legs:
                not_in_group.append(str(message_id))
                continue
            del legs[leg_id]
            removed.append(str(message_id))

        if not removed:
            return f"None of those IDs are currently in **{identifier}**."

        if not legs:
            data.pop(key, None)
            state.save_parlays(data)
            parts = [f"Removed {len(removed)} leg(s): {', '.join(removed)}", f"**{identifier}** is now empty and has been deleted."]
            if not_in_group:
                parts.append(f"Not in this parlay: {', '.join(not_in_group)}")
            return "\n".join(parts)

        _recompute_group_counts(group)
        group["summary_message_id"] = await _post_or_edit_summary(channel, channel_id, group)
        data[key] = group
        state.save_parlays(data)

    parts = [f"Removed {len(removed)} leg(s): {', '.join(removed)}"]
    if not_in_group:
        parts.append(f"Not in this parlay: {', '.join(not_in_group)}")
    return "\n".join(parts)


async def set_leg_result(
    channel: discord.abc.Messageable, channel_id: int, identifier: str, message_ids: list[int], result: str,
) -> str:
    """Manually marks specific legs won/lost/push/void by message ID - the
    escape hatch for a leg whose own tracker can never finish the job on
    its own (crashed, got orphaned by a since-fixed bug, or its match
    already ended and can no longer be re-tracked at all). Matches purely
    against each leg's own stored message_id (set at /parlay add time, kept
    sticky across every later report_leg_progress/handle_leg_result
    overwrite) rather than resolve_leg - a leg needing this is exactly the
    case where nothing live may remember it anymore, so this can't depend
    on anything still being tracked.

    Recomputes every counter from scratch afterward via
    _recompute_group_counts (same helper remove_legs already uses) instead
    of incrementally updating - safe to call again on an already-resolved
    leg to correct a mistake, no double-counting risk either way."""
    if result not in _RESULT_DETAIL:
        return f"Not a valid result: {result}"
    key = _key(channel_id, identifier)
    resolved_ids, not_found = [], []

    async with _group_locks[key]:
        data = state.load_parlays()
        group = data.get(key)
        if group is None:
            return f"No parlay named **{identifier}** in this channel."

        legs = group.setdefault("legs", {})
        for message_id in message_ids:
            leg_id = next((lid for lid, leg in legs.items() if leg.get("message_id") == message_id), None)
            if leg_id is None:
                not_found.append(str(message_id))
                continue
            legs[leg_id]["status"] = result
            legs[leg_id]["detail"] = _RESULT_DETAIL[result]
            resolved_ids.append(str(message_id))

        if not resolved_ids:
            return f"None of those IDs are legs of **{identifier}** with a known message ID."

        _recompute_group_counts(group)
        group["summary_message_id"] = await _repost_summary(channel, channel_id, key, group)

        all_resolved = group["resolved_legs"] >= group["total_legs"] + group["voided"]
        if all_resolved:
            data.pop(key, None)
        else:
            data[key] = group
        state.save_parlays(data)

    parts = [f"Marked {len(resolved_ids)} leg(s) as {_RESULT_DETAIL[result]}: {', '.join(resolved_ids)}"]
    if not_found:
        parts.append(f"Not a leg of **{identifier}** (or added before manual-resolve support): {', '.join(not_found)}")
    return "\n".join(parts)

#!/usr/bin/env python3
"""
Regression tests for mergetracker.py - the /merge command that combines
several already-tracked cards (tracker.py/doublechancetracker.py) on the
SAME game into one card. See mergetracker.py's own module docstring for why
a merged-away leg's own tracker loop must report through here instead of
just being left to fail its own (deleted) card's edit.

Run with: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import doublechancetracker
import mergetracker
import scores365
import tracker


class _FakeResponse:
    status = 404
    reason = "Not Found"


def _not_found():
    return discord.NotFound(_FakeResponse(), "Unknown Message")


class _FakeEmbed:
    def __init__(self, description=None, author_name=None):
        self.description = description
        self.author = SimpleNamespace(name=author_name) if author_name else None


class _FakeMessage:
    def __init__(self, message_id, embeds=None):
        self.id = message_id
        self.embeds = embeds or []
        self.deleted = False
        self.edits = []
        self.jump_url = f"https://discord.com/channels/0/0/{message_id}"

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self):
        self.messages: dict[int, _FakeMessage] = {}
        self.sent: list[dict] = []
        self._next_id = 9000

    def add(self, message: _FakeMessage):
        self.messages[message.id] = message

    async def fetch_message(self, message_id):
        msg = self.messages.get(message_id)
        if msg is None or msg.deleted:
            raise _not_found()
        return msg

    async def send(self, **kwargs):
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        self.sent.append(kwargs)
        return msg


class MergedIntoAndLegIndex(unittest.TestCase):
    """merged_into is the sync, hot-path lookup every supported tracker's
    poll loop calls every cycle - a plain in-memory dict read, kept in sync
    by report_leg/handle_leg_result/create_merge/resume_all directly rather
    than re-reading merge_state.json every poll."""

    def setUp(self):
        self._saved = dict(mergetracker._leg_index)
        mergetracker._leg_index.clear()

    def tearDown(self):
        mergetracker._leg_index.clear()
        mergetracker._leg_index.update(self._saved)

    def test_unrelated_leg_returns_none(self):
        self.assertIsNone(mergetracker.merged_into(555, "tracker", "some:key"))

    def test_registered_leg_returns_its_group_key(self):
        mergetracker._leg_index["555:tracker:some:key"] = "555:100"
        self.assertEqual(mergetracker.merged_into(555, "tracker", "some:key"), "555:100")

    def test_resume_all_rebuilds_the_index_from_persisted_state(self):
        fake_data = {
            "555:100": {
                "channel_id": 555, "game_id": 100,
                "legs": {"tracker:a": {}, "doublechancetracker:b": {}},
            },
        }
        orig_load = mergetracker.state.load_merges
        mergetracker.state.load_merges = lambda: fake_data
        try:
            asyncio.run(mergetracker.resume_all())
        finally:
            mergetracker.state.load_merges = orig_load
        self.assertEqual(mergetracker._leg_index.get("555:tracker:a"), "555:100")
        self.assertEqual(mergetracker._leg_index.get("555:doublechancetracker:b"), "555:100")


class ReportLegAndHandleLegResult(unittest.TestCase):
    """Drives the real per-leg status bookkeeping against an in-memory
    dict instead of the real JSON file - _edit_or_repost is faked out
    entirely (no Discord/image-rendering I/O) since this is only exercising
    mergetracker's own state transitions, same pattern as
    test_parlaytracker.py's ReportLegProgressUnResolvesAStaleTerminalLeg."""

    def setUp(self):
        self._data: dict = {}
        self._orig_load = mergetracker.state.load_merges
        self._orig_save = mergetracker.state.save_merges
        self._orig_edit = mergetracker._edit_or_repost
        mergetracker.state.load_merges = lambda: dict(self._data)
        mergetracker.state.save_merges = lambda data: self._data.clear() or self._data.update(data)
        mergetracker._edit_or_repost = self._fake_edit_or_repost
        mergetracker._leg_index.clear()

    def tearDown(self):
        mergetracker.state.load_merges = self._orig_load
        mergetracker.state.save_merges = self._orig_save
        mergetracker._edit_or_repost = self._orig_edit
        mergetracker._leg_index.clear()

    async def _fake_edit_or_repost(self, channel, channel_id, group, game, sport_id):
        return group["message_id"]

    def _seed_group(self):
        self._data["555:100"] = {
            "channel_id": 555, "game_id": 100, "message_id": 1, "header": "Soccer • LaLiga",
            "legs": {
                "tracker:555:100:ml:Atletico Madrid": {"label": "Over 2.5", "status": "pending", "detail": "Pending"},
                "doublechancetracker:555:100": {"label": "Draw or Atletico Madrid", "status": "pending", "detail": "Pending"},
            },
        }
        mergetracker._leg_index["555:tracker:555:100:ml:Atletico Madrid"] = "555:100"
        mergetracker._leg_index["555:doublechancetracker:555:100"] = "555:100"

    def test_report_leg_updates_only_the_targeted_leg(self):
        self._seed_group()
        asyncio.run(mergetracker.report_leg(
            None, 555, "555:100", "tracker", "555:100:ml:Atletico Madrid", "LIVE, 2nd Half (10:00)", {}, 1,
        ))
        legs = self._data["555:100"]["legs"]
        self.assertEqual(legs["tracker:555:100:ml:Atletico Madrid"]["detail"], "LIVE, 2nd Half (10:00)")
        self.assertEqual(legs["doublechancetracker:555:100"]["detail"], "Pending")

    def test_report_leg_no_ops_for_a_missing_group(self):
        # Group already fully resolved and removed - a stale/late poll from
        # some other task must not resurrect it.
        asyncio.run(mergetracker.report_leg(None, 555, "555:999", "tracker", "x", "LIVE", {}, 1))
        self.assertNotIn("555:999", self._data)

    def test_report_leg_does_not_downgrade_an_already_terminal_leg(self):
        self._seed_group()
        self._data["555:100"]["legs"]["tracker:555:100:ml:Atletico Madrid"]["status"] = "won"
        asyncio.run(mergetracker.report_leg(
            None, 555, "555:100", "tracker", "555:100:ml:Atletico Madrid", "LIVE, should not apply", {}, 1,
        ))
        leg = self._data["555:100"]["legs"]["tracker:555:100:ml:Atletico Madrid"]
        self.assertEqual(leg["status"], "won")
        self.assertNotEqual(leg["detail"], "LIVE, should not apply")

    def test_handle_leg_result_sets_that_legs_terminal_status(self):
        self._seed_group()
        orig_detail = scores365._get_game_detail
        scores365._get_game_detail = lambda game_id: None
        try:
            asyncio.run(mergetracker.handle_leg_result(None, 555, "555:100", "tracker", "555:100:ml:Atletico Madrid", "won"))
        finally:
            scores365._get_game_detail = orig_detail
        # Still one leg pending - group stays active.
        self.assertIn("555:100", self._data)
        legs = self._data["555:100"]["legs"]
        self.assertEqual(legs["tracker:555:100:ml:Atletico Madrid"]["status"], "won")
        self.assertEqual(legs["doublechancetracker:555:100"]["status"], "pending")

    def test_group_and_leg_index_are_cleaned_up_once_every_leg_is_terminal(self):
        self._seed_group()
        orig_detail = scores365._get_game_detail
        scores365._get_game_detail = lambda game_id: None
        try:
            asyncio.run(mergetracker.handle_leg_result(None, 555, "555:100", "tracker", "555:100:ml:Atletico Madrid", "won"))
            asyncio.run(mergetracker.handle_leg_result(None, 555, "555:100", "doublechancetracker", "555:100", "lost"))
        finally:
            scores365._get_game_detail = orig_detail
        self.assertNotIn("555:100", self._data)
        self.assertIsNone(mergetracker.merged_into(555, "tracker", "555:100:ml:Atletico Madrid"))
        self.assertIsNone(mergetracker.merged_into(555, "doublechancetracker", "555:100"))

    def test_ignores_an_invalid_result_value(self):
        self._seed_group()
        asyncio.run(mergetracker.handle_leg_result(None, 555, "555:100", "tracker", "555:100:ml:Atletico Madrid", "bogus"))
        legs = self._data["555:100"]["legs"]
        self.assertEqual(legs["tracker:555:100:ml:Atletico Madrid"]["status"], "pending")


class PersistDoesNotClobberAConcurrentlySavedDifferentGroup(unittest.TestCase):
    """Same race shape parlaytracker._persist was fixed for once already
    (see that function's own docstring): state.save_merges writes the WHOLE
    file, so a transaction that loads, awaits a Discord call, then saves
    can clobber a DIFFERENT group's own concurrent update that landed in
    that window. _persist must reload immediately before writing so it only
    ever overwrites its own key."""

    def setUp(self):
        self._real_file: dict = {"555:100": {"marker": "group-a-original"}}
        self._orig_load = mergetracker.state.load_merges
        self._orig_save = mergetracker.state.save_merges
        mergetracker.state.load_merges = lambda: dict(self._real_file)
        mergetracker.state.save_merges = lambda data: self._real_file.clear() or self._real_file.update(data)

    def tearDown(self):
        mergetracker.state.load_merges = self._orig_load
        mergetracker.state.save_merges = self._orig_save

    def test_a_concurrent_write_to_a_different_group_survives(self):
        # Group A's transaction "loads" (implicitly, inside _persist) here -
        # simulate a DIFFERENT task (group B) saving its own update in the
        # window group A's own caller would have spent awaiting Discord.
        self._real_file["555:200"] = {"marker": "group-b-fresh"}
        mergetracker._persist("555:100", {"marker": "group-a-updated"})
        self.assertEqual(self._real_file["555:100"], {"marker": "group-a-updated"})
        self.assertEqual(self._real_file["555:200"], {"marker": "group-b-fresh"})


class CreateMergeValidation(unittest.TestCase):
    """create_merge is the /merge command's entry point - resolves each
    pasted card id via tracker.py/doublechancetracker.py's own
    get_message_owner (same source parlaytracker.resolve_leg uses), then
    requires every id to belong to this channel and the SAME game."""

    def setUp(self):
        self._registered_tracker = []
        self._registered_dc = []
        self._data: dict = {}
        self._orig_load = mergetracker.state.load_merges
        self._orig_save = mergetracker.state.save_merges
        mergetracker.state.load_merges = lambda: dict(self._data)
        mergetracker.state.save_merges = lambda data: self._data.clear() or self._data.update(data)
        mergetracker._leg_index.clear()
        self._orig_detail = scores365._get_game_detail
        scores365._get_game_detail = lambda game_id: None
        self._orig_render = mergetracker.scoreimage.render_score_card
        mergetracker.scoreimage.render_score_card = lambda *a, **k: b"fake-image-bytes"

    def tearDown(self):
        for mid in self._registered_tracker:
            tracker.unregister_message(mid)
        for mid in self._registered_dc:
            doublechancetracker.unregister_message(mid)
        mergetracker.state.load_merges = self._orig_load
        mergetracker.state.save_merges = self._orig_save
        mergetracker._leg_index.clear()
        scores365._get_game_detail = self._orig_detail
        mergetracker.scoreimage.render_score_card = self._orig_render

    def _register_tracker_leg(self, mid, channel_id, game_id, picked_team=None, total_direction=None, total_line=None):
        tracker.register_message(mid, channel_id, game_id, 1, picked_team, None, total_direction, total_line)
        self._registered_tracker.append(mid)

    def _register_dc_leg(self, mid, channel_id, game_id):
        doublechancetracker.register_message(mid, channel_id, game_id, 1)
        self._registered_dc.append(mid)

    def test_needs_at_least_two_ids(self):
        result = asyncio.run(mergetracker.create_merge(_FakeChannel(), 555, [1]))
        self.assertIn("at least 2", result)

    def test_unresolvable_id_is_rejected(self):
        self._register_tracker_leg(1, 555, 100)
        result = asyncio.run(mergetracker.create_merge(_FakeChannel(), 555, [1, 999999]))
        self.assertIn("Not currently tracked", result)
        self.assertIn("999999", result)

    def test_wrong_channel_id_is_rejected(self):
        self._register_tracker_leg(1, 555, 100)
        self._register_dc_leg(2, 777, 100)
        result = asyncio.run(mergetracker.create_merge(_FakeChannel(), 555, [1, 2]))
        self.assertIn("Not from this channel", result)

    def test_mismatched_game_is_rejected(self):
        self._register_tracker_leg(1, 555, 100)
        self._register_dc_leg(2, 555, 200)
        result = asyncio.run(mergetracker.create_merge(_FakeChannel(), 555, [1, 2]))
        self.assertIn("aren't all the same match", result)

    def test_same_game_across_both_modules_succeeds(self):
        self._register_tracker_leg(1, 555, 100, total_direction="over", total_line=2.5)
        self._register_dc_leg(2, 555, 100)
        channel = _FakeChannel()
        channel.add(_FakeMessage(1, embeds=[_FakeEmbed(description="Over 2.5", author_name="Soccer • LaLiga")]))
        channel.add(_FakeMessage(2, embeds=[_FakeEmbed(description="Double Chance: Draw or Atletico Madrid")]))

        result = asyncio.run(mergetracker.create_merge(channel, 555, [1, 2]))

        self.assertIn("Merged 2 leg(s)", result)
        self.assertIn("555:100", self._data)
        group = self._data["555:100"]
        self.assertEqual(group["header"], "Soccer • LaLiga")
        labels = {leg["label"] for leg in group["legs"].values()}
        self.assertEqual(labels, {"Over 2.5", "Double Chance: Draw or Atletico Madrid"})
        # Both originals deleted, one new combined card posted.
        self.assertTrue(channel.messages[1].deleted)
        self.assertTrue(channel.messages[2].deleted)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(mergetracker.merged_into(555, "tracker", tracker.track_key(555, 100, None, None, "over", 2.5)), "555:100")
        self.assertEqual(mergetracker.merged_into(555, "doublechancetracker", doublechancetracker.track_key(555, 100)), "555:100")

    def test_rejects_merging_into_an_already_merged_game(self):
        self._data["555:100"] = {"channel_id": 555, "game_id": 100, "legs": {}}
        self._register_tracker_leg(1, 555, 100, total_direction="over", total_line=2.5)
        self._register_dc_leg(2, 555, 100)
        channel = _FakeChannel()
        channel.add(_FakeMessage(1, embeds=[_FakeEmbed(description="Over 2.5")]))
        channel.add(_FakeMessage(2, embeds=[_FakeEmbed(description="Double Chance: Draw or Atletico Madrid")]))
        result = asyncio.run(mergetracker.create_merge(channel, 555, [1, 2]))
        self.assertIn("already has a merged card", result)


if __name__ == "__main__":
    unittest.main()

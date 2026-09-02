#!/usr/bin/env python3
"""
Regression tests for parlaytracker.resolve_leg - it reconstructs each
tracker module's own track_key/prop_key from that module's in-memory
_message_owners tuple, which must stay in sync with (a) that tuple's
actual shape and (b) its track_key/prop_key's full signature. See
resolve_leg's own docstring for the real-world break this guards against:
confirmed live, several of these had drifted out of sync as each module's
owner tuple grew extra discriminator fields over time - most crashed
/parlay add outright ("too many values to unpack", which left a deferred
interaction stuck on "thinking..." forever since the reply never got
sent), a couple more silently rebuilt the wrong key instead, and
halftracker was missing from this function entirely.

Run with: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import boxingtracker
import esportstracker
import f5tracker
import halftracker
import inning1tracker
import inningtracker
import kboproptracker
import parlaytracker
import proptracker
import settracker
import soccerpropstracker
import tennispropstracker
import tracker
import ufctracker


class _FakeResponse:
    status = 429
    reason = "Too Many Requests"


def _edit_cap_error() -> discord.HTTPException:
    return discord.HTTPException(_FakeResponse(), "Maximum number of edits to messages older than 1 hour reached.")


class ResolveLegMatchesEachModulesOwnerShape(unittest.TestCase):
    def setUp(self):
        self._registered = []

    def tearDown(self):
        # Every module's _message_owners is a module-level global - clear
        # what this test registered so it can't leak into another test.
        for mod, msg_id in self._registered:
            mod.unregister_message(msg_id)

    def _register(self, mod, msg_id, *args, **kwargs):
        mod.register_message(msg_id, *args, **kwargs)
        self._registered.append((mod, msg_id))

    def test_tracker_moneyline(self):
        self._register(tracker, 1, 555, 100, 1, "Denver Broncos")
        self.assertEqual(
            parlaytracker.resolve_leg(1),
            ("tracker", tracker.track_key(555, 100, "Denver Broncos"), 555),
        )

    def test_tracker_team_total_spread(self):
        # The exact shape confirmed live to crash resolve_leg: a
        # team_total pick (this session's NFL spread pick is one) stores a
        # 7-wide tuple, but resolve_leg used to unpack only 3.
        self._register(tracker, 2, 555, 100, 1, None, "Denver Broncos", "spread", -3.5)
        self.assertEqual(
            parlaytracker.resolve_leg(2),
            ("tracker", tracker.track_key(555, 100, None, "Denver Broncos", "spread", -3.5), 555),
        )

    def test_proptracker(self):
        self._register(proptracker, 3, 555, "401", "123", ("Points", "nba"), 1, "over", 24.5)
        self.assertEqual(
            parlaytracker.resolve_leg(3),
            ("proptracker", proptracker.prop_key(555, "401", "123", ("Points", "nba"), "over", 24.5), 555),
        )

    def test_inningtracker_unaffected(self):
        self._register(inningtracker, 4, 555, "401", "yrfi", None, 1)
        self.assertEqual(
            parlaytracker.resolve_leg(4),
            ("inningtracker", inningtracker.track_key(555, "401", "yrfi"), 555),
        )

    def test_inningtracker_total_runs_line(self):
        # The owner tuple grew a `line` field for the general "1st Inning
        # Total Runs Over/Under N" market (YRFI/NRFI never pass one, hence
        # the separate case above) - confirmed this stays in sync since
        # track_key folds a non-None line into the key string.
        self._register(inningtracker, 40, 555, "401", "INNING1_TOTAL_OVER", 1.5, 1)
        self.assertEqual(
            parlaytracker.resolve_leg(40),
            ("inningtracker", inningtracker.track_key(555, "401", "INNING1_TOTAL_OVER", 1.5), 555),
        )

    def test_f5tracker_handicap(self):
        self._register(f5tracker, 5, 555, 100, 1, "Kia Tigers", None, None, 0.5)
        self.assertEqual(
            parlaytracker.resolve_leg(5),
            ("f5tracker", f5tracker.track_key(555, 100, "Kia Tigers", None, None, 0.5), 555),
        )

    def test_halftracker_was_completely_missing(self):
        self._register(halftracker, 6, 555, 100, 1, "Arizona Cardinals", "over", 10.5)
        self.assertEqual(
            parlaytracker.resolve_leg(6),
            ("halftracker", halftracker.track_key(555, 100, "Arizona Cardinals", "over", 10.5), 555),
        )

    def test_inning1tracker_unaffected(self):
        self._register(inning1tracker, 7, 555, 100, 1)
        self.assertEqual(
            parlaytracker.resolve_leg(7),
            ("inning1tracker", inning1tracker.track_key(555, 100), 555),
        )

    def test_settracker_unaffected(self):
        self._register(settracker, 8, 555, 100, "set1_moneyline", "Naomi Osaka", 1)
        self.assertEqual(
            parlaytracker.resolve_leg(8),
            ("settracker", settracker.track_key(555, 100, "set1_moneyline", "Naomi Osaka"), 555),
        )

    def test_soccerpropstracker(self):
        self._register(soccerpropstracker, 9, 555, 100, "42", "Assists", 1, "over", 0.5)
        self.assertEqual(
            parlaytracker.resolve_leg(9),
            ("soccerpropstracker", soccerpropstracker.prop_key(555, 100, "42", "Assists", "over", 0.5), 555),
        )

    def test_tennispropstracker(self):
        self._register(tennispropstracker, 10, 555, 100, "88", "Aces", 1, "under", 8.5)
        self.assertEqual(
            parlaytracker.resolve_leg(10),
            ("tennispropstracker", tennispropstracker.prop_key(555, 100, "88", "Aces", "under", 8.5), 555),
        )

    def test_ufctracker_round_total(self):
        self._register(ufctracker, 11, 555, 100, 1, None, "over", 2.5)
        self.assertEqual(
            parlaytracker.resolve_leg(11),
            ("ufctracker", ufctracker.track_key(555, 100, None, "over", 2.5), 555),
        )

    def test_boxingtracker(self):
        self._register(boxingtracker, 13, 555, 10758, 4524, 1)
        self.assertEqual(
            parlaytracker.resolve_leg(13),
            ("boxingtracker", boxingtracker.track_key(555, 10758, 4524), 555),
        )

    def test_kboproptracker(self):
        self._register(kboproptracker, 14, 555, "53123", "Total Bases", "over", 0.5, "08.16", 1)
        self.assertEqual(
            parlaytracker.resolve_leg(14),
            ("kboproptracker", kboproptracker.track_key(555, "53123", "Total Bases", "over", 0.5, "08.16"), 555),
        )

    def test_esportstracker_unaffected(self):
        self._register(esportstracker, 12, 555, "cs2", "Team A", "Team B", "match_winner", 1)
        self.assertEqual(
            parlaytracker.resolve_leg(12),
            ("esportstracker", esportstracker.track_key(555, "cs2", "Team A", "Team B", "match_winner"), 555),
        )

    def test_unknown_message_id_returns_none(self):
        self.assertIsNone(parlaytracker.resolve_leg(999999))


class ReportLegProgressUnResolvesAStaleTerminalLeg(unittest.TestCase):
    """report_leg_progress used to blindly overwrite a leg's status back to
    "pending" without ever touching the group's own aggregate counters -
    fine for a leg reporting live progress for the first time, but a real
    bug once a leg that was already counted as resolved (most commonly
    voided by a tracker's own MAX_CONSECUTIVE_MISSES safety net after a
    transient data-source hiccup) starts reporting live progress again
    (e.g. manually re-/track'ed after discovering the match was still
    live). Confirmed live: a 6-leg parlay with 4 legs genuinely still
    pending got stuck thinking only 3 more results were needed (still
    carrying a phantom void's +1 resolved/+1 voided/-1 total from before
    that leg resumed), which would have finalized and deleted the group's
    own tracking one leg early - silently dropping whichever leg finished
    last, with no summary card ever showing its result.

    Monkeypatches state.load_parlays/save_parlays (an in-memory dict
    instead of the real JSON file) and _post_or_edit_summary (a no-op -
    this test isn't exercising real Discord posting) so this drives the
    real aggregate-counter logic without any network/file I/O."""

    def setUp(self):
        self._orig_load = parlaytracker.state.load_parlays
        self._orig_save = parlaytracker.state.save_parlays
        self._orig_post = parlaytracker._post_or_edit_summary
        self._data: dict = {}
        parlaytracker.state.load_parlays = lambda: self._data
        parlaytracker.state.save_parlays = lambda data: self._data.update(data)
        parlaytracker._post_or_edit_summary = self._fake_post

    def tearDown(self):
        parlaytracker.state.load_parlays = self._orig_load
        parlaytracker.state.save_parlays = self._orig_save
        parlaytracker._post_or_edit_summary = self._orig_post

    async def _fake_post(self, channel, channel_id, group):
        return 999

    def _report(self, leg_id_status: str, detail: str = "LIVE, Set 3"):
        key = "555:testparlay"
        self._data[key] = {
            "channel_id": 555, "identifier": "testparlay", "total_legs": 5, "resolved_legs": 3,
            "won": 2, "voided": 1, "lost": leg_id_status == "lost", "summary_message_id": 1,
            "legs": {
                "tracker:555:4612254:ml:Hungary": {
                    "label": "Latvia vs Hungary - Hungary ML", "status": leg_id_status,
                    "detail": "VOID", "message_id": 111,
                },
            },
        }
        asyncio.run(parlaytracker.report_leg_progress(
            None, 555, None, "tracker", "555:4612254:ml:Hungary", "Latvia vs Hungary - Hungary ML", detail, ["testparlay"],
        ))
        return self._data["555:testparlay"]

    def test_a_voided_leg_reporting_live_again_gives_back_its_void_count(self):
        group = self._report("void")
        self.assertEqual(group["resolved_legs"], 2)
        self.assertEqual(group["voided"], 0)
        self.assertEqual(group["total_legs"], 6)
        self.assertEqual(group["legs"]["tracker:555:4612254:ml:Hungary"]["status"], "pending")
        self.assertEqual(group["legs"]["tracker:555:4612254:ml:Hungary"]["detail"], "LIVE, Set 3")

    def test_a_won_leg_reporting_live_again_gives_back_its_win_count(self):
        group = self._report("won")
        self.assertEqual(group["resolved_legs"], 2)
        self.assertEqual(group["won"], 1)
        self.assertEqual(group["total_legs"], 5)  # unchanged - only void/push give a slot back

    def test_a_lost_group_stays_lost_even_if_this_one_leg_resumes(self):
        # Can't tell whether THIS leg or some other one is why the group
        # is marked lost - only resolved_legs itself gets given back.
        group = self._report("lost")
        self.assertEqual(group["resolved_legs"], 2)
        self.assertTrue(group["lost"])

    def test_a_genuinely_still_pending_leg_is_left_alone(self):
        # The normal, everyday case (a leg reporting its Nth live tick, not
        # a resumed-after-terminal one) must not touch the counters at all.
        group = self._report("pending")
        self.assertEqual(group["resolved_legs"], 3)
        self.assertEqual(group["voided"], 1)
        self.assertEqual(group["total_legs"], 5)


class _FakeMessage:
    def __init__(self, message_id: int, edit_error: Exception = None):
        self.id = message_id
        self._edit_error = edit_error

    async def edit(self, **kwargs):
        if self._edit_error:
            raise self._edit_error

    async def delete(self):
        pass


class _FakeChannel:
    """fetch_message always returns the one message currently "live" (its
    id and whether editing it raises are swapped in per test case); send
    creates a fresh one and records it, same shape _post_or_edit_summary
    actually calls."""

    def __init__(self, existing: _FakeMessage):
        self.existing = existing
        self.sent: list[_FakeMessage] = []
        self._next_id = 9000

    async def fetch_message(self, message_id):
        if self.existing is None or self.existing.id != message_id:
            raise discord.HTTPException(_FakeResponse(), "Unknown Message")
        return self.existing

    async def send(self, embed=None):
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.sent.append(msg)
        self.existing = msg
        return msg


class PostOrEditSummaryThrottlesFailureReposts(unittest.TestCase):
    """_post_or_edit_summary used to fall back to posting a brand-new card
    on EVERY single edit failure with no throttling - fine for a one-off,
    but once a card permanently can't be edited anymore (Discord's own
    edit-count cap on a message older than 1 hour, error 30046), every
    still-pending leg's own independent poll tick (report_leg_progress
    calls this on every one) each triggered their own fresh repost, over
    and over. Confirmed live: a parlay with several legs still live
    produced a new summary card roughly every 10-30 seconds, unbounded,
    for as long as any leg stayed pending."""

    def _base_group(self, **overrides) -> dict:
        group = {
            "identifier": "testparlay", "total_legs": 3, "resolved_legs": 0,
            "won": 0, "voided": 0, "lost": False, "legs": {},
        }
        group.update(overrides)
        return group

    def test_first_failure_reposts_immediately(self):
        group = self._base_group(summary_message_id=1234)
        channel = _FakeChannel(_FakeMessage(1234, edit_error=_edit_cap_error()))
        new_id = asyncio.run(parlaytracker._post_or_edit_summary(channel, 555, group))
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(new_id, channel.sent[0].id)
        self.assertIn("last_repost_attempt", group)

    def test_a_second_failure_moments_later_is_throttled_not_reposted(self):
        group = self._base_group(summary_message_id=1234, last_repost_attempt=time.time())
        channel = _FakeChannel(_FakeMessage(1234, edit_error=_edit_cap_error()))
        result = asyncio.run(parlaytracker._post_or_edit_summary(channel, 555, group))
        self.assertEqual(len(channel.sent), 0)
        self.assertEqual(result, 1234)  # unchanged - no new card posted

    def test_after_the_cooldown_elapses_it_reposts_again(self):
        group = self._base_group(
            summary_message_id=1234,
            last_repost_attempt=time.time() - parlaytracker._REPOST_ON_FAILURE_COOLDOWN_SECONDS - 1,
        )
        channel = _FakeChannel(_FakeMessage(1234, edit_error=_edit_cap_error()))
        new_id = asyncio.run(parlaytracker._post_or_edit_summary(channel, 555, group))
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(new_id, channel.sent[0].id)

    def test_a_successful_edit_is_never_throttled(self):
        group = self._base_group(summary_message_id=1234, last_repost_attempt=time.time())
        channel = _FakeChannel(_FakeMessage(1234, edit_error=None))
        result = asyncio.run(parlaytracker._post_or_edit_summary(channel, 555, group))
        self.assertEqual(len(channel.sent), 0)
        self.assertEqual(result, 1234)

    def test_the_very_first_post_ever_is_never_throttled(self):
        group = self._base_group(summary_message_id=None)
        channel = _FakeChannel(existing=None)
        new_id = asyncio.run(parlaytracker._post_or_edit_summary(channel, 555, group))
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(new_id, channel.sent[0].id)


class AutoDeleteOldGroups(unittest.TestCase):
    """auto_delete_old_groups sweeps every channel's /parlay groups for ones
    at least AUTO_DELETE_AGE_SECONDS old - regardless of resolution status
    (a still-active group just stops aggregating; its legs' own trackers
    keep running independently). A group with no "created_at" at all (from
    before this field existed) is treated as age 0/the epoch, not skipped,
    so the pre-existing backlog gets swept on the first pass too."""

    def setUp(self):
        self._orig_load = parlaytracker.state.load_parlays
        self._orig_save = parlaytracker.state.save_parlays
        self._data: dict = {}
        parlaytracker.state.load_parlays = lambda: self._data
        parlaytracker.state.save_parlays = lambda data: self._data.update(data)

    def tearDown(self):
        parlaytracker.state.load_parlays = self._orig_load
        parlaytracker.state.save_parlays = self._orig_save

    def _group(self, channel_id, identifier, created_at=None):
        entry = {
            "channel_id": channel_id, "identifier": identifier, "total_legs": 1,
            "resolved_legs": 0, "won": 0, "voided": 0, "lost": False,
            "summary_message_id": 1, "legs": {},
        }
        if created_at is not None:
            entry["created_at"] = created_at
        self._data[f"{channel_id}:{identifier}"] = entry

    def test_old_group_gets_deleted(self):
        self._group(555, "old", created_at=time.time() - parlaytracker.AUTO_DELETE_AGE_SECONDS - 1)
        deleted = asyncio.run(parlaytracker.auto_delete_old_groups())
        self.assertEqual(deleted, [(555, "old")])
        self.assertNotIn("555:old", self._data)

    def test_recent_group_is_left_alone(self):
        self._group(555, "fresh", created_at=time.time())
        deleted = asyncio.run(parlaytracker.auto_delete_old_groups())
        self.assertEqual(deleted, [])
        self.assertIn("555:fresh", self._data)

    def test_group_missing_created_at_is_treated_as_old(self):
        self._group(555, "legacy", created_at=None)
        deleted = asyncio.run(parlaytracker.auto_delete_old_groups())
        self.assertEqual(deleted, [(555, "legacy")])

    def test_active_group_is_deleted_too_regardless_of_pending_legs(self):
        self._group(555, "stillgoing", created_at=time.time() - parlaytracker.AUTO_DELETE_AGE_SECONDS - 1)
        self._data["555:stillgoing"]["resolved_legs"] = 0
        self._data["555:stillgoing"]["total_legs"] = 3
        deleted = asyncio.run(parlaytracker.auto_delete_old_groups())
        self.assertEqual(deleted, [(555, "stillgoing")])


if __name__ == "__main__":
    unittest.main()

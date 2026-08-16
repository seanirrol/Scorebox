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

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        self._register(inningtracker, 4, 555, "401", "yrfi", 1)
        self.assertEqual(
            parlaytracker.resolve_leg(4),
            ("inningtracker", inningtracker.track_key(555, "401", "yrfi"), 555),
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


if __name__ == "__main__":
    unittest.main()

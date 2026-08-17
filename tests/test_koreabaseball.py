#!/usr/bin/env python3
"""
Regression tests for koreabaseball.py's pure computation logic - against
constructed game-log rows, not live eng.koreabaseball.com scraping (that
side was confirmed live during development instead - see this module's own
docstring for the full source-evaluation story: mykbostats.com and BoxRec
were both Cloudflare-blocked, eng.koreabaseball.com works via plain
requests, and the exact three players from a real mismatch bug - Gwak
Been, Austin Dean, Sam Hilliard - were confirmed resolvable via its
Batting/Pitching Leaders pages).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import koreabaseball as kbo


class ParseIpOuts(unittest.TestCase):
    def test_whole_innings_only(self):
        self.assertEqual(kbo._parse_ip_outs("6"), 18)

    def test_one_third(self):
        self.assertEqual(kbo._parse_ip_outs("4 2/3"), 14)

    def test_two_thirds(self):
        self.assertEqual(kbo._parse_ip_outs("5 1/3"), 16)

    def test_blank_returns_none(self):
        self.assertIsNone(kbo._parse_ip_outs(""))
        self.assertIsNone(kbo._parse_ip_outs(None))


class StatValue(unittest.TestCase):
    def _hitter_row(self, **overrides):
        row = {"H": "2", "2B": "1", "3B": "0", "HR": "0", "R": "1", "RBI": "1", "BB": "0", "SO": "1"}
        row.update(overrides)
        return row

    def _pitcher_row(self, **overrides):
        row = {"K": "10", "BB": "1", "ER": "0", "IP": "6"}
        row.update(overrides)
        return row

    def test_total_bases_computed_from_hits_and_extra_base_hits(self):
        # 2 hits, 1 double, 0 triples, 0 HR - 1 single + 1 double = 1*1 + 1*2 = 3.
        row = self._hitter_row(H="2", **{"2B": "1", "3B": "0", "HR": "0"})
        self.assertEqual(kbo.stat_value(row, "Total Bases", is_pitcher=False), 3.0)

    def test_total_bases_with_a_home_run(self):
        # Confirmed-shape example: Gwak Been's real pitching line wasn't
        # this, but Austin Dean's real hitter row (0-for-3, 2 SO) shows the
        # zero case cleanly: no hits at all -> 0 total bases.
        row = self._hitter_row(H="0", **{"2B": "0", "3B": "0", "HR": "0"})
        self.assertEqual(kbo.stat_value(row, "Total Bases", is_pitcher=False), 0.0)

    def test_plain_hitter_column(self):
        row = self._hitter_row(SO="4")
        self.assertEqual(kbo.stat_value(row, "Strikeouts (Batting)", is_pitcher=False), 4.0)

    def test_plain_pitcher_column(self):
        row = self._pitcher_row(K="10")
        self.assertEqual(kbo.stat_value(row, "Strikeouts (Pitching)", is_pitcher=True), 10.0)

    def test_innings_pitched_from_ip_notation(self):
        row = self._pitcher_row(IP="4 2/3")
        self.assertAlmostEqual(kbo.stat_value(row, "Innings Pitched", is_pitcher=True), 14 / 3)

    def test_pitching_outs_from_ip_notation(self):
        row = self._pitcher_row(IP="4 2/3")
        self.assertEqual(kbo.stat_value(row, "Pitching Outs", is_pitcher=True), 14.0)

    def test_missing_column_returns_none(self):
        self.assertIsNone(kbo.stat_value({}, "Strikeouts (Pitching)", is_pitcher=True))


class StatSupported(unittest.TestCase):
    def test_hitter_stats_not_supported_for_a_pitcher(self):
        self.assertFalse(kbo.stat_supported("Total Bases", is_pitcher=True))
        self.assertTrue(kbo.stat_supported("Total Bases", is_pitcher=False))

    def test_pitcher_stats_not_supported_for_a_hitter(self):
        self.assertFalse(kbo.stat_supported("Strikeouts (Pitching)", is_pitcher=False))
        self.assertTrue(kbo.stat_supported("Strikeouts (Pitching)", is_pitcher=True))


class FindGameRow(unittest.TestCase):
    """_game_rows itself hits the network (exercised live during
    development instead - see module docstring) - monkeypatched here so
    find_game_row's own date-match logic gets real coverage without a live
    request."""

    def setUp(self):
        self._orig_game_rows = kbo._game_rows
        kbo._game_rows = lambda pcode, is_pitcher: [
            {"DATE": "08.11", "K": "10"}, {"DATE": "08.16", "K": "5"},
        ]

    def tearDown(self):
        kbo._game_rows = self._orig_game_rows

    def test_matches_by_exact_date(self):
        self.assertEqual(kbo.find_game_row("68220", True, "08.16"), {"DATE": "08.16", "K": "5"})

    def test_no_row_for_date_returns_none(self):
        self.assertIsNone(kbo.find_game_row("68220", True, "08.20"))


class GameDateEastern(unittest.TestCase):
    """Backs kboproptracker.py's own record_pick game_date - confirmed
    live noon KST wasn't a safe anchor (it converts to 11 PM the
    *previous* Eastern day, crossing midnight), so this locks in the 2 PM
    KST anchor actually being used instead."""

    def test_matches_the_kst_date_number(self):
        # A realistic KBO start time (2 PM+ KST) should land on the same
        # numbered Eastern date, not the day before or after. `now` is
        # 2026-06-01 noon KST, purely to fix the year used.
        self.assertEqual(kbo.game_date_eastern("08.17", now=1780282800.0), "2026-08-17")

    def test_year_is_derived_from_now_not_hardcoded(self):
        # `now` is 2024-01-01 noon KST - a year far from the other test's,
        # to prove the year comes from `now` rather than a baked-in
        # constant.
        self.assertEqual(kbo.game_date_eastern("01.05", now=1704078000.0), "2024-01-05")


if __name__ == "__main__":
    unittest.main()

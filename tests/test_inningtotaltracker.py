#!/usr/bin/env python3
"""
Regression tests for inningtotaltracker.py's pure helpers (track_key's line
suffix, _pick_label, _grade) - mirrors test_inningtracker.py's scope for the
365scores-backed equivalent (see that module's own docstring for why it
exists separately from inningtracker.py).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inningtotaltracker


class TrackKeyLineSuffix(unittest.TestCase):
    def test_no_line_matches_pre_existing_yrfi_nrfi_key_shape(self):
        self.assertEqual(inningtotaltracker.track_key(555, "401", "YRFI"), "555:401:YRFI")

    def test_line_appends_a_distinguishing_suffix(self):
        self.assertEqual(inningtotaltracker.track_key(555, "401", "INNING1_TOTAL_OVER", 1.5), "555:401:INNING1_TOTAL_OVER:1.5")

    def test_different_lines_on_the_same_game_get_different_keys(self):
        self.assertNotEqual(
            inningtotaltracker.track_key(555, "401", "INNING1_TOTAL_OVER", 1.5),
            inningtotaltracker.track_key(555, "401", "INNING1_TOTAL_OVER", 2.5),
        )


class PickLabel(unittest.TestCase):
    def test_yrfi_uses_the_static_label(self):
        self.assertEqual(inningtotaltracker._pick_label("YRFI"), "Yes Runs 1st Inning")

    def test_nrfi_uses_the_static_label(self):
        self.assertEqual(inningtotaltracker._pick_label("NRFI"), "No Runs 1st Inning")

    def test_total_over_includes_the_line(self):
        self.assertEqual(inningtotaltracker._pick_label("INNING1_TOTAL_OVER", 1.5), "Over 1.5 1st Inning Runs")

    def test_total_under_includes_the_line(self):
        self.assertEqual(inningtotaltracker._pick_label("INNING1_TOTAL_UNDER", 2.5), "Under 2.5 1st Inning Runs")


class Grade(unittest.TestCase):
    def test_yrfi_wins_when_a_run_scores(self):
        self.assertEqual(inningtotaltracker._grade("YRFI", None, 1, 0), "won")

    def test_yrfi_loses_when_no_run_scores(self):
        self.assertEqual(inningtotaltracker._grade("YRFI", None, 0, 0), "lost")

    def test_nrfi_wins_when_no_run_scores(self):
        self.assertEqual(inningtotaltracker._grade("NRFI", None, 0, 0), "won")

    def test_nrfi_loses_when_either_side_scores(self):
        self.assertEqual(inningtotaltracker._grade("NRFI", None, 0, 1), "lost")

    def test_total_over_clears_the_line(self):
        self.assertEqual(inningtotaltracker._grade("INNING1_TOTAL_OVER", 1.5, 1, 1), "won")

    def test_total_over_falls_short_of_the_line(self):
        self.assertEqual(inningtotaltracker._grade("INNING1_TOTAL_OVER", 1.5, 1, 0), "lost")

    def test_total_under_stays_below_the_line(self):
        self.assertEqual(inningtotaltracker._grade("INNING1_TOTAL_UNDER", 1.5, 1, 0), "won")

    def test_total_under_exceeds_the_line(self):
        self.assertEqual(inningtotaltracker._grade("INNING1_TOTAL_UNDER", 1.5, 1, 1), "lost")


if __name__ == "__main__":
    unittest.main()

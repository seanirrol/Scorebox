#!/usr/bin/env python3
"""
Regression tests for esports.py's grading logic - against constructed
series_data dicts, not live hawk.live/GosuGamers scraping (that side was
confirmed live during development instead - see esportstracker.py's own
module docstring).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esports


def _series(status, home_score, away_score, home_team="Team A", away_team="Team B"):
    return {
        "sport": "dota2", "status": status,
        "home_team": home_team, "away_team": away_team,
        "home_score": home_score, "away_score": away_score,
    }


class GradeWinAtLeastOneMap(unittest.TestCase):
    def test_locked_in_but_series_still_live_returns_none(self):
        # Confirmed live: a still-live best-of-3 (1-0, picked team already
        # has its one map) used to grade "won" here and immediately bump
        # the card to Final, ending live tracking mid-series. Must wait for
        # the series to actually finish instead.
        series = _series("inprogress", 1, 0)
        self.assertIsNone(esports.grade_win_at_least_one_map(series, "Team A"))

    def test_series_finished_picked_team_has_a_map_wins(self):
        series = _series("finished", 1, 2)
        self.assertEqual(esports.grade_win_at_least_one_map(series, "Team A"), "won")

    def test_series_finished_picked_team_swept_loses(self):
        series = _series("finished", 0, 2)
        self.assertEqual(esports.grade_win_at_least_one_map(series, "Team A"), "lost")

    def test_no_direction_still_live_returns_none(self):
        series = _series("inprogress", 1, 0)
        self.assertIsNone(esports.grade_win_at_least_one_map(series, "Team A", "no"))

    def test_no_direction_series_finished_picked_swept_wins(self):
        series = _series("finished", 0, 2)
        self.assertEqual(esports.grade_win_at_least_one_map(series, "Team A", "no"), "won")

    def test_no_direction_series_finished_picked_has_a_map_loses(self):
        series = _series("finished", 1, 2)
        self.assertEqual(esports.grade_win_at_least_one_map(series, "Team A", "no"), "lost")

    def test_unrelated_team_returns_none(self):
        series = _series("finished", 1, 2)
        self.assertIsNone(esports.grade_win_at_least_one_map(series, "Team C"))


if __name__ == "__main__":
    unittest.main()

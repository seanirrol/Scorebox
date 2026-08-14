#!/usr/bin/env python3
"""
Regression tests for espn.py's computed-stat conversions.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import espn


def _boxscore_event(ip_raw, entity_id="1", home=True):
    team = {"id": "10" if home else "20"}
    other_team = {"id": "20" if home else "10"}
    row = {
        "team": team,
        "statistics": [{
            "labels": ["IP", "H", "R", "ER", "BB", "K", "HR", "PC-ST", "ERA", "PC"],
            "athletes": [{"athlete": {"id": entity_id}, "stats": [ip_raw, "3", "2", "2", "2", "6", "1", "102-59", "2.59", "102"]}],
        }],
    }
    other_row = {"team": other_team, "statistics": []}
    return {
        "header": {"competitions": [{"status": {"type": {"state": "post"}}}]},
        "boxscore": {"players": [row, other_row] if home else [other_row, row]},
    }


class PitchingOutsConversion(unittest.TestCase):
    """ESPN's raw "IP" boxscore field uses baseball notation - "5.2" means
    5 full innings plus 2 outs (17 outs total), NOT 5.2 decimal innings -
    confirmed live against a real finished game's boxscore. Pitching Outs
    is a real, distinct prop market that needs this converted properly."""

    def test_partial_inning_two_outs(self):
        event = _boxscore_event("5.2")
        value, is_home, team = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 17)

    def test_partial_inning_one_out(self):
        event = _boxscore_event("0.1")
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 1)

    def test_exact_inning_no_partial(self):
        event = _boxscore_event("6.0")
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 18)

    def test_player_not_in_boxscore_returns_none(self):
        event = _boxscore_event("5.2")
        value, _, _ = espn.get_stat_value(event, "does-not-exist", espn.PITCHING_OUTS_KEY)
        self.assertIsNone(value)

    def test_grades_correctly_against_an_over_line(self):
        event = _boxscore_event("5.2")  # 17 outs
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(espn.grade_over_under(value, "over", 17.5), "lost")
        self.assertEqual(espn.grade_over_under(value, "over", 16.5), "won")


if __name__ == "__main__":
    unittest.main()

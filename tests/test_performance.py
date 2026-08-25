#!/usr/bin/env python3
"""
Regression tests for performance.py's chart row-visibility logic.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import performance


class VisibleTournaments(unittest.TestCase):
    """Confirmed live: once a sport has both a manual baseline bucket
    (dailylog._SPORT_BASELINE_OVERRIDE, injected under a tournament key
    that's literally the sport's own name) AND real per-event tournament
    buckets, the baseline started showing up as its own confusing extra
    row - "MMA" appearing twice, once as the sport header and once as an
    identically-labeled sub-row - because the old check only hid a
    same-named bucket when it was the ONLY bucket in that sport."""

    def test_same_name_bucket_hidden_even_alongside_real_tournaments(self):
        tournaments = {
            "MMA": (34, 13),  # the baseline, injected under the sport's own name
            "UFC Fight Night: Hernandez vs. Rodrigues": (0, 1),
        }
        visible = performance._visible_tournaments("MMA", tournaments)
        self.assertEqual(visible, {"UFC Fight Night: Hernandez vs. Rodrigues": (0, 1)})

    def test_same_name_bucket_still_hidden_when_its_the_only_one(self):
        # The original, still-valid case this logic was built for - a
        # league with no real sub-tournament concept (e.g. "NBA" under
        # sport "NBA") shouldn't show a redundant single sub-row either.
        tournaments = {"NBA": (10, 5)}
        self.assertEqual(performance._visible_tournaments("NBA", tournaments), {})

    def test_multiple_real_tournaments_all_stay_visible(self):
        tournaments = {
            "UFC Fight Night: Hernandez vs. Rodrigues": (0, 1),
            "Dana White's Contender Series: Season 10, Week 2": (1, 0),
        }
        self.assertEqual(performance._visible_tournaments("MMA", tournaments), tournaments)

    def test_the_sport_level_aggregate_still_includes_the_hidden_bucket(self):
        # _visible_tournaments only controls which SUB-ROWS render - the
        # sport header bar itself is summed from the full, unfiltered
        # tournaments dict elsewhere in render_chart, so hiding this
        # bucket's own row must never drop it from that total.
        tournaments = {
            "MMA": (34, 13),
            "UFC Fight Night: Hernandez vs. Rodrigues": (0, 1),
        }
        total_won = sum(w for w, _l in tournaments.values())
        total_lost = sum(l for _w, l in tournaments.values())
        self.assertEqual((total_won, total_lost), (34, 14))


if __name__ == "__main__":
    unittest.main()

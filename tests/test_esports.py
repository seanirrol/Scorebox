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
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esports


def _series(status, home_score, away_score, home_team="Team A", away_team="Team B"):
    return {
        "sport": "dota2", "status": status,
        "home_team": home_team, "away_team": away_team,
        "home_score": home_score, "away_score": away_score,
    }


def _hawk_series(
    games, home_is_team1=True, status="inprogress", home_score=0, away_score=0,
    home_team="Team A", away_team="Team B", series_id="s1",
):
    """Builds a series_data dict backed by the hawklive provider - seeds
    esports._hawk_detail_cache directly with the map list instead of hitting
    the network, so map_winners()/map_kills() (and anything grading off
    them) can run against hand-built per-map state data."""
    esports._hawk_detail_cache[series_id] = (games, time.time())
    return {
        "sport": "dota2", "status": status,
        "home_team": home_team, "away_team": away_team,
        "home_score": home_score, "away_score": away_score,
        "_ref": {"provider": "hawklive", "series": {"id": series_id}, "home_is_team1": home_is_team1},
    }


def _map(number, is_team1_radiant, radiant_score, dire_score, winner=True):
    return {
        "number": number, "isTeam1Radiant": is_team1_radiant,
        "isRadiantWinner": winner, "states": [{"radiantScore": radiant_score, "direScore": dire_score}],
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


class GradeMapKillsHandicap(unittest.TestCase):
    def test_picked_home_covers_the_spread_wins(self):
        series = _hawk_series([_map(1, True, 32, 20)], series_id="win")
        self.assertEqual(esports.grade_map_kills_handicap(series, 1, "Team A", -4.5), "won")

    def test_picked_home_fails_to_cover_loses(self):
        series = _hawk_series([_map(1, True, 20, 32)], series_id="lose")
        self.assertEqual(esports.grade_map_kills_handicap(series, 1, "Team A", -4.5), "lost")

    def test_exact_push(self):
        series = _hawk_series([_map(1, True, 24, 20)], series_id="push")
        self.assertEqual(esports.grade_map_kills_handicap(series, 1, "Team A", -4), "push")

    def test_map_never_played_but_series_decided_voids(self):
        series = _hawk_series([_map(1, True, 0, 0, winner=None)], status="finished", series_id="void1")
        self.assertEqual(esports.grade_map_kills_handicap(series, 1, "Team A", -4.5), "void")

    def test_map_number_beyond_any_recorded_map_but_series_decided_voids(self):
        series = _hawk_series([], status="finished", series_id="void2")
        self.assertEqual(esports.grade_map_kills_handicap(series, 3, "Team A", -4.5), "void")

    def test_map_not_yet_played_series_still_live_returns_none(self):
        series = _hawk_series([_map(1, True, 0, 0, winner=None)], status="inprogress", series_id="pending")
        self.assertIsNone(esports.grade_map_kills_handicap(series, 1, "Team A", -4.5))

    def test_unrelated_team_returns_none(self):
        series = _hawk_series([_map(1, True, 32, 20)], series_id="unrelated")
        self.assertIsNone(esports.grade_map_kills_handicap(series, 1, "Team C", -4.5))

    def test_orientation_when_home_is_not_hawk_team1(self):
        # isTeam1Radiant=True means team1=radiant/team2=dire; home_is_team1
        # False means home is actually team2 (dire) here - home_kills=30
        # (dire), away_kills=15 (radiant). Confirms _hawk_map_kills flips
        # correctly rather than assuming home is always team1.
        series = _hawk_series([_map(1, True, 15, 30)], home_is_team1=False, series_id="orient")
        self.assertEqual(esports.grade_map_kills_handicap(series, 1, "Team A", -4.5), "won")


class GradeMapTotalKills(unittest.TestCase):
    def test_over_clears_wins(self):
        series = _hawk_series([_map(1, True, 32, 20)], series_id="over-win")
        self.assertEqual(esports.grade_map_total_kills(series, 1, "over", 50.5), "won")

    def test_over_short_loses(self):
        series = _hawk_series([_map(1, True, 20, 20)], series_id="over-lose")
        self.assertEqual(esports.grade_map_total_kills(series, 1, "over", 50.5), "lost")

    def test_under_wins_when_total_stays_below_line(self):
        series = _hawk_series([_map(1, True, 10, 10)], series_id="under-win")
        self.assertEqual(esports.grade_map_total_kills(series, 1, "under", 50.5), "won")

    def test_exact_push(self):
        series = _hawk_series([_map(1, True, 25, 25)], series_id="push")
        self.assertEqual(esports.grade_map_total_kills(series, 1, "over", 50), "push")

    def test_map_never_played_but_series_decided_voids(self):
        series = _hawk_series([_map(1, True, 0, 0, winner=None)], status="finished", series_id="void1")
        self.assertEqual(esports.grade_map_total_kills(series, 1, "over", 50.5), "void")

    def test_map_number_beyond_any_recorded_map_but_series_decided_voids(self):
        series = _hawk_series([], status="finished", series_id="void2")
        self.assertEqual(esports.grade_map_total_kills(series, 3, "over", 50.5), "void")

    def test_map_not_yet_played_series_still_live_returns_none(self):
        series = _hawk_series([_map(1, True, 0, 0, winner=None)], status="inprogress", series_id="pending")
        self.assertIsNone(esports.grade_map_total_kills(series, 1, "over", 50.5))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Regression tests for tabletennis.py's grading/parsing logic - against
constructed match dicts shaped like scores24.live's own API responses, not
live scraping (that side was confirmed live during development instead -
see the module's own docstring).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabletennis


def _listing_match(match_id, home, away, is_live=True):
    return {
        "id": match_id, "slug": f"slug-{match_id}",
        "teams": [{"name": home}, {"name": away}],
        "result_scores": [{"type": "FT", "value": "0:0"}],
        "is_live": is_live, "is_finished": False, "match_date": "2026-09-06T00:00:00.000000Z",
    }


def _match(
    result_scores, winner=None, is_live=False, is_finished=False,
    home="Player A", away="Player B",
):
    return {
        "teams": [{"name": home}, {"name": away}],
        "result_scores": result_scores,
        "winner": winner,
        "is_live": is_live,
        "is_finished": is_finished,
    }


class GamesWonAndTotalPoints(unittest.TestCase):
    def test_games_won_reads_the_ft_entry(self):
        m = _match([
            {"type": "1", "value": "13:15"}, {"type": "2", "value": "17:15"},
            {"type": "3", "value": "11:7"}, {"type": "4", "value": "2:9"},
            {"type": "FT", "value": "2:1"},
        ])
        self.assertEqual(tabletennis.games_won(m), (2, 1))

    def test_games_won_none_without_an_ft_entry(self):
        m = _match([{"type": "1", "value": "10:8"}])
        self.assertIsNone(tabletennis.games_won(m))

    def test_total_points_sums_every_game_including_the_in_progress_one(self):
        # Confirmed live: the LAST non-FT entry is the currently-in-
        # progress game's own live point score, not yet folded into FT -
        # total_points still needs to include it for Point Handicap
        # grading, unlike games_won.
        m = _match([
            {"type": "1", "value": "13:15"}, {"type": "2", "value": "17:15"},
            {"type": "3", "value": "11:7"}, {"type": "4", "value": "2:9"},
            {"type": "FT", "value": "2:1"},
        ])
        self.assertEqual(tabletennis.total_points(m), (13 + 17 + 11 + 2, 15 + 15 + 7 + 9))

    def test_current_game_score_is_the_last_non_ft_entry(self):
        m = _match([{"type": "1", "value": "13:15"}, {"type": "2", "value": "4:6"}, {"type": "FT", "value": "1:0"}])
        self.assertEqual(tabletennis.current_game_score(m), (4, 6))

    def test_no_games_at_all_returns_none(self):
        m = _match([])
        self.assertIsNone(tabletennis.total_points(m))
        self.assertIsNone(tabletennis.current_game_score(m))


class IsCancelled(unittest.TestCase):
    def test_finished_with_no_winner_and_blank_score_is_cancelled(self):
        # Confirmed live: status.code "70" carries exactly this shape for
        # a scheduled match that was never actually played.
        m = _match([{"type": "FT", "value": "0:0"}], winner=0, is_finished=True)
        self.assertTrue(tabletennis.is_cancelled(m))

    def test_finished_with_a_real_winner_is_not_cancelled(self):
        m = _match([{"type": "FT", "value": "3:1"}], winner=1, is_finished=True)
        self.assertFalse(tabletennis.is_cancelled(m))

    def test_not_finished_yet_is_not_cancelled(self):
        m = _match([{"type": "1", "value": "5:3"}], is_finished=False)
        self.assertFalse(tabletennis.is_cancelled(m))


class GradeMoneyline(unittest.TestCase):
    def test_still_live_returns_none(self):
        m = _match([{"type": "1", "value": "11:5"}], is_live=True)
        self.assertIsNone(tabletennis.grade_moneyline(m, "Player A"))

    def test_home_winner_grades_won_for_home_pick(self):
        m = _match([{"type": "FT", "value": "3:1"}], winner=1, is_finished=True)
        self.assertEqual(tabletennis.grade_moneyline(m, "Player A"), "won")

    def test_home_winner_grades_lost_for_away_pick(self):
        m = _match([{"type": "FT", "value": "3:1"}], winner=1, is_finished=True)
        self.assertEqual(tabletennis.grade_moneyline(m, "Player B"), "lost")

    def test_away_winner_grades_won_for_away_pick(self):
        m = _match([{"type": "FT", "value": "1:3"}], winner=2, is_finished=True)
        self.assertEqual(tabletennis.grade_moneyline(m, "Player B"), "won")

    def test_cancelled_match_voids(self):
        m = _match([{"type": "FT", "value": "0:0"}], winner=0, is_finished=True)
        self.assertEqual(tabletennis.grade_moneyline(m, "Player A"), "void")

    def test_unrelated_player_returns_none(self):
        m = _match([{"type": "FT", "value": "3:1"}], winner=1, is_finished=True)
        self.assertIsNone(tabletennis.grade_moneyline(m, "Someone Else"))


class GradePointHandicap(unittest.TestCase):
    def test_still_live_returns_none(self):
        m = _match([{"type": "1", "value": "11:5"}], is_live=True)
        self.assertIsNone(tabletennis.grade_point_handicap(m, "Player A", -3.5))

    def test_picked_home_covers_the_line_wins(self):
        m = _match([
            {"type": "1", "value": "11:5"}, {"type": "2", "value": "11:6"},
            {"type": "3", "value": "11:4"}, {"type": "FT", "value": "3:0"},
        ], winner=1, is_finished=True)
        # Home totals 33, away totals 15 - a -10.5 line still covers (33-10.5=22.5 > 15).
        self.assertEqual(tabletennis.grade_point_handicap(m, "Player A", -10.5), "won")

    def test_picked_home_fails_to_cover_loses(self):
        m = _match([
            {"type": "1", "value": "11:9"}, {"type": "2", "value": "9:11"},
            {"type": "3", "value": "11:8"}, {"type": "FT", "value": "2:1"},
        ], winner=1, is_finished=True)
        # Home totals 31, away totals 28 - a -5.5 line does not cover (31-5.5=25.5 < 28).
        self.assertEqual(tabletennis.grade_point_handicap(m, "Player A", -5.5), "lost")

    def test_exact_push(self):
        m = _match([{"type": "1", "value": "20:10"}, {"type": "FT", "value": "1:0"}], winner=1, is_finished=True)
        self.assertEqual(tabletennis.grade_point_handicap(m, "Player A", -10), "push")

    def test_cancelled_match_voids(self):
        m = _match([{"type": "FT", "value": "0:0"}], winner=0, is_finished=True)
        self.assertEqual(tabletennis.grade_point_handicap(m, "Player A", -3.5), "void")

    def test_unrelated_player_returns_none(self):
        m = _match([{"type": "1", "value": "11:5"}, {"type": "FT", "value": "1:0"}], winner=1, is_finished=True)
        self.assertIsNone(tabletennis.grade_point_handicap(m, "Someone Else", -3.5))


class StatusLine(unittest.TestCase):
    def test_finished_shows_final(self):
        m = _match([{"type": "FT", "value": "3:0"}], is_finished=True)
        self.assertEqual(tabletennis.status_line(m), "Final")

    def test_live_shows_current_game_number(self):
        m = _match([{"type": "1", "value": "11:5"}, {"type": "2", "value": "4:6"}], is_live=True)
        self.assertEqual(tabletennis.status_line(m), "Game 2")

    def test_not_started_is_blank(self):
        m = _match([])
        self.assertEqual(tabletennis.status_line(m), "")


class FindMatchForTeamDisambiguatesByOpponent(unittest.TestCase):
    """Confirmed live: the same player ("Marek Bereiter") had two
    genuinely different matches in flight at once against different
    opponents - a name-only search has no way to tell them apart and just
    returns whichever one ranks best (is_live first) for BOTH picks,
    silently resolving the second pick to the wrong match entirely."""

    def setUp(self):
        self._orig = tabletennis._fetch_matches_for_day
        self._matches = [
            _listing_match("m1", "Lukas Martinak", "Marek Bereiter", is_live=True),
            _listing_match("m2", "Marek Bereiter", "Adrian Walek", is_live=True),
        ]
        tabletennis._fetch_matches_for_day = lambda day: list(self._matches)

    def tearDown(self):
        tabletennis._fetch_matches_for_day = self._orig

    def test_without_opponent_returns_some_match_but_cant_tell_them_apart(self):
        result = tabletennis.find_match_for_team("Marek Bereiter")
        self.assertIn(result["id"], ("m1", "m2"))

    def test_with_opponent_resolves_to_the_correct_match(self):
        result = tabletennis.find_match_for_team("Marek Bereiter", opponent="Lukas Martinak")
        self.assertEqual(result["id"], "m1")

    def test_with_the_other_opponent_resolves_to_the_other_match(self):
        result = tabletennis.find_match_for_team("Marek Bereiter", opponent="Adrian Walek")
        self.assertEqual(result["id"], "m2")

    def test_opponent_that_matches_neither_match_returns_none(self):
        result = tabletennis.find_match_for_team("Marek Bereiter", opponent="Someone Else")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

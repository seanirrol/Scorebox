#!/usr/bin/env python3
"""
Regression tests for snooker.py's grading/parsing logic - against
constructed match dicts shaped like scores24.live's own API responses
(same site/API family as table tennis, see tests/test_tabletennis.py's
own docstring for why this isn't live-scraping in a unit test).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snooker


def _listing_match(match_id, home, away, is_live=True):
    return {
        "id": match_id, "slug": f"slug-{match_id}",
        "teams": [{"name": home}, {"name": away}],
        "result_score": "0:0",
        "result_scores": [[]],
        "is_live": is_live, "is_finished": False, "match_date": "2026-09-08T00:00:00.000000Z",
    }


def _match(
    result_score, result_scores=None, winner=None, is_live=False, is_finished=False,
    home="Player A", away="Player B",
):
    return {
        "teams": [{"name": home}, {"name": away}],
        "result_score": result_score,
        "result_scores": result_scores if result_scores is not None else [[]],
        "winner": winner,
        "is_live": is_live,
        "is_finished": is_finished,
    }


class FramesWonAndCurrentFrame(unittest.TestCase):
    def test_frames_won_reads_the_top_level_result_score(self):
        m = _match("4:3", winner=1, is_finished=True)
        self.assertEqual(snooker.frames_won(m), (4, 3))

    def test_frames_won_none_without_a_valid_result_score(self):
        m = _match("")
        self.assertIsNone(snooker.frames_won(m))

    def test_current_frame_score_is_the_last_non_ft_entry(self):
        m = _match("1:0", result_scores=[[], {"type": "1", "value": "64:53"}, {"type": "2", "value": "0:2"}], is_live=True)
        self.assertEqual(snooker.current_frame_score(m), (0, 2))

    def test_no_frames_at_all_returns_none(self):
        m = _match("0:0", result_scores=[[]])
        self.assertIsNone(snooker.current_frame_score(m))

    def test_frame_scores_skip_the_leading_empty_placeholder(self):
        m = _match("1:0", result_scores=[[], {"type": "1", "value": "64:53"}])
        self.assertEqual(snooker.current_frame_score(m), (64, 53))


class IsCancelled(unittest.TestCase):
    def test_finished_with_no_winner_and_blank_score_is_cancelled(self):
        m = _match("0:0", winner=0, is_finished=True)
        self.assertTrue(snooker.is_cancelled(m))

    def test_finished_with_a_real_winner_is_not_cancelled(self):
        m = _match("4:1", winner=1, is_finished=True)
        self.assertFalse(snooker.is_cancelled(m))

    def test_not_finished_yet_is_not_cancelled(self):
        m = _match("1:0", is_finished=False)
        self.assertFalse(snooker.is_cancelled(m))


class GradeMoneyline(unittest.TestCase):
    def test_still_live_returns_none(self):
        m = _match("1:0", is_live=True)
        self.assertIsNone(snooker.grade_moneyline(m, "Player A"))

    def test_home_winner_grades_won_for_home_pick(self):
        m = _match("4:1", winner=1, is_finished=True)
        self.assertEqual(snooker.grade_moneyline(m, "Player A"), "won")

    def test_home_winner_grades_lost_for_away_pick(self):
        m = _match("4:1", winner=1, is_finished=True)
        self.assertEqual(snooker.grade_moneyline(m, "Player B"), "lost")

    def test_away_winner_grades_won_for_away_pick(self):
        m = _match("1:4", winner=2, is_finished=True)
        self.assertEqual(snooker.grade_moneyline(m, "Player B"), "won")

    def test_cancelled_match_voids(self):
        m = _match("0:0", winner=0, is_finished=True)
        self.assertEqual(snooker.grade_moneyline(m, "Player A"), "void")

    def test_walkover_with_no_scores_still_grades_via_winner_field(self):
        # Confirmed live: a walkover carries a real winner but an empty
        # result_score/result_scores - moneyline grading must not require
        # frames_won() to succeed.
        m = _match("", result_scores=[{"type": "FT"}], winner=2, is_finished=True)
        self.assertEqual(snooker.grade_moneyline(m, "Player B"), "won")

    def test_unrelated_player_returns_none(self):
        m = _match("4:1", winner=1, is_finished=True)
        self.assertIsNone(snooker.grade_moneyline(m, "Someone Else"))


class GradeFrameHandicap(unittest.TestCase):
    def test_still_live_returns_none(self):
        m = _match("1:0", is_live=True)
        self.assertIsNone(snooker.grade_frame_handicap(m, "Player A", -1.5))

    def test_picked_home_covers_the_line_wins(self):
        m = _match("4:3", winner=1, is_finished=True)
        self.assertEqual(snooker.grade_frame_handicap(m, "Player A", -0.5), "won")

    def test_picked_home_fails_to_cover_loses(self):
        m = _match("3:4", winner=2, is_finished=True)
        self.assertEqual(snooker.grade_frame_handicap(m, "Player A", 0.5), "lost")

    def test_exact_push(self):
        m = _match("4:2", winner=1, is_finished=True)
        self.assertEqual(snooker.grade_frame_handicap(m, "Player A", -2), "push")

    def test_cancelled_match_voids(self):
        m = _match("0:0", winner=0, is_finished=True)
        self.assertEqual(snooker.grade_frame_handicap(m, "Player A", -1.5), "void")

    def test_walkover_with_no_scores_returns_none(self):
        # Unlike moneyline, frame handicap genuinely needs frames_won() -
        # a walkover with no scores can't settle this market at all.
        m = _match("", result_scores=[{"type": "FT"}], winner=2, is_finished=True)
        self.assertIsNone(snooker.grade_frame_handicap(m, "Player B", -1.5))

    def test_unrelated_player_returns_none(self):
        m = _match("4:1", winner=1, is_finished=True)
        self.assertIsNone(snooker.grade_frame_handicap(m, "Someone Else", -1.5))


class StatusLine(unittest.TestCase):
    def test_finished_shows_final(self):
        m = _match("4:1", is_finished=True)
        self.assertEqual(snooker.status_line(m), "Final")

    def test_live_shows_current_frame_number(self):
        m = _match("1:0", result_scores=[[], {"type": "1", "value": "64:53"}, {"type": "2", "value": "0:2"}], is_live=True)
        self.assertEqual(snooker.status_line(m), "Frame 2")

    def test_not_started_is_blank(self):
        m = _match("0:0", result_scores=[[]])
        self.assertEqual(snooker.status_line(m), "")


class FindMatchForTeamDisambiguatesByOpponent(unittest.TestCase):
    """Same disambiguation confirmed live for table tennis - see
    tests/test_tabletennis.py's own class of this name."""

    def setUp(self):
        self._orig = snooker._fetch_matches_for_day
        self._matches = [
            _listing_match("m1", "Thepchaiya Un-Nooh", "Wang Xinbo", is_live=True),
            _listing_match("m2", "Thepchaiya Un-Nooh", "Xiao Guodong", is_live=True),
        ]
        snooker._fetch_matches_for_day = lambda day: list(self._matches)

    def tearDown(self):
        snooker._fetch_matches_for_day = self._orig

    def test_without_opponent_returns_some_match_but_cant_tell_them_apart(self):
        result = snooker.find_match_for_team("Thepchaiya Un-Nooh")
        self.assertIn(result["id"], ("m1", "m2"))

    def test_with_opponent_resolves_to_the_correct_match(self):
        result = snooker.find_match_for_team("Thepchaiya Un-Nooh", opponent="Wang Xinbo")
        self.assertEqual(result["id"], "m1")

    def test_with_the_other_opponent_resolves_to_the_other_match(self):
        result = snooker.find_match_for_team("Thepchaiya Un-Nooh", opponent="Xiao Guodong")
        self.assertEqual(result["id"], "m2")

    def test_opponent_that_matches_neither_match_returns_none(self):
        result = snooker.find_match_for_team("Thepchaiya Un-Nooh", opponent="Someone Else")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Regression tests for scores365.py's name-matching and grading logic. A
name-match failure means the wrong game gets tracked (or none at all) -
which surfaces in /summary as a missing or wrong-game entry, so this is
just as load-bearing for summary/win-rate correctness as the parsing
tests, even though the bug shows up one step removed.

Run with: python -m unittest discover -s tests -t .
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scores365


class NamesMatchAccentFolding(unittest.TestCase):
    """_normalize/_meaningful_words used to filter out anything not in
    [a-z0-9] - an accented letter doesn't match that class at all, so it
    was being deleted rather than folded to its base letter, silently
    mangling the comparison instead."""

    def test_accented_team_name_matches_plain_ascii_spelling(self):
        # Confirmed live: 365scores stores "Club América" with the accent;
        # a pick parsed as plain "Club America" never matched at all.
        self.assertTrue(scores365.names_match("Club America", "Club América"))

    def test_accent_folding_is_general_not_a_one_off(self):
        self.assertTrue(scores365.names_match("Etoile du Sahel", "Étoile du Sahel"))

    def test_unrelated_teams_still_dont_match(self):
        self.assertFalse(scores365.names_match("New York Yankees", "Boston Red Sox"))

    def test_kbo_mlb_disambiguation_unaffected_by_the_accent_fix(self):
        # "LG Twins" collapsing to just {"twins"} once used to be a false
        # positive against the MLB Minnesota Twins - the 2-letter-prefix
        # fix for that must still hold after accent-folding was added.
        self.assertFalse(scores365.names_match("LG Twins", "Minnesota Twins"))

    def test_hyphenated_name_variant_still_matches(self):
        self.assertTrue(scores365.names_match("Xin-Yu Wang", "Xinyu"))


class NamesMatchFuzzyTransliteration(unittest.TestCase):
    """A first name transliterated differently by two sources (e.g. a
    Russian name romanized two valid ways) is allowed to differ by a
    small edit distance, but only when every OTHER word already matches
    exactly - that anchor is what keeps this from matching two different
    people who happen to share a surname."""

    def test_liudmila_vs_ludmilla_matches(self):
        # Confirmed live: a pick for "Liudmila Samsonova" went untracked
        # because 365scores itself spells it "Ludmilla Samsonova".
        self.assertTrue(scores365.names_match("Liudmila Samsonova", "Ludmilla Samsonova"))

    def test_different_first_names_same_surname_still_rejected(self):
        self.assertFalse(scores365.names_match("Emma Smith", "Olivia Smith"))

    def test_short_words_are_not_fuzzy_matched(self):
        # Below the length floor - must be an exact match, not edit-distance.
        self.assertFalse(scores365.names_match("Al Jones", "Ed Jones"))

    def test_surname_must_still_match_exactly(self):
        self.assertFalse(scores365.names_match("Liudmila Samsonova", "Ludmilla Petrova"))


class GradeSpread(unittest.TestCase):
    """grade_spread is grade_f5_handicap's math applied to the whole
    game's final score instead of just the first 5 innings - the line is
    added to the picked team's own score before comparing to the other
    side's."""

    def _game(self, home, away, home_score, away_score):
        return {
            "homeCompetitor": {"name": home, "score": home_score},
            "awayCompetitor": {"name": away, "score": away_score},
        }

    def test_favorite_covers_the_spread(self):
        # Broncos (away) win by 6 - covers -3.5.
        game = self._game("New York Jets", "Denver Broncos", 18.0, 24.0)
        self.assertEqual(scores365.grade_spread(game, "Denver Broncos", -3.5), "won")

    def test_favorite_wins_but_doesnt_cover(self):
        # Broncos win by only 4 - doesn't cover -5.0.
        game = self._game("New York Jets", "Denver Broncos", 20.0, 24.0)
        self.assertEqual(scores365.grade_spread(game, "Denver Broncos", -5.0), "lost")

    def test_underdog_covers_with_the_points(self):
        # Jets (home) lose by only 3.5 - covers +5.0.
        game = self._game("New York Jets", "Denver Broncos", 20.0, 24.0)
        self.assertEqual(scores365.grade_spread(game, "New York Jets", 5.0), "won")

    def test_whole_number_line_can_push(self):
        game = self._game("New York Jets", "Denver Broncos", 20.0, 24.0)
        self.assertEqual(scores365.grade_spread(game, "Denver Broncos", -4.0), "push")

    def test_team_not_in_the_game_returns_none(self):
        game = self._game("New York Jets", "Denver Broncos", 20.0, 24.0)
        self.assertIsNone(scores365.grade_spread(game, "Los Angeles Rams", -3.5))


class TennisSetsWonRetirement(unittest.TestCase):
    """tennis_sets_won derives each side's real sets-won count from the
    completed Set N stages themselves, not 365scores' own aggregate
    homeCompetitor/awayCompetitor "score" field (what main_scores reads) -
    confirmed live that field can stay stuck at 0-0 when a match ends by
    mid-set retirement, even though an earlier set had already finished
    with a clear winner (a real WTA match: Set 1 ended 5-3, the match then
    ended by retirement in Set 2, and main_scores still showed 0-0)."""

    def _game(self, stages):
        return {
            "homeCompetitor": {"name": "Elisabetta Cocciaretto", "score": 0.0},
            "awayCompetitor": {"name": "Lucrezia Stefanini", "score": 0.0},
            "stages": stages,
        }

    def test_retirement_after_one_completed_set(self):
        game = self._game([
            {"name": "Set 1", "homeCompetitorScore": 3.0, "awayCompetitorScore": 5.0, "isEnded": True},
            {"name": "Set 2", "homeCompetitorScore": -1.0, "awayCompetitorScore": -1.0},
        ])
        self.assertEqual(scores365.tennis_sets_won(game), (0, 1))

    def test_unplayed_sets_dont_count(self):
        game = self._game([
            {"name": "Set 1", "homeCompetitorScore": 6.0, "awayCompetitorScore": 4.0, "isEnded": True},
            {"name": "Set 2", "homeCompetitorScore": -1.0, "awayCompetitorScore": -1.0},
            {"name": "Set 3", "homeCompetitorScore": -1.0, "awayCompetitorScore": -1.0},
        ])
        self.assertEqual(scores365.tennis_sets_won(game), (1, 0))

    def test_still_live_set_not_yet_ended_is_not_counted(self):
        game = self._game([
            {"name": "Set 1", "homeCompetitorScore": 6.0, "awayCompetitorScore": 2.0, "isEnded": True},
            {"name": "Set 2", "homeCompetitorScore": 3.0, "awayCompetitorScore": 2.0},
        ])
        self.assertEqual(scores365.tennis_sets_won(game), (1, 0))


class GradeMoneyline(unittest.TestCase):
    """isWinner is checked before falling back to score comparison - a
    walkover/retirement sits at 0-0 with no sets played, which used to
    grade as a push even though 365scores' own isWinner flag already
    correctly identifies who actually won."""

    def _game(self, home, away, home_score=0.0, away_score=0.0, home_winner=None, away_winner=None):
        return {
            "homeCompetitor": {"name": home, "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": away, "score": away_score, "isWinner": away_winner},
        }

    def test_walkover_uses_iswinner_not_the_0_0_score(self):
        game = self._game("Belinda Bencic", "Coco Gauff", home_winner=False, away_winner=True)
        self.assertEqual(scores365.grade_moneyline(game, "Coco Gauff"), "won")
        self.assertEqual(scores365.grade_moneyline(game, "Belinda Bencic"), "lost")

    def test_genuine_tie_with_neither_side_flagged_falls_through_to_score(self):
        game = self._game("Team A", "Team B", home_score=2.0, away_score=2.0)
        self.assertEqual(scores365.grade_moneyline(game, "Team A"), "push")

    def test_normal_final_score_grades_by_score(self):
        game = self._game("New York Yankees", "Boston Red Sox", home_score=5.0, away_score=2.0)
        self.assertEqual(scores365.grade_moneyline(game, "New York Yankees"), "won")
        self.assertEqual(scores365.grade_moneyline(game, "Boston Red Sox"), "lost")

    def test_picked_team_not_in_the_game_returns_none(self):
        # Deliberately no shared words with either side - "Team A"/"Team B"
        # vs. e.g. "Totally Different Team" would falsely fuzzy-match on
        # the shared word "Team" (names_match is word-overlap based), the
        # exact class of test-data mistake to avoid here.
        game = self._game("New York Yankees", "Boston Red Sox", home_score=5.0, away_score=2.0)
        self.assertIsNone(scores365.grade_moneyline(game, "Los Angeles Dodgers"))


class IsWalkover(unittest.TestCase):
    def _game(self, home_score=0.0, away_score=0.0, home_winner=None, away_winner=None, status_group=4):
        return {
            "statusGroup": status_group,
            "homeCompetitor": {"name": "Player A", "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": "Player B", "score": away_score, "isWinner": away_winner},
        }

    def test_finished_0_0_with_exactly_one_side_flagged_is_a_walkover(self):
        self.assertTrue(scores365.is_walkover(self._game(home_winner=False, away_winner=True)))

    def test_not_finished_is_never_a_walkover(self):
        self.assertFalse(scores365.is_walkover(self._game(home_winner=False, away_winner=True, status_group=3)))

    def test_finished_with_a_real_score_is_not_a_walkover(self):
        self.assertFalse(scores365.is_walkover(self._game(home_score=2.0, away_score=1.0, home_winner=True, away_winner=False)))

    def test_finished_0_0_with_neither_side_flagged_is_not_a_walkover(self):
        # Can't actually happen for tennis in practice (a real completed
        # match always has exactly one winner) - guards the flag-mismatch
        # logic itself rather than a real-world case.
        self.assertFalse(scores365.is_walkover(self._game()))


class GradeWinASet(unittest.TestCase):
    def _game(self, home_score=0.0, away_score=0.0, home_winner=None, away_winner=None, status_group=4):
        return {
            "statusGroup": status_group,
            "homeCompetitor": {"name": "Xiyu Wang", "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": "Elina Svitolina", "score": away_score, "isWinner": away_winner},
        }

    def test_walkover_voids_instead_of_grading_off_the_0_0_score(self):
        # Confirmed live: this exact real-world case (Xiyu Wang won via
        # walkover) used to grade "Xiyu Wang to Win a Set" LOST, since
        # main_scores sits at 0-0 for a walkover, indistinguishable from a
        # genuine "lost every set" result before this fix.
        game = self._game(home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_win_a_set(game, "Xiyu Wang", "yes"), "void")
        self.assertEqual(scores365.grade_win_a_set(game, "Elina Svitolina", "yes"), "void")

    def test_normal_finished_match_still_grades_by_sets_won(self):
        game = self._game(home_score=2.0, away_score=0.0, home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_win_a_set(game, "Xiyu Wang", "yes"), "won")
        self.assertEqual(scores365.grade_win_a_set(game, "Elina Svitolina", "yes"), "lost")


class GradeGamesHandicap(unittest.TestCase):
    def test_walkover_voids_instead_of_grading_off_zero_games(self):
        game = {
            "statusGroup": 4,
            "homeCompetitor": {"name": "Xiyu Wang", "score": 0.0, "isWinner": True},
            "awayCompetitor": {"name": "Elina Svitolina", "score": 0.0, "isWinner": False},
        }
        self.assertEqual(scores365.grade_games_handicap(game, "Xiyu Wang", -2.5), "void")


class GradeSetsHandicap(unittest.TestCase):
    def test_walkover_voids_instead_of_grading_off_zero_sets(self):
        game = {
            "statusGroup": 4,
            "homeCompetitor": {"name": "Xiyu Wang", "score": 0.0, "isWinner": True},
            "awayCompetitor": {"name": "Elina Svitolina", "score": 0.0, "isWinner": False},
        }
        self.assertEqual(scores365.grade_sets_handicap(game, "Xiyu Wang", -1.5), "void")


class VolleyballSet1Handicap(unittest.TestCase):
    """grade_volleyball_set1_handicap backs settracker.py's volleyball-only
    set1_point_handicap market - monkeypatches volleyball_set_scores (a live
    365scores per-game detail fetch) so this exercises the real
    adjust-then-compare grading logic without a network request."""

    def setUp(self):
        self._orig = scores365.volleyball_set_scores
        self._sets = None
        scores365.volleyball_set_scores = lambda sport_id, status, game_id: self._sets

    def tearDown(self):
        scores365.volleyball_set_scores = self._orig

    def _game(self, status_group=4):
        return {
            "id": 1, "statusGroup": status_group,
            "homeCompetitor": {"name": "Serbia", "score": 3.0},
            "awayCompetitor": {"name": "Greece", "score": 1.0},
        }

    def test_set1_still_live_returns_none(self):
        self._sets = [{"set_number": 1, "home": 15, "away": 10, "is_live": True}]
        self.assertIsNone(scores365.volleyball_first_set_result(self._game(status_group=3)))

    def test_set1_ended_returns_final_points(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]
        self.assertEqual(scores365.volleyball_first_set_result(self._game()), (25, 20))

    def test_favorite_covers_the_line(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 15, "is_live": False}]
        self.assertEqual(scores365.grade_volleyball_set1_handicap(self._game(), "Serbia", -4.5), "won")

    def test_favorite_fails_to_cover_the_line(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 23, "is_live": False}]
        self.assertEqual(scores365.grade_volleyball_set1_handicap(self._game(), "Serbia", -4.5), "lost")

    def test_exact_push_on_a_whole_number_line(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]
        self.assertEqual(scores365.grade_volleyball_set1_handicap(self._game(), "Serbia", -5), "push")

    def test_underdog_side_grades_off_the_same_set(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]
        self.assertEqual(scores365.grade_volleyball_set1_handicap(self._game(), "Greece", 5.5), "won")
        self.assertEqual(scores365.grade_volleyball_set1_handicap(self._game(), "Greece", 4.5), "lost")

    def test_set1_not_ended_yet_returns_none_not_a_grade(self):
        self._sets = None
        self.assertIsNone(scores365.grade_volleyball_set1_handicap(self._game(status_group=3), "Serbia", -4.5))


class VolleyballMatchPoints(unittest.TestCase):
    """volleyball_match_points/grade_volleyball_match_point_handicap back
    settracker.py's match_point_total/match_point_handicap markets - the
    combined rally-point total across the WHOLE match (distinct from
    volleyball_first_set_result's Set-1-only breakdown, and from
    main_scores' sets-won tally). Monkeypatches volleyball_set_scores (a
    live 365scores per-game detail fetch) so this exercises the real
    summing/grading logic without a network request."""

    def setUp(self):
        self._orig = scores365.volleyball_set_scores
        self._sets = None
        scores365.volleyball_set_scores = lambda sport_id, status, game_id: self._sets

    def tearDown(self):
        scores365.volleyball_set_scores = self._orig

    def _game(self, home_score=3.0, away_score=1.0, home_winner=True, away_winner=False, status_group=4):
        return {
            "id": 1, "statusGroup": status_group,
            "homeCompetitor": {"name": "Poland", "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": "Germany", "score": away_score, "isWinner": away_winner},
        }

    def test_sums_points_across_every_set_not_sets_won(self):
        self._sets = [
            {"set_number": 1, "home": 25, "away": 20, "is_live": False},
            {"set_number": 2, "home": 20, "away": 25, "is_live": False},
            {"set_number": 3, "home": 25, "away": 18, "is_live": False},
            {"set_number": 4, "home": 25, "away": 22, "is_live": False},
        ]
        self.assertEqual(scores365.volleyball_match_points(self._game()), (95, 85))

    def test_no_sets_yet_returns_zero_zero_not_none(self):
        self._sets = None
        self.assertEqual(scores365.volleyball_match_points(self._game(status_group=1)), (0, 0))

    def test_live_in_progress_set_contributes_its_partial_score(self):
        self._sets = [
            {"set_number": 1, "home": 25, "away": 20, "is_live": False},
            {"set_number": 2, "home": 10, "away": 8, "is_live": True},
        ]
        self.assertEqual(scores365.volleyball_match_points(self._game(status_group=3)), (35, 28))

    def test_handicap_favorite_covers_the_line(self):
        self._sets = [
            {"set_number": 1, "home": 25, "away": 20, "is_live": False},
            {"set_number": 2, "home": 20, "away": 25, "is_live": False},
            {"set_number": 3, "home": 25, "away": 18, "is_live": False},
            {"set_number": 4, "home": 25, "away": 22, "is_live": False},
        ]
        self.assertEqual(scores365.grade_volleyball_match_point_handicap(self._game(), "Poland", -4.5), "won")

    def test_handicap_favorite_fails_to_cover_the_line(self):
        self._sets = [
            {"set_number": 1, "home": 25, "away": 20, "is_live": False},
            {"set_number": 2, "home": 20, "away": 25, "is_live": False},
            {"set_number": 3, "home": 25, "away": 18, "is_live": False},
            {"set_number": 4, "home": 25, "away": 22, "is_live": False},
        ]
        self.assertEqual(scores365.grade_volleyball_match_point_handicap(self._game(), "Poland", -15.5), "lost")

    def test_handicap_walkover_voids_instead_of_grading_off_zero_points(self):
        self._sets = None
        game = self._game(home_score=0.0, away_score=0.0, home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_volleyball_match_point_handicap(game, "Poland", -4.5), "void")


class GradeTennisSet(unittest.TestCase):
    def test_walkover_voids_instead_of_grading_off_zero_games(self):
        game = {
            "statusGroup": 4,
            "homeCompetitor": {"name": "Xiyu Wang", "score": 0.0, "isWinner": True},
            "awayCompetitor": {"name": "Elina Svitolina", "score": 0.0, "isWinner": False},
        }
        self.assertEqual(scores365.grade_tennis_set(game, 0, 0, "Xiyu Wang"), "void")


class GradeDoubleChance(unittest.TestCase):
    """grade_double_chance backs doublechancetracker.py - a soccer market
    covering two of the three possible full-time outcomes (home win/draw/
    away win) in one pick. No network mocking needed - grades purely off
    the game dict's own is_finished/main_scores."""

    def _game(self, home_score=0.0, away_score=0.0, status_group=4):
        return {
            "id": 1, "statusGroup": status_group,
            "homeCompetitor": {"name": "Paris FC", "score": home_score},
            "awayCompetitor": {"name": "Nice", "score": away_score},
        }

    def test_not_finished_returns_none(self):
        game = self._game(status_group=3)
        self.assertIsNone(scores365.grade_double_chance(game, ("Paris FC", "DRAW")))

    def test_1x_wins_on_home_win(self):
        game = self._game(home_score=2.0, away_score=1.0)
        self.assertEqual(scores365.grade_double_chance(game, ("Paris FC", "DRAW")), "won")

    def test_1x_wins_on_draw(self):
        game = self._game(home_score=1.0, away_score=1.0)
        self.assertEqual(scores365.grade_double_chance(game, ("Paris FC", "DRAW")), "won")

    def test_1x_loses_on_away_win(self):
        game = self._game(home_score=0.0, away_score=2.0)
        self.assertEqual(scores365.grade_double_chance(game, ("Paris FC", "DRAW")), "lost")

    def test_x2_wins_on_draw(self):
        game = self._game(home_score=1.0, away_score=1.0)
        self.assertEqual(scores365.grade_double_chance(game, ("DRAW", "Nice")), "won")

    def test_x2_wins_on_away_win(self):
        game = self._game(home_score=0.0, away_score=2.0)
        self.assertEqual(scores365.grade_double_chance(game, ("DRAW", "Nice")), "won")

    def test_x2_loses_on_home_win(self):
        game = self._game(home_score=2.0, away_score=0.0)
        self.assertEqual(scores365.grade_double_chance(game, ("DRAW", "Nice")), "lost")

    def test_12_wins_on_either_team_winning(self):
        home_win = self._game(home_score=2.0, away_score=0.0)
        away_win = self._game(home_score=0.0, away_score=2.0)
        self.assertEqual(scores365.grade_double_chance(home_win, ("Paris FC", "Nice")), "won")
        self.assertEqual(scores365.grade_double_chance(away_win, ("Paris FC", "Nice")), "won")

    def test_12_loses_on_a_draw(self):
        game = self._game(home_score=1.0, away_score=1.0)
        self.assertEqual(scores365.grade_double_chance(game, ("Paris FC", "Nice")), "lost")

    def test_never_a_push_unlike_a_plain_moneyline(self):
        # The whole point of covering two outcomes is that it's always
        # exactly won or lost, never voided as a tie the way a plain
        # moneyline pick would be.
        for home, away in ((1.0, 1.0), (2.0, 0.0), (0.0, 2.0)):
            result = scores365.grade_double_chance(self._game(home, away), ("Paris FC", "DRAW"))
            self.assertIn(result, ("won", "lost"))


class GradeHtFt(unittest.TestCase):
    """grade_ht_ft backs htfttracker.py - monkeypatches quarters_breakdown
    (a live 365scores fetch) so this exercises the real compound-bet logic
    without a network request. Confirmed live against a real finished WNBA
    game (Washington Mystics led at half and won outright) before these
    were written."""

    def setUp(self):
        self._orig_quarters_breakdown = scores365.quarters_breakdown
        self._halftime = None
        scores365.quarters_breakdown = lambda game_id, through_quarter: self._halftime

    def tearDown(self):
        scores365.quarters_breakdown = self._orig_quarters_breakdown

    def _game(self, home_score=0.0, away_score=0.0, home_winner=None, away_winner=None, status_group=4):
        # Deliberately no shared words between the two names (unlike
        # "Team A"/"Team B") - names_match is word-overlap based, and a
        # shared word would fuzzy-match the two sides as equal, exactly
        # the test-data mistake to avoid (see GradeMoneyline's own note).
        return {
            "id": 1, "statusGroup": status_group,
            "homeCompetitor": {"name": "Washington Mystics", "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": "Los Angeles Sparks", "score": away_score, "isWinner": away_winner},
        }

    def test_halftime_not_yet_decided_returns_none(self):
        self._halftime = None
        self.assertIsNone(scores365.grade_ht_ft(self._game(status_group=3), "Washington Mystics", "Washington Mystics"))

    def test_wrong_team_leading_at_half_loses_immediately_even_before_game_ends(self):
        self._halftime = (10, 20)  # Los Angeles Sparks (away) leads at half
        self.assertEqual(scores365.grade_ht_ft(self._game(status_group=3), "Washington Mystics", "Washington Mystics"), "lost")

    def test_tied_at_half_loses_regardless_of_named_team(self):
        self._halftime = (10, 10)
        self.assertEqual(scores365.grade_ht_ft(self._game(status_group=3), "Washington Mystics", "Washington Mystics"), "lost")

    def test_correct_ht_leader_but_game_not_finished_returns_none(self):
        self._halftime = (20, 10)  # Washington Mystics leads at half
        self.assertIsNone(scores365.grade_ht_ft(self._game(status_group=3), "Washington Mystics", "Washington Mystics"))

    def test_both_legs_correct_wins(self):
        self._halftime = (20, 10)
        game = self._game(home_score=80.0, away_score=70.0, home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_ht_ft(game, "Washington Mystics", "Washington Mystics"), "won")

    def test_ht_correct_but_ft_wrong_loses(self):
        self._halftime = (20, 10)  # Washington Mystics leads at half
        game = self._game(home_score=70.0, away_score=80.0, home_winner=False, away_winner=True)
        self.assertEqual(scores365.grade_ht_ft(game, "Washington Mystics", "Washington Mystics"), "lost")

    def test_different_ht_and_ft_teams_both_correct_wins(self):
        self._halftime = (10, 20)  # Los Angeles Sparks leads at half
        game = self._game(home_score=80.0, away_score=70.0, home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_ht_ft(game, "Los Angeles Sparks", "Washington Mystics"), "won")


class GradeHtFtVolleyballDoubleResult(unittest.TestCase):
    """grade_ht_ft's sport_id branch for volleyball's own "Double Result
    (1st Set/Match)" market - same compound-bet shape as GradeHtFt above,
    just sourced from volleyball_first_set_result (Set 1's own final score)
    instead of quarters_breakdown. Monkeypatches volleyball_set_scores (a
    live 365scores per-game detail fetch) so this exercises the real
    grading logic without a network request."""

    def setUp(self):
        self._orig = scores365.volleyball_set_scores
        self._sets = None
        scores365.volleyball_set_scores = lambda sport_id, status, game_id: self._sets

    def tearDown(self):
        scores365.volleyball_set_scores = self._orig

    def _game(self, home_score=0.0, away_score=0.0, home_winner=None, away_winner=None, status_group=4):
        return {
            "id": 1, "statusGroup": status_group,
            "homeCompetitor": {"name": "Puerto Rico", "score": home_score, "isWinner": home_winner},
            "awayCompetitor": {"name": "Guatemala", "score": away_score, "isWinner": away_winner},
        }

    def test_set1_not_ended_yet_returns_none(self):
        self._sets = None
        self.assertIsNone(scores365.grade_ht_ft(self._game(status_group=3), "Puerto Rico", "Puerto Rico", scores365.SPORT_IDS["volleyball"]))

    def test_wrong_team_winning_set1_loses_immediately_even_before_match_ends(self):
        self._sets = [{"set_number": 1, "home": 20, "away": 25, "is_live": False}]  # Guatemala takes Set 1
        self.assertEqual(
            scores365.grade_ht_ft(self._game(status_group=3), "Puerto Rico", "Puerto Rico", scores365.SPORT_IDS["volleyball"]), "lost",
        )

    def test_both_legs_correct_wins(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]
        game = self._game(home_score=3.0, away_score=1.0, home_winner=True, away_winner=False)
        self.assertEqual(scores365.grade_ht_ft(game, "Puerto Rico", "Puerto Rico", scores365.SPORT_IDS["volleyball"]), "won")

    def test_set1_correct_but_match_winner_wrong_loses(self):
        self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]  # Puerto Rico takes Set 1
        game = self._game(home_score=2.0, away_score=3.0, home_winner=False, away_winner=True)  # but loses the match
        self.assertEqual(scores365.grade_ht_ft(game, "Puerto Rico", "Puerto Rico", scores365.SPORT_IDS["volleyball"]), "lost")

    def test_no_sport_id_falls_back_to_quarters_breakdown_untouched(self):
        # A stray call site that forgets to pass sport_id must keep the
        # original basketball/football behavior, not silently misread a
        # volleyball match's Set 1 as if it were a quarter score.
        orig_quarters_breakdown = scores365.quarters_breakdown
        scores365.quarters_breakdown = lambda game_id, through_quarter: None
        try:
            self._sets = [{"set_number": 1, "home": 25, "away": 20, "is_live": False}]
            self.assertIsNone(scores365.grade_ht_ft(self._game(status_group=3), "Puerto Rico", "Puerto Rico"))
        finally:
            scores365.quarters_breakdown = orig_quarters_breakdown


class FetchGamesForSportRetriesFailedPages(unittest.TestCase):
    """_fetch_games_for_sport's own multi-page walk used to silently
    truncate the whole list on a single transient page failure instead of
    retrying - confirmed live, this made a genuinely still-live volleyball
    match (Latvia vs Hungary) appear "missing" for a poll cycle and get
    voided by a tracker's MAX_CONSECUTIVE_MISSES safety net, despite being
    present in the feed moments before and after. _get_retrying now retries
    a failed page once (PAGE_FETCH_RETRIES total attempts) before giving
    up. Monkeypatches scores365._get directly (the underlying HTTP call) so
    this exercises the real retry/pagination logic without a network
    request."""

    def setUp(self):
        self._orig_get = scores365._get
        self._orig_cache = scores365._games_cache
        scores365._games_cache = {}  # bypass GAMES_CACHE_SECONDS between test cases
        self._calls: list = []

    def tearDown(self):
        scores365._get = self._orig_get
        scores365._games_cache = self._orig_cache

    def _install(self, responses):
        queue = list(responses)

        def fake_get(url, **params):
            self._calls.append(url)
            behavior = queue.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

        scores365._get = fake_get

    def test_a_single_transient_failure_on_a_forward_page_is_retried_and_recovered(self):
        self._install([
            {"games": [{"id": 1}], "paging": {"nextPage": "/page2"}},
            scores365.ScoresError("timeout"),  # first attempt at page2 fails
            {"games": [{"id": 2}], "paging": {}},  # retry succeeds
        ])
        games = scores365._fetch_games_for_sport(8)
        self.assertEqual([g["id"] for g in games], [1, 2])
        self.assertEqual(len(self._calls), 3)

    def test_a_page_that_fails_every_retry_still_stops_the_walk_but_keeps_what_it_has(self):
        self._install([
            {"games": [{"id": 1}], "paging": {"nextPage": "/page2"}},
            scores365.ScoresError("timeout"),
            scores365.ScoresError("timeout"),
        ])
        games = scores365._fetch_games_for_sport(8)
        self.assertEqual([g["id"] for g in games], [1])

    def test_the_base_fetch_itself_also_gets_retried(self):
        self._install([
            scores365.ScoresError("timeout"),
            {"games": [{"id": 1}], "paging": {}},
        ])
        games = scores365._fetch_games_for_sport(8)
        self.assertEqual([g["id"] for g in games], [1])

    def test_base_fetch_failing_every_retry_raises(self):
        self._install([
            scores365.ScoresError("timeout"),
            scores365.ScoresError("timeout"),
        ])
        with self.assertRaises(scores365.ScoresError):
            scores365._fetch_games_for_sport(8)


class FindMatchForTeam(unittest.TestCase):
    """find_match_for_team backs every auto-tracked pick's match lookup -
    monkeypatches _fetch_games_for_sport so this exercises the real
    date/status bounding logic without a live 365scores request."""

    def setUp(self):
        self._orig_fetch = scores365._fetch_games_for_sport
        self._games: list = []
        scores365._fetch_games_for_sport = lambda sport_id: self._games

    def tearDown(self):
        scores365._fetch_games_for_sport = self._orig_fetch

    def _game(self, home, away, status_group, days_offset, hour=18):
        # A KST/Eastern-agnostic "days from today, at a fixed local hour"
        # anchor - avoids the test being sensitive to what time it's
        # actually run at, the same reasoning as koreabaseball.py's own
        # 2 PM KST anchor.
        now = datetime.datetime.now(tz=scores365.EASTERN)
        start = (now + datetime.timedelta(days=days_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return {
            "homeCompetitor": {"name": home}, "awayCompetitor": {"name": away},
            "statusGroup": status_group, "startTime": start.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def test_default_never_returns_an_already_finished_game(self):
        # The exact bug this was built to stop: confirmed live, a team
        # that already finished playing today still resolved to that same
        # stale finished game hours later.
        self._games = [self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0)]
        self.assertIsNone(scores365.find_match_for_team("Milwaukee Brewers", "baseball"))

    def test_default_finds_tomorrows_game_when_todays_already_finished(self):
        self._games = [
            self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0),
            self._game("Milwaukee Brewers", "San Diego Padres", status_group=2, days_offset=1),
        ]
        result = scores365.find_match_for_team("Milwaukee Brewers", "baseball")
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "San Diego Padres")

    def test_default_never_looks_more_than_a_day_ahead(self):
        # Confirmed live concern: a team's actual next game sitting several
        # days out (bye day, rescheduled) must never get silently attached
        # to a pick posted today - GreenFox only ever posts for today's or
        # the next day's slate.
        self._games = [self._game("Milwaukee Brewers", "San Diego Padres", status_group=2, days_offset=3)]
        self.assertIsNone(scores365.find_match_for_team("Milwaukee Brewers", "baseball"))

    def test_default_still_finds_todays_live_or_upcoming_game(self):
        self._games = [self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=2, days_offset=0)]
        result = scores365.find_match_for_team("Milwaukee Brewers", "baseball")
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "Los Angeles Dodgers")

    def test_default_never_resolves_to_a_stale_past_day_game(self):
        self._games = [self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=-1)]
        self.assertIsNone(scores365.find_match_for_team("Milwaukee Brewers", "baseball"))

    def test_tracktoday_bounds_find_todays_finished_game(self):
        self._games = [self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0)]
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=1, allow_finished=True,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "Los Angeles Dodgers")

    def test_tracktoday_bounds_fall_back_to_yesterdays_finished_game(self):
        self._games = [self._game("Milwaukee Brewers", "San Diego Padres", status_group=4, days_offset=-1)]
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=1, allow_finished=True,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "San Diego Padres")

    def test_tracktoday_bounds_never_reach_two_days_back(self):
        self._games = [self._game("Milwaukee Brewers", "San Diego Padres", status_group=4, days_offset=-2)]
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=1, allow_finished=True,
        )
        self.assertIsNone(result)

    def test_tracktoday_bounds_never_reach_tomorrow(self):
        self._games = [self._game("Milwaukee Brewers", "San Diego Padres", status_group=2, days_offset=1)]
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=1, allow_finished=True,
        )
        self.assertIsNone(result)

    def test_live_game_always_wins_regardless_of_bounds(self):
        self._games = [
            self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0),
            self._game("Milwaukee Brewers", "San Diego Padres", status_group=3, days_offset=0),
        ]
        result = scores365.find_match_for_team("Milwaukee Brewers", "baseball")
        self.assertEqual(result[0]["awayCompetitor"]["name"], "San Diego Padres")

    def test_reference_date_picks_the_older_game_over_the_one_closest_to_now(self):
        # Confirmed live: masterparlay.py re-resolving a days-old slip
        # against a team that's since played again picked up the NEWER
        # game instead of the one the slip was actually about, because
        # the tie-break below always preferred whichever candidate was
        # closest to the real current moment - which, once a newer game
        # exists, is never the older one anymore.
        self._games = [
            self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0),
            self._game("Milwaukee Brewers", "San Diego Padres", status_group=4, days_offset=-2),
        ]
        older_date = (datetime.datetime.now(tz=scores365.EASTERN) - datetime.timedelta(days=2)).date()
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=3, allow_finished=True, reference_date=older_date,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "San Diego Padres")

    def test_without_reference_date_still_prefers_the_game_closest_to_now(self):
        # Same setup as above, no reference_date - confirms the fix left
        # every other (real-time) caller's own behavior unchanged.
        self._games = [
            self._game("Milwaukee Brewers", "Los Angeles Dodgers", status_group=4, days_offset=0),
            self._game("Milwaukee Brewers", "San Diego Padres", status_group=4, days_offset=-2),
        ]
        result = scores365.find_match_for_team(
            "Milwaukee Brewers", "baseball", days_ahead=0, days_back=3, allow_finished=True,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["awayCompetitor"]["name"], "Los Angeles Dodgers")


class EasternDateHelpers(unittest.TestCase):
    def test_eastern_date_str_from_epoch(self):
        epoch = datetime.datetime(2026, 8, 17, 12, 0, tzinfo=scores365.EASTERN).timestamp()
        self.assertEqual(scores365.eastern_date_str(epoch), "2026-08-17")

    def test_eastern_date_str_missing_epoch_sentinel_returns_none(self):
        self.assertIsNone(scores365.eastern_date_str(0.0))

    def test_eastern_date_str_from_iso(self):
        self.assertEqual(scores365.eastern_date_str_from_iso("2026-08-17T20:00Z"), "2026-08-17")

    def test_eastern_date_str_from_iso_missing_returns_none(self):
        self.assertIsNone(scores365.eastern_date_str_from_iso(None))

    def test_eastern_date_str_from_iso_unparseable_returns_none(self):
        self.assertIsNone(scores365.eastern_date_str_from_iso("not a date"))


class SportLabel(unittest.TestCase):
    """Confirmed live: 365scores' sport_id 7 spans MLB, KBO, NPB, etc. all
    at once, and sport_id 2 (basketball) spans NBA and WNBA - a bare
    "Baseball"/"Basketball" label used to lump every one of those leagues
    together under one /performance sport bucket instead of matching each
    tracker's own league-specific label (proptracker.py's "MLB"/"NBA"/
    "WNBA", kboproptracker.py's "KBO")."""

    def test_baseball_with_no_competition_name_stays_generic(self):
        self.assertEqual(scores365.sport_label(7), "Baseball")

    def test_mlb_competition_name_resolves_to_mlb(self):
        self.assertEqual(scores365.sport_label(7, "MLB"), "MLB")

    def test_kbo_competition_name_resolves_to_kbo(self):
        self.assertEqual(scores365.sport_label(7, "KBO"), "KBO")

    def test_unrecognized_baseball_competition_name_stays_generic(self):
        # A real one confirmed live: "LMB - Playoffs - 1st Round" (Mexican
        # league) - neither "mlb" nor "kbo" appears in it.
        self.assertEqual(scores365.sport_label(7, "LMB - Playoffs - 1st Round"), "Baseball")

    def test_wnba_competition_name_still_resolves_to_wnba(self):
        self.assertEqual(scores365.sport_label(2, "WNBA"), "WNBA")

    def test_nba_competition_name_resolves_to_nba(self):
        self.assertEqual(scores365.sport_label(2, "NBA"), "NBA")

    def test_wnba_checked_before_nba_since_nba_is_a_substring(self):
        self.assertEqual(scores365.sport_label(2, "WNBA"), "WNBA")

    def test_basketball_with_no_competition_name_stays_generic(self):
        self.assertEqual(scores365.sport_label(2), "Basketball")

    def test_unrecognized_sport_id_returns_none(self):
        self.assertIsNone(scores365.sport_label(999))


class TournamentName(unittest.TestCase):
    """Backs /performance's tournament sub-grouping (see dailylog.
    sport_tournament_win_loss) - confirmed live, 365scores'
    competitionDisplayName already carries this directly for baseball
    ("MLB"/"KBO") and soccer ("Premier League", ...), just needs a
    tennis-style trailing round suffix stripped (e.g. "Cincinnati - 3rd
    Round") so a tournament's win rate combines every round of it."""

    def test_plain_competition_name_passes_through(self):
        self.assertEqual(scores365.tournament_name({"competitionDisplayName": "MLB"}), "MLB")

    def test_tennis_round_suffix_is_stripped(self):
        self.assertEqual(scores365.tournament_name({"competitionDisplayName": "Cincinnati - 3rd Round"}), "Cincinnati")

    def test_final_suffix_is_stripped(self):
        self.assertEqual(scores365.tournament_name({"competitionDisplayName": "Hamburg - Final"}), "Hamburg")

    def test_missing_competition_name_returns_none(self):
        self.assertIsNone(scores365.tournament_name({}))


if __name__ == "__main__":
    unittest.main()

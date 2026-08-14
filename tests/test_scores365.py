#!/usr/bin/env python3
"""
Regression tests for scores365.py's name-matching and grading logic. A
name-match failure means the wrong game gets tracked (or none at all) -
which surfaces in /summary as a missing or wrong-game entry, so this is
just as load-bearing for summary/win-rate correctness as the parsing
tests, even though the bug shows up one step removed.

Run with: python -m unittest discover -s tests -t .
"""

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


if __name__ == "__main__":
    unittest.main()

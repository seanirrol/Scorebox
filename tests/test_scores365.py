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

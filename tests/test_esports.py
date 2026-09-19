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


class NormalizeSport(unittest.TestCase):
    """LoL and Mobile Legends both route through GosuGamers only (see
    _GOSU_GAME_SLUGS) - confirmed live against real GosuGamers matches
    during development (gosugamers.net/lol and /mobile-legends both have a
    live matches listing), unlike hawk.live which only ever covers Dota 2."""

    def test_lol_recognized(self):
        self.assertTrue(esports.is_supported_sport("LoL"))
        self.assertTrue(esports.is_supported_sport("League of Legends"))

    def test_mobile_legends_recognized(self):
        self.assertTrue(esports.is_supported_sport("mobilelegends"))
        self.assertTrue(esports.is_supported_sport("Mobile Legends"))
        self.assertTrue(esports.is_supported_sport("MLBB"))

    def test_unrelated_esport_not_recognized(self):
        # Confirms this didn't accidentally broaden to "any esport" - only
        # the four games actually wired up here (see _GOSU_GAME_SLUGS)
        # should be recognized.
        self.assertFalse(esports.is_supported_sport("Valorant"))


class LiveKillCountUnsupportedSport(unittest.TestCase):
    """live_kill_count's CS2 branch calls out to Strafe.com (a CS2-only
    site) - LoL/Mobile Legends must return None directly instead of
    silently making a doomed request to a site that can never have their
    data, same "no live in-map stat available" outcome as before this sport
    existed at all, just without the wasted network call."""

    def test_lol_returns_none_without_hitting_strafe(self):
        series = {"sport": "lol", "status": "inprogress", "home_team": "Team A", "away_team": "Team B"}
        self.assertIsNone(esports.live_kill_count(series))

    def test_mobile_legends_returns_none_without_hitting_strafe(self):
        series = {"sport": "mobilelegends", "status": "inprogress", "home_team": "Team A", "away_team": "Team B"}
        self.assertIsNone(esports.live_kill_count(series))


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


def _gosu_detail(
    team1_name="Team A", team2_name="Team B", team1_score=0, team2_score=0,
    games_per_match=3, starting_at_ms=None, tournament="Some Cup",
):
    """Mirrors the real shape captured live from a GosuGamers match-page's
    own embedded flight data (see _series_from_gosu_detail's own callers) -
    just the fields that function actually reads."""
    return {
        "opponents": [
            {"registrationId": 1, "name": team1_name, "score": team1_score, "opponentImage": "https://img/a"},
            {"registrationId": 2, "name": team2_name, "score": team2_score, "opponentImage": "https://img/b"},
        ],
        "team1RegistrationId": 1, "team2RegistrationId": 2,
        "gamesPerMatch": games_per_match,
        "startingAt": starting_at_ms,
        "parentTournamentName": tournament,
    }


class GosuMatchUrlRegex(unittest.TestCase):
    """_GOSU_MATCH_URL_RE only needs the two numeric ids - confirmed live
    the human-readable slug text after each is purely cosmetic (GosuGamers
    redirects a wrong-slug-but-correct-id URL to the real canonical page,
    see _curl_text's own -L comment)."""

    def test_extracts_slug_and_both_ids_from_a_real_looking_url(self):
        m = esports._GOSU_MATCH_URL_RE.search(
            "https://www.gosugamers.net/dota2/tournaments/63119-pgl-wallachia-season-9/matches/659411-lgd-gaming-vs-yakult-brothers"
        )
        self.assertEqual(m.groups(), ("dota2", "63119", "659411"))

    def test_still_matches_with_placeholder_slug_text(self):
        m = esports._GOSU_MATCH_URL_RE.search("https://www.gosugamers.net/cs2/tournaments/1-x/matches/2-x")
        self.assertEqual(m.groups(), ("cs2", "1", "2"))

    def test_does_not_match_a_bare_matches_list_url(self):
        self.assertIsNone(esports._GOSU_MATCH_URL_RE.search("https://www.gosugamers.net/dota2/matches"))

    def test_does_not_match_an_unrelated_url(self):
        self.assertIsNone(esports._GOSU_MATCH_URL_RE.search("https://www.espn.com/nba/game/_/id/1"))


class GetSeriesByUrl(unittest.TestCase):
    """get_series_by_url is /tracktoday's escape hatch for a real match not
    yet on esports.get_series' own list-search (see that function's own
    docstring - confirmed live for PGL Wallachia Season 9's later Round 1
    matches). Monkeypatches _gosu_match_detail so this exercises the URL-
    parsing/sport-validation/orientation logic without a live request."""

    def setUp(self):
        self._orig = esports._gosu_match_detail
        self._detail = None

    def tearDown(self):
        esports._gosu_match_detail = self._orig

    def _stub(self, detail):
        self._detail = detail
        esports._gosu_match_detail = lambda game_slug, match: self._detail

    def test_resolves_a_real_looking_dota2_url(self):
        self._stub(_gosu_detail(team1_name="LGD Gaming", team2_name="Yakult Brothers"))
        result = esports.get_series_by_url(
            "dota2",
            "https://www.gosugamers.net/dota2/tournaments/63119-x/matches/659411-x",
            "LGD Gaming", "Yakult Brothers",
        )
        self.assertEqual(result["home_team"], "LGD Gaming")
        self.assertEqual(result["away_team"], "Yakult Brothers")
        self.assertEqual(result["tournament"], "Some Cup")

    def test_team_a_orients_which_side_is_home_regardless_of_url_order(self):
        self._stub(_gosu_detail(team1_name="LGD Gaming", team2_name="Yakult Brothers"))
        result = esports.get_series_by_url(
            "dota2", "https://www.gosugamers.net/dota2/tournaments/63119-x/matches/659411-x",
            "Yakult Brothers", "LGD Gaming",
        )
        self.assertEqual(result["home_team"], "Yakult Brothers")
        self.assertEqual(result["away_team"], "LGD Gaming")

    def test_wrong_sport_slug_in_url_is_rejected(self):
        self._stub(_gosu_detail())
        result = esports.get_series_by_url(
            "cs2",  # url says dota2, sport param says cs2 - mismatch
            "https://www.gosugamers.net/dota2/tournaments/63119-x/matches/659411-x",
            "Team A", "Team B",
        )
        self.assertIsNone(result)

    def test_unparseable_url_returns_none_without_ever_fetching(self):
        self._stub(_gosu_detail())
        result = esports.get_series_by_url("dota2", "not a url at all", "Team A", "Team B")
        self.assertIsNone(result)

    def test_no_detail_found_returns_none(self):
        self._stub(None)
        result = esports.get_series_by_url(
            "dota2", "https://www.gosugamers.net/dota2/tournaments/63119-x/matches/659411-x", "Team A", "Team B",
        )
        self.assertIsNone(result)

    def test_not_yet_started_match_reports_notstarted_status(self):
        future_ms = (time.time() + 3600) * 1000
        self._stub(_gosu_detail(starting_at_ms=future_ms))
        result = esports.get_series_by_url(
            "dota2", "https://www.gosugamers.net/dota2/tournaments/63119-x/matches/659411-x", "Team A", "Team B",
        )
        self.assertEqual(result["status"], "notstarted")
        self.assertEqual(result["home_score"], 0)
        self.assertEqual(result["away_score"], 0)


if __name__ == "__main__":
    unittest.main()

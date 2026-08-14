#!/usr/bin/env python3
"""
Regression tests for picks.py - every case here is a real parsing bug that
shipped and was confirmed live at some point, not a hypothetical. A picks
message that fails to parse (or mis-parses) never makes it into dailylog at
all, or does so under the wrong player/stat/team - which is the single
biggest source of wrong or missing /summary and win-rate entries, so this
file exists specifically to stop those regressions from recurring silently.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import picks


class MatchupPrefixedProps(unittest.TestCase):
    """A "Team A vs Team B - Player Over N Stat" line under a Props tag
    used to get swallowed by the generic team-total parser (which doesn't
    validate its captured team name at all), mistracking the whole line as
    a nonsense combined-game total instead of the intended player prop."""

    def test_matchup_prefixed_prop_recovers_the_real_prop(self):
        pick = picks.parse_pick_line(
            "[MLB Props] Colorado Rockies vs Arizona Diamondbacks - "
            "Corbin Carroll Over 0.5 Total Bases (Alt Line) (FanDuel -100)"
        )
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["player"], "Corbin Carroll")
        self.assertEqual(pick["stat"], "Total Bases")

    def test_plain_prop_without_matchup_prefix_still_works(self):
        pick = picks.parse_pick_line(
            "[MLB Props] Corbin Carroll Over 0.5 Total Bases (Alt Line) (Fanatics -170)"
        )
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["player"], "Corbin Carroll")

    def test_genuine_game_total_not_tagged_as_props_still_a_total(self):
        pick = picks.parse_pick_line("[MLB] Angels vs Giants - Over 8.5 Total Runs (FanDuel -110)")
        self.assertEqual(pick["kind"], "total")
        self.assertEqual(pick["team"], "Angels")

    def test_genuine_named_team_total_under_props_tag_still_works(self):
        pick = picks.parse_pick_line("[MLB Props] Angels vs Giants - Angels Over 3.5 Total Runs (FanDuel -110)")
        self.assertEqual(pick["kind"], "team_total")
        self.assertEqual(pick["team"], "Angels")

    def test_mistagged_unparseable_props_line_returns_none_not_a_guess(self):
        # Tagged as Props but has no prop shape at all - must not fall
        # through to a guessed team-pick/total.
        pick = picks.parse_pick_line("[MLB Props] Angels vs Giants - Over 8.5 Total Runs (FanDuel -110)")
        self.assertIsNone(pick)


class StatAliases(unittest.TestCase):
    """Wording variants that don't substring-match the catalog label at
    all - each one silently dropped the whole pick until an explicit
    alias was added."""

    def test_made_threes_maps_to_3_pointers_made(self):
        pick = picks.parse_pick_line("[WNBA Props] Shakira Austin Under 0.5 Made Threes (PrizePicks -535)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "3-Pointers Made")

    def test_pass_yds_abbreviation_maps_to_passing_yards(self):
        pick = picks.parse_pick_line("[NFL] Tyrod Taylor Over 75.5 Pass Yds (Alt Line) (DraftKings +765)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "Passing Yards")

    def test_rush_yds_abbreviation_maps_to_rushing_yards(self):
        pick = picks.parse_pick_line("[NFL] Emanuel Wilson Over 15.5 Rush Yds (Alt Line) (DraftKings +165)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "Rushing Yards")


class YrfiNrfiSeparators(unittest.TestCase):
    """The matchup separator regex only recognized "vs"/"vs." - a real
    source used "@" (the away-@-home convention) and those lines silently
    parsed to zero picks."""

    def test_at_separator_parses(self):
        pick = picks.parse_pick_line(
            "[YRFI/NRFI Slate] Philadelphia Phillies @ St. Louis Cardinals - "
            "NRFI - No Runs 1st Inning (FanDuel -115)"
        )
        self.assertEqual(pick["kind"], "inning_runs")
        self.assertEqual(pick["pick_type"], "NRFI")

    def test_vs_separator_still_parses(self):
        pick = picks.parse_pick_line(
            "[MLB] Baltimore Orioles vs Minnesota Twins - NRFI - No Runs 1st Inning (Fanatics -110)"
        )
        self.assertEqual(pick["kind"], "inning_runs")

    def test_colon_separator_before_market_still_parses(self):
        pick = picks.parse_pick_line("[MLB] Team A vs Team B: NRFI - No Runs 1st Inning (FanDuel -110)")
        self.assertEqual(pick["kind"], "inning_runs")


class NamedTeamTotalCanonicalName(unittest.TestCase):
    """The named-team-total regex's capture can pull in trailing wording
    along with the real team name - names_match still validated it
    correctly, but the function used to return the raw (junk-suffixed)
    capture instead of the clean matched team name."""

    def test_trailing_wording_in_capture_still_resolves_to_clean_team_name(self):
        pick = picks.parse_pick_line(
            "[Soccer] Austin FC vs Club America - Club America Team Total OVER 1.5 Goals (Alt Line) (Fanatics -115)"
        )
        self.assertEqual(pick["kind"], "team_total")
        self.assertEqual(pick["team"], "Club America")

    def test_clean_named_team_total_unaffected(self):
        pick = picks.parse_pick_line(
            "[MLB] New York Yankees vs Chicago White Sox - "
            "New York Yankees Over 3.5 Team Total Runs (FanDuel -110)"
        )
        self.assertEqual(pick["team"], "New York Yankees")


class BareSectionHeaders(unittest.TestCase):
    """A bare (non-bracket-tagged) message uses a plain header line
    followed by a bullet list. Two separate bugs lived here: a sub-header
    with trailing text got parsed as a literal pick, and (regression risk)
    a real source's un-bulleted pick lines must still parse."""

    def test_sub_header_with_trailing_text_does_not_become_a_bogus_pick(self):
        msg = (
            "MLB\n"
            "- Chicago Cubs vs Washington Nationals - Chicago Cubs Over 3.5 Team Total Runs (Alt Line) (Bet365 -135)\n"
            "\n"
            "MLB Home Run Predictor\n"
            "- Bryan Reynolds Over 0.5 Home Runs (DraftKings +958)\n"
            "- Cody Bellinger Over 0.5 Home Runs (DraftKings +290)"
        )
        results = picks.parse_picks_message(msg)
        raws = [p["raw"] for p in results]
        self.assertNotIn("MLB Home Run Predictor", raws)
        self.assertEqual(len(results), 3)

    def test_unbulleted_pick_lines_still_parse(self):
        # Confirmed live: a real message posted bare "PlayerName - Over N
        # Stat" and "PlayerName ML" lines with no bullet at all - an
        # earlier fix wrongly treated any un-bulleted line as a header and
        # dropped all of these.
        msg = (
            "NFL\n"
            "Emanuel Wilson - Over 15.5 Rush Yards\n"
            "Justin Fields - Over 75.5 Rush Yards\n"
            "\n"
            "Soccer\n"
            "Austin FC vs Club America - Club America Team Total OVER 1.5 Goals (Alt Line) (Fanatics -115)\n"
            "\n"
            "Tennis\n"
            "Jan-Lennard Struff ML"
        )
        results = picks.parse_picks_message(msg)
        self.assertEqual(len(results), 4)
        kinds = {p["raw"]: p["kind"] for p in results}
        self.assertEqual(kinds["Emanuel Wilson - Over 15.5 Rush Yards"], "playerprops")
        self.assertEqual(kinds["Justin Fields - Over 75.5 Rush Yards"], "playerprops")
        self.assertEqual(kinds["Jan-Lennard Struff ML"], "track")

    def test_cosmetic_player_props_sub_label_does_not_reset_category(self):
        msg = (
            "Tennis\n"
            "- Marcos Giron (Fanatics -1985)\n"
            "- Jakub Mensik ML (DraftKings -583)\n"
            "\n"
            "NFL\n"
            "Player props\n"
            "- Tyrod Taylor Over 75.5 Pass Yds (Alt Line) (DraftKings +765)\n"
            "\n"
            "1. Aces Moneyline (Fanatics -557)\n"
        )
        results = picks.parse_picks_message(msg)
        raws = {p["raw"]: p for p in results}
        self.assertIn("Marcos Giron (Fanatics -1985)", raws)
        self.assertIn("Jakub Mensik ML (DraftKings -583)", raws)
        self.assertEqual(raws["Tyrod Taylor Over 75.5 Pass Yds (Alt Line) (DraftKings +765)"]["kind"], "playerprops")
        self.assertEqual(raws["Aces Moneyline (Fanatics -557)"]["section"], "NFL")

    def test_bare_stat_shaped_line_with_no_header_at_all_infers_sport(self):
        results = picks.parse_picks_message("Dustin May Over 1.5 Earned Runs Allowed (FanDuel -110)")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "playerprops")
        self.assertEqual(results[0]["player"], "Dustin May")


if __name__ == "__main__":
    unittest.main()

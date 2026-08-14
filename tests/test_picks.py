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


class PitchingOutsAndMidPhraseAltLine(unittest.TestCase):
    """Two distinct real-message bugs confirmed live in the same slate:
    "Pitching Outs" wasn't a recognized stat at all (fell through to a
    team-pick guess on the pitcher's own name), and "(Alt Line)" placed
    BETWEEN the number and the stat name (rather than at the very end,
    where _clean_line's trailing-paren stripping would remove it) got
    captured as part of the stat name and matched nothing."""

    def test_pitching_outs_recognized_as_a_stat(self):
        pick = picks.parse_pick_line("[MLB] George Kirby Over 17.5 Pitching Outs (PrizePicks O 17.5)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "Pitching Outs")

    def test_pitching_outs_with_trailing_alt_line_still_works(self):
        pick = picks.parse_pick_line("[MLB] Jake Bennett Over 15.5 Pitching Outs (Alt Line) (Underdog Higher 17.5)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "Pitching Outs")

    def test_alt_line_between_number_and_stat_name(self):
        pick = picks.parse_pick_line("[WNBA] Bridget Carleton OVER 1.5 (Alt Line) THREE POINTERS (Underdog -235)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "3-Pointers Made")
        self.assertEqual(pick["line"], 1.5)

    def test_alt_line_at_the_end_still_works_unaffected(self):
        pick = picks.parse_pick_line("[WNBA] Bridget Carleton OVER 2.5 THREE POINTERS (PrizePicks O 2.5)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["stat"], "3-Pointers Made")


class NflSpreadNoMatchup(unittest.TestCase):
    """A full-game point-spread pick like "Denver Broncos -3.5" had no
    parser at all anywhere in this file (only tennis games/sets and F5/
    esports handicaps existed) - it fell through to the bare-name fallback,
    which rejects anything with a digit, and silently vanished with no
    botlog trace. Confirmed live: a real "[NFL] NFL / Denver Broncos -3.5
    (Fanatics -100) / New York Jets -5.0 (Bet365 -105)" message parsed 0/3
    lines."""

    def test_bracket_tagged_spread_line(self):
        pick = picks.parse_pick_line("[NFL] Denver Broncos -3.5 (Fanatics -100)")
        self.assertEqual(pick, {"kind": "team_total", "sport": "nfl", "team": "Denver Broncos", "direction": "spread", "line": -3.5})

    def test_underdog_positive_spread(self):
        pick = picks.parse_pick_line("[NFL] New York Jets +5.0 (Bet365 -105)")
        self.assertEqual(pick["direction"], "spread")
        self.assertEqual(pick["line"], 5.0)

    def test_bare_header_bullet_list(self):
        msg = "NFL\n- Denver Broncos -3.5 (Fanatics -100)\n- New York Jets -5.0 (Bet365 -105)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0]["team"], "Denver Broncos")
        self.assertEqual(picked[0]["line"], -3.5)
        self.assertEqual(picked[1]["team"], "New York Jets")
        self.assertEqual(picked[1]["line"], -5.0)

    def test_matchup_prefixed_line_is_not_misread_as_a_spread_pick(self):
        # No opponent-named spread parser exists for this shape yet - the
        # nomatchup guard just keeps it out of THIS parser rather than
        # capturing "Team A @ Team B" whole as one literal team+line. It
        # still falls through to the pre-existing generic team-pick
        # fallback below (a separate, unrelated gap: that fallback's own
        # matchup-separator list doesn't include "@" at all) - not
        # reproducing that behavior here since it's out of scope for this
        # spread-specific fix.
        pick = picks.parse_pick_line("[NFL] Denver Broncos @ New York Jets -3.5")
        self.assertNotEqual((pick or {}).get("kind"), "team_total")

    def test_ordinary_moneyline_pick_still_unaffected(self):
        pick = picks.parse_pick_line("[NFL] Denver Broncos ML (Fanatics -150)")
        self.assertEqual(pick["kind"], "track")


class WinASetNoMatchup(unittest.TestCase):
    """"Player to Win at Least 1 Set" / "Player to Win a Set" with no
    opponent named at all - confirmed live, 7 of 8 picks in one real
    message vanished with no botlog trace because the existing
    _WIN_A_SET_RE required a full "Team A vs Team B - " matchup prefix,
    and the bare wording fell through to the simple-name fallback, which
    rejects anything with a digit in it (the "1" in "at least 1 set")."""

    def test_at_least_1_set_wording_with_no_matchup(self):
        msg = "Tennis\nStefanos Tsitsipas to Win at Least 1 Set (Alt Line) (Bet365 -275)"
        results = picks.parse_picks_message(msg)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "tennis_win_a_set")
        self.assertEqual(results[0]["team"], "Stefanos Tsitsipas")
        self.assertEqual(results[0]["direction"], "yes")

    def test_plain_to_win_a_set_wording_with_no_matchup(self):
        msg = "Tennis\nVenus Williams to Win a Set (DraftKings -150)"
        results = picks.parse_picks_message(msg)
        self.assertEqual(results[0]["team"], "Venus Williams")

    def test_no_prefix_flips_direction(self):
        msg = "Tennis\nNo Venus Williams to Win a Set (DraftKings +150)"
        results = picks.parse_picks_message(msg)
        self.assertEqual(results[0]["team"], "Venus Williams")
        self.assertEqual(results[0]["direction"], "no")

    def test_matchup_prefixed_wording_still_works(self):
        # The pre-existing _WIN_A_SET_RE path, must not regress.
        pick = picks.parse_pick_line(
            "[Tennis] Venus Williams vs Serena Williams - Venus Williams to Win a Set (DraftKings -150)"
        )
        self.assertEqual(pick["kind"], "tennis_win_a_set")
        self.assertEqual(pick["team"], "Venus Williams")

    def test_whole_real_message_all_eight_picks_parse(self):
        msg = (
            "Tennis\n"
            "Stefanos Tsitsipas to Win at Least 1 Set (Alt Line) (Bet365 -275)\n"
            "Hubert Hurkacz to Win at Least 1 Set (Alt Line) (Bet365 -270)\n"
            "Wang Xinyu to Win at Least 1 Set (Alt Line) (Bet365 -220)\n"
            "Grigor Dimitrov to Win at Least 1 Set (Alt Line) (FanDuel -240)\n"
            "Karen Khachanov to Win at Least 1 Set (Alt Line) (DraftKings -270)\n"
            "Elisabetta Cocciaretto to Win at Least 1 Set (Alt Line) (Fanatics -245)\n"
            "Karolina Pliskova to Win at Least 1 Set (Alt Line) (Bet365 -225)\n"
            "Liudmila Samsonova +1.5 Sets (Alt Line) (Bet365 -195)"
        )
        results = picks.parse_picks_message(msg)
        self.assertEqual(len(results), 8)


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

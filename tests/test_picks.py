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


class CleanLabel(unittest.TestCase):
    """clean_label (used for /summary and dailylog display, not parsing)
    strips every trailing "(Bookmaker odds)"/"(Alt Line)" annotation off a
    pick line - confirmed live, a real label showed
    "Paul Skenes Over 5.5 Strikeouts (Alt Line) (FanDuel O 6.5 (+122))" in
    /summary instead of just the clean pick text."""

    def test_single_trailing_paren_stripped(self):
        self.assertEqual(picks.clean_label("Tampa Bay Rays ML (Bet365 -148)"), "Tampa Bay Rays ML")

    def test_two_flat_trailing_parens_both_stripped(self):
        self.assertEqual(
            picks.clean_label("Jorge Polanco Over 0.5 Total Bases (Alt Line) (DraftKings O 0.5)"),
            "Jorge Polanco Over 0.5 Total Bases",
        )

    def test_nested_parens_in_the_trailing_group_fully_stripped(self):
        # The odds themselves are wrapped in their own parens inside the
        # bookmaker one ("(FanDuel O 6.5 (+122))") - a flat non-nested
        # regex only ever matched the innermost "(+122)", leaving
        # "(FanDuel O 6.5 " dangling unstripped before this fix.
        self.assertEqual(
            picks.clean_label("Paul Skenes Over 5.5 Strikeouts (Alt Line) (FanDuel O 6.5 (+122))"),
            "Paul Skenes Over 5.5 Strikeouts",
        )

    def test_unbalanced_trailing_paren_left_alone(self):
        # No matching "(" for the trailing ")" - stripping would eat real
        # pick text, so this is deliberately left untouched rather than
        # guessed at.
        self.assertEqual(picks.clean_label("Weird pick text)"), "Weird pick text)")

    def test_line_with_no_trailing_parens_unaffected(self):
        self.assertEqual(picks.clean_label("Las Vegas Aces ML"), "Las Vegas Aces ML")


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

    def test_single_team_name_prefix_recovers_the_real_prop(self):
        # Same idea as the full "Team A vs Team B - " matchup prefix above,
        # but only one team named - no "vs"/"@" separator at all, so
        # has_matchup never triggers. Confirmed live in a real parlay slip's
        # own leg wording ("Las Vegas Aces - A'ja Wilson Over 21.5 Points")
        # - player used to capture the team name too and fail to resolve.
        pick = picks.parse_pick_line("[WNBA Props] Las Vegas Aces - A'ja Wilson Over 21.5 Points (DraftKings -225)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["player"], "A'ja Wilson")
        self.assertEqual(pick["stat"], "Points")

    def test_single_team_name_prefix_combo_stat_prop(self):
        pick = picks.parse_pick_line("[WNBA Props] Chicago Sky - Angel Reese P+R+A Over 24.5 (PrizePicks -139)")
        self.assertEqual(pick["kind"], "playerprops")
        self.assertEqual(pick["player"], "Angel Reese")


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


class BoxingMoneyline(unittest.TestCase):
    """Boxing moneyline picks - added after confirming neither 365scores
    nor ESPN's public API cover boxing at all (BoxingScene.com is the data
    source instead - see boxing.py). Mirrors mma's own handling: moneyline
    only, and _parse_team_pick already covers both a matchup-prefixed line
    and a bare fighter name with no opponent."""

    def test_matchup_prefixed_bracket_tagged(self):
        pick = picks.parse_pick_line("[Boxing] Claressa Shields vs. Kaye Scott - Claressa Shields ML (Bet365 -305)")
        self.assertEqual(pick, {"kind": "boxing_moneyline", "team": "Claressa Shields"})

    def test_bare_fighter_no_opponent(self):
        pick = picks.parse_pick_line("[Boxing] Troy Isley ML (DraftKings -250)")
        self.assertEqual(pick, {"kind": "boxing_moneyline", "team": "Troy Isley"})

    def test_bare_header_bullet_list(self):
        msg = (
            "Boxing\n"
            "Claressa Shields vs. Kaye Scott - Claressa Shields ML (Bet365 -305)\n"
            "Troy Isley vs. Derrick Hicks - Troy Isley ML (DraftKings -250)"
        )
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 2)
        self.assertEqual([p["team"] for p in picked], ["Claressa Shields", "Troy Isley"])
        self.assertTrue(all(p["kind"] == "boxing_moneyline" for p in picked))


class EsportsBareHeaderNoMatchupFallback(unittest.TestCase):
    """Every esports market requires an explicit "Team A vs Team B" matchup
    (hawk.live/GosuGamers can only resolve a match by both team names
    together - see picks.py's own comment above _ESPORTS_MAP_HANDICAP_RE),
    so _parse_esports_pick correctly returns None for a bare no-matchup
    line. But under a bare (non-bracket-tagged) "Dota 2"/"CS2" header, that
    None used to fall all the way through to the generic
    _is_simple_pick_name bare-team fallback meant for ordinary sports -
    confirmed live, a real "Iron Wing to Win at Least One Map"/"BoomBoys
    ML" pick got misread as literal (nonsense) team names and routed to
    the generic scores365-backed auto-track, which has no esports coverage
    at all, so it queued and retried forever with zero chance of ever
    resolving."""

    def test_bare_header_no_matchup_lines_are_rejected_not_misrouted(self):
        msg = (
            "Dota 2\n"
            "Iron Wing to Win at Least One Map\n"
            "LGD Gaming Wins at Least One Map\n"
            "BoomBoys ML"
        )
        self.assertEqual(picks.parse_picks_message(msg), [])

    def test_bare_header_with_a_real_matchup_still_works(self):
        msg = "Dota 2\nOG vs Huligani - OG ML"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["kind"], "esports_match_winner")

    def test_ordinary_sport_bare_name_fallback_unaffected(self):
        msg = "WNBA\nIndiana Fever"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(picked, [{"kind": "track", "sport": "basketball", "team": "Indiana Fever", "section": "WNBA", "raw": "Indiana Fever"}])


class EsportsMapKillsHandicap(unittest.TestCase):
    """"Team X (-4.5) Map 1 Kills Handicap" - a spread on one specific map's
    own kill count, distinct from the maps-won Map Handicap and the
    series-combined Total Kills markets already covered above."""

    def test_parens_line_form(self):
        pick = picks.parse_pick_line(
            "[Dota 2] Nigma Galaxy vs Team Falcons - Team Falcons (-4.5) Map 1 Kills Handicap (DraftKings -110)"
        )
        self.assertEqual(pick, {
            "kind": "esports_map_kills_handicap", "sport": "dota2",
            "team_a": "Nigma Galaxy", "team_b": "Team Falcons", "team": "Team Falcons",
            "line": -4.5, "map_number": 1,
        })

    def test_no_parens_line_form(self):
        pick = picks.parse_pick_line("[Dota 2] Nigma Galaxy vs Team Falcons - Team Falcons -4.5 Map 1 Kills Handicap")
        self.assertEqual(pick["line"], -4.5)
        self.assertEqual(pick["map_number"], 1)

    def test_positive_line_and_later_map_number(self):
        pick = picks.parse_pick_line("[Dota 2] Nigma Galaxy vs Team Falcons - Nigma Galaxy (+4.5) Map 2 Kills Handicap")
        self.assertEqual(pick["team"], "Nigma Galaxy")
        self.assertEqual(pick["line"], 4.5)
        self.assertEqual(pick["map_number"], 2)

    def test_unrelated_named_team_returns_none(self):
        pick = picks.parse_pick_line("[Dota 2] Nigma Galaxy vs Team Falcons - Team Spirit (-4.5) Map 1 Kills Handicap")
        self.assertIsNone(pick)

    def test_does_not_collide_with_plain_map_handicap(self):
        # Same "Team X (-N) Map ... Handicap" shape minus "Kills" - must
        # still resolve as the maps-won handicap, not this market.
        pick = picks.parse_pick_line("[Dota 2] Nigma Galaxy vs Team Falcons - Team Falcons (-1.5) Map Handicap")
        self.assertEqual(pick["kind"], "esports_map_handicap")

    def test_does_not_collide_with_total_kills(self):
        pick = picks.parse_pick_line("[Dota 2] Nigma Galaxy vs Team Falcons - Over 130.5 Total Kills")
        self.assertEqual(pick["kind"], "esports_total_kills")

    def test_cs2_not_supported(self):
        # Kills are Dota 2-only - CS2 has no kill data anywhere.
        pick = picks.parse_pick_line("[CS2] Team Liquid vs Team Vitality - Team Liquid (-4.5) Map 1 Kills Handicap")
        self.assertIsNone(pick)


class EsportsMapTotalKills(unittest.TestCase):
    """"Over/Under N Map 1 Total Kills" - combined kill total within one
    specific map (both teams summed), distinct from the series-wide Total
    Kills market and the Map Kills Handicap spread above."""

    def test_marker_before_the_number(self):
        pick = picks.parse_pick_line("[Dota 2] Team Liquid vs Team Yandex - Over 50.5 Map 1 Total Kills")
        self.assertEqual(pick, {
            "kind": "esports_map_total_kills", "sport": "dota2",
            "team_a": "Team Liquid", "team_b": "Team Yandex",
            "direction": "over", "line": 50.5, "map_number": 1,
        })

    def test_marker_after_the_number(self):
        pick = picks.parse_pick_line("[Dota 2] Team Liquid vs Team Yandex - Map 1 Total Kills Over 50.5")
        self.assertEqual(pick["direction"], "over")
        self.assertEqual(pick["line"], 50.5)
        self.assertEqual(pick["map_number"], 1)

    def test_under_direction_and_later_map_number(self):
        pick = picks.parse_pick_line("[Dota 2] Team Liquid vs Team Yandex - Under 45 Map 2 Total Kills")
        self.assertEqual(pick["direction"], "under")
        self.assertEqual(pick["map_number"], 2)

    def test_does_not_collide_with_series_total_kills(self):
        pick = picks.parse_pick_line("[Dota 2] Team Liquid vs Team Yandex - Over 130.5 Total Kills")
        self.assertEqual(pick["kind"], "esports_total_kills")

    def test_does_not_collide_with_map_kills_handicap(self):
        pick = picks.parse_pick_line("[Dota 2] Team Liquid vs Team Yandex - Team Liquid (-4.5) Map 1 Kills Handicap")
        self.assertEqual(pick["kind"], "esports_map_kills_handicap")

    def test_cs2_not_supported(self):
        pick = picks.parse_pick_line("[CS2] Team Liquid vs Team Vitality - Over 50.5 Map 1 Total Kills")
        self.assertIsNone(pick)


class MisfiledPropUnderWrongSectionHeader(unittest.TestCase):
    """A player-prop-shaped line whose stat belongs to a DIFFERENT sport
    than its own section header (the tipster's own mistake, not a code
    gap) used to fall through to _parse_bare_team_total_pick, which
    silently treated the player's own name as if it were a literal team.
    Confirmed live: a real "Yoshi Yamamoto Over 4.5 Strikeouts" pick (an
    MLB pitcher prop) posted under a "WNBA" header parsed as a basketball
    team-total bet on a team named "Yoshi Yamamoto" and queued forever
    searching for a team that will never exist."""

    def test_baseball_prop_under_wnba_header_is_rejected_not_misparsed(self):
        msg = (
            "WNBA\n"
            "Kelsey Mitchell Over 20.5 Points (DraftKings -170)\n"
            "Yoshi Yamamoto Over 4.5 Strikeouts (Underdog -145)\n"
            "Caitlin Clark Over 6.5 Assists (DraftKings -130)"
        )
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 2)
        self.assertNotIn("Yoshi Yamamoto", [p.get("team") or p.get("player") for p in picked])

    def test_bracket_tagged_misfiled_prop_returns_none(self):
        pick = picks.parse_pick_line("[WNBA] Yoshi Yamamoto Over 4.5 Strikeouts (Underdog -145)")
        self.assertIsNone(pick)

    def test_genuine_bare_team_total_with_no_trailing_word_unaffected(self):
        pick = picks.parse_pick_line("[MLB] Los Angeles Dodgers Over 4.5 (FanDuel -110)")
        self.assertEqual(pick, {"kind": "team_total", "sport": "baseball", "team": "Los Angeles Dodgers", "direction": "over", "line": 4.5})


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


class NflSpreadWithMatchup(unittest.TestCase):
    """"Team A at Team B - Team X -3.5" - a spread pick WITH the matchup
    named (unlike NflSpreadNoMatchup above), using the "at" separator
    convention very common for NFL/MLB. Confirmed live: 3 real picks
    worded this way ("Packers at Broncos - Broncos -3.5",
    "Jets at Steelers - Steelers +1.5", "Panthers at Jaguars - Under
    38.5") sat permanently stuck in the auto-track retry queue - not
    because the underlying games couldn't be found (they resolved fine
    on their own), but because no parser recognized "at" as a matchup
    separator at all, so the picked team's name got mangled into "Packers
    at Broncos - Broncos" (the whole matchup swallowed as one literal,
    unfindable team name) before ever reaching the team lookup."""

    def test_at_separated_named_team_spread(self):
        pick = picks.parse_pick_line("[NFL] Packers at Broncos - Broncos -3.5 (Alt Spread) (Fanatics -130)")
        self.assertEqual(pick, {"kind": "team_total", "sport": "nfl", "team": "Broncos", "direction": "spread", "line": -3.5})

    def test_at_separated_named_team_spread_underdog(self):
        pick = picks.parse_pick_line("[NFL] Jets at Steelers - Steelers +1.5 (DraftKings -108)")
        self.assertEqual(pick["team"], "Steelers")
        self.assertEqual(pick["line"], 1.5)

    def test_at_separated_combined_total(self):
        pick = picks.parse_pick_line("[NFL] Panthers at Jaguars - Under 38.5 (Alt Total) (Bet365 -125)")
        self.assertEqual(pick, {"kind": "total", "sport": "nfl", "team": "Panthers", "direction": "under", "line": 38.5})

    def test_vs_separated_named_team_spread_also_fixed(self):
        # Same underlying gap as the "at" case above - no dedicated
        # matchup+named-team spread parser existed at all before, so this
        # was broken for "vs" too, just never confirmed live with that
        # wording specifically.
        pick = picks.parse_pick_line("[NFL] Packers vs Broncos - Broncos -3.5 (Alt Spread) (Fanatics -130)")
        self.assertEqual(pick["team"], "Broncos")
        self.assertEqual(pick["line"], -3.5)

    def test_named_team_not_matching_either_side_isnt_guessed_by_this_parser(self):
        # _parse_team_spread_matchup_pick itself correctly declines (named
        # team matches neither side) - but _parse_description's own
        # ultimate fallback for an unresolved bracket-tagged line
        # (_parse_team_pick's final "no cutword matched either, just
        # return whatever's left" case) is a separate, pre-existing gap
        # that predates this fix entirely, not something introduced here -
        # it still swallows the whole garbled string as a literal team
        # name rather than returning None. Documenting the current
        # (imperfect) behavior rather than asserting a stronger guarantee
        # this parser was never responsible for.
        pick = picks.parse_pick_line("[NFL] Packers at Broncos - Chiefs -3.5 (Fanatics -130)")
        self.assertNotEqual((pick or {}).get("kind"), "team_total")

    def test_win_a_set_at_least_wording_not_broken_by_the_at_separator_fix(self):
        # "to Win at Least 1 Set" contains " at " as an ordinary word, not
        # a matchup separator - confirmed live this was a real regression
        # risk: an earlier version of the "at" fix used a bare substring
        # check in several places, which falsely treated this line as
        # having a matchup and skipped the whole no-matchup parser block
        # entirely, silently misparsing it as a bare (nonsense) team name.
        msg = "Tennis\nStefanos Tsitsipas to Win at Least 1 Set (Alt Line) (Bet365 -275)"
        results = picks.parse_picks_message(msg)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "tennis_win_a_set")
        self.assertEqual(results[0]["team"], "Stefanos Tsitsipas")


class BasketballSpreadNoMatchup(unittest.TestCase):
    """Same no-opponent-named spread shape as NflSpreadNoMatchup above, but
    for WNBA/NBA - confirmed live, a real "Phoenix Mercury -2.5 (Alt
    Spread)" / "Indiana Fever +5.5 (Alt Spread)" pair under a WNBA header
    silently vanished with no botlog trace at all (not even a "not
    tracked" line) because the spread parser was scoped to sport == "nfl"
    only. _SPORT_MAP maps both "wnba" and "nba" to the same "basketball"
    sport key, so this covers both."""

    def test_wnba_header_spread_line(self):
        msg = "WNBA\nPhoenix Mercury -2.5 (Alt Spread) (Bet365 -190)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["kind"], "team_total")
        self.assertEqual(picked[0]["sport"], "basketball")
        self.assertEqual(picked[0]["team"], "Phoenix Mercury")
        self.assertEqual(picked[0]["direction"], "spread")
        self.assertEqual(picked[0]["line"], -2.5)

    def test_underdog_positive_spread(self):
        msg = "WNBA\nIndiana Fever +5.5 (Alt Spread) (Bet365 -170)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["direction"], "spread")
        self.assertEqual(picked[0]["line"], 5.5)

    def test_nba_header_also_covered(self):
        msg = "NBA\nBoston Celtics -6.5 (Fanatics -110)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["sport"], "basketball")

    def test_trailing_points_word_doesnt_swallow_the_spread(self):
        # Confirmed live: "Las Vegas Aces -1.5 Points" (no parenthetical,
        # unlike the Alt-Spread-wording tests above) silently fell all the
        # way through to the bare-team-name fallback, swallowing the whole
        # line - including the spread number - as one literal team name,
        # since the regex required the number to be the very last thing on
        # the line with nothing trailing it at all.
        msg = "WNBA\nLas Vegas Aces -1.5 Points\nChicago Sky +5 Points"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0], {
            "kind": "team_total", "sport": "basketball", "team": "Las Vegas Aces",
            "direction": "spread", "line": -1.5, "section": "WNBA", "raw": "Las Vegas Aces -1.5 Points",
        })
        self.assertEqual(picked[1]["team"], "Chicago Sky")
        self.assertEqual(picked[1]["line"], 5.0)

    def test_trailing_period_market_word_still_safely_unparsed(self):
        # "1st Half"/"Q1"/etc is a genuinely different market (a period
        # spread, not full-game) - must NOT be swallowed the same way
        # "Points" is, or a half-spread would silently misgrade as a
        # full-game one.
        self.assertIsNone(picks._parse_team_spread_nomatchup_pick("basketball", "Chicago Sky +5 1st Half"))


class HalftimeFulltime(unittest.TestCase):
    """Halftime/Fulltime (HT/FT) - a compound bet needing both legs to
    hit (see scores365.grade_ht_ft, htfttracker.py). Scoped to nfl/
    basketball only - the two sports quarters_breakdown is confirmed live
    to work for."""

    def test_same_team_both_legs_with_matchup_prefix(self):
        msg = "WNBA\nToronto Tempo vs Indiana Fever - Indiana Fever/Indiana Fever Halftime/Fulltime (Bet365 -110)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0], {
            "kind": "ht_ft", "sport": "basketball", "ht_team": "Indiana Fever", "ft_team": "Indiana Fever",
            "section": "WNBA", "raw": "Toronto Tempo vs Indiana Fever - Indiana Fever/Indiana Fever Halftime/Fulltime (Bet365 -110)",
        })

    def test_no_matchup_prefix_needed(self):
        msg = "WNBA\nIndiana Fever/Indiana Fever Halftime/Fulltime"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["kind"], "ht_ft")
        self.assertEqual(picked[0]["ht_team"], "Indiana Fever")
        self.assertEqual(picked[0]["ft_team"], "Indiana Fever")

    def test_different_ht_and_ft_teams(self):
        msg = "NFL\nBuffalo Bills vs Miami Dolphins - Buffalo Bills/Miami Dolphins Halftime/Fulltime"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["ht_team"], "Buffalo Bills")
        self.assertEqual(picked[0]["ft_team"], "Miami Dolphins")

    def test_ht_ft_abbreviation(self):
        msg = "NFL\nKansas City Chiefs/Kansas City Chiefs HT/FT"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["kind"], "ht_ft")

    def test_same_team_shorthand_with_ml_suffix(self):
        # Confirmed live: this exact wording silently misparsed as a plain
        # moneyline with "Indiana Fever Halftime/Fulltime" (the whole
        # phrase) as the team name, before _HT_FT_SAME_TEAM_RE existed.
        msg = "WNBA\nIndiana Fever Halftime/Fulltime ML"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0], {
            "kind": "ht_ft", "sport": "basketball", "ht_team": "Indiana Fever", "ft_team": "Indiana Fever",
            "section": "WNBA", "raw": "Indiana Fever Halftime/Fulltime ML",
        })

    def test_same_team_shorthand_without_ml_suffix(self):
        msg = "WNBA\nIndiana Fever Halftime/Fulltime"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["ht_team"], "Indiana Fever")
        self.assertEqual(picked[0]["ft_team"], "Indiana Fever")

    def test_same_team_shorthand_with_matchup_prefix(self):
        msg = "WNBA\nToronto Tempo vs Indiana Fever - Indiana Fever Halftime/Fulltime ML"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["ht_team"], "Indiana Fever")
        self.assertEqual(picked[0]["ft_team"], "Indiana Fever")

    def test_not_supported_outside_nfl_basketball(self):
        msg = "Tennis\nCarlos Alcaraz/Carlos Alcaraz Halftime/Fulltime"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(picked, [])


class KboPlayerProps(unittest.TestCase):
    """A KBO player prop used to parse with sport "baseball" - identical to
    a real MLB prop - which let ESPN's player search (MLB-only, no KBO
    league) silently match a former-MLB player's old MLB athlete record and
    "track" the pick against the wrong team/game entirely. Confirmed live:
    Austin Dean and Sam Hilliard (both KBO imports with MLB pasts) matched
    the San Francisco Giants and Colorado Rockies respectively. Tagging the
    sport "kbo" instead (still using baseball's own stat catalog to resolve
    the stat name) lets bot.py reject it honestly instead of mismatching."""

    def test_kbo_prop_tagged_distinctly_from_mlb(self):
        msg = "KBO\nGwak Been Over 4.5 Strikeouts (Alt Line) (Bet365 -200)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["kind"], "playerprops")
        self.assertEqual(picked[0]["sport"], "kbo")
        self.assertEqual(picked[0]["player"], "Gwak Been")
        self.assertEqual(picked[0]["stat"], "Strikeouts (Pitching)")

    def test_kbo_prop_stat_still_resolves_via_the_baseball_catalog(self):
        # The whole point: KBO has no catalog of its own on ESPN's side, but
        # the stat wording is identical to MLB's, so matching must still
        # succeed - a KBO prop failing to parse at all would be the same
        # silent-vanish bug this file exists to prevent, just for a
        # different reason.
        msg = "KBO\nAustin Dean Over 0.5 Total Bases (Alt Line) (Fanatics -225)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["stat"], "Total Bases")

    def test_mlb_prop_unaffected(self):
        msg = "MLB\nElly De La Cruz Over 1.5 Total Bases (Fanatics -150)"
        picked = picks.parse_picks_message(msg)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["sport"], "baseball")


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

    def test_1_plus_set_wording_with_no_matchup(self):
        # Confirmed live: "Frances Tiafoe to Win 1+ Set" - a third real
        # wording variant beyond "a set"/"at least 1 set(s)" - vanished
        # with no botlog trace for the same "contains a digit" fallback-
        # rejection reason as the "at least 1" case above.
        msg = "Tennis\nFrances Tiafoe to Win 1+ Set (-1.5 Sets) (FanDuel -105)"
        results = picks.parse_picks_message(msg)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "tennis_win_a_set")
        self.assertEqual(results[0]["team"], "Frances Tiafoe")
        self.assertEqual(results[0]["direction"], "yes")

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


class InningOneTotalRuns(unittest.TestCase):
    """"Over/Under N 1st Inning" at the 0.5 line is exactly YRFI/NRFI worded
    differently; any other line is the general 1st Inning Total Runs
    market (inningtracker.py's INNING1_TOTAL_OVER/UNDER)."""

    def test_half_point_line_still_routes_to_yrfi(self):
        pick = picks.parse_pick_line("[MLB] Philadelphia Phillies vs Miami Marlins - Over 0.5 1st Inning")
        self.assertEqual(pick, {"kind": "inning_runs", "sport": "baseball", "team": "Philadelphia Phillies", "pick_type": "YRFI"})

    def test_half_point_line_under_routes_to_nrfi(self):
        pick = picks.parse_pick_line("[MLB] Milwaukee Brewers vs Seattle Mariners - Under 0.5 1st Inning")
        self.assertEqual(pick, {"kind": "inning_runs", "sport": "baseball", "team": "Milwaukee Brewers", "pick_type": "NRFI"})

    def test_arbitrary_under_line_is_a_new_market(self):
        pick = picks.parse_pick_line("[MLB] Philadelphia Phillies vs Miami Marlins - Under 1.5 1st Inning")
        self.assertEqual(pick, {
            "kind": "inning_runs", "sport": "baseball", "team": "Philadelphia Phillies",
            "pick_type": "INNING1_TOTAL_UNDER", "line": 1.5,
        })

    def test_arbitrary_over_line_is_a_new_market(self):
        pick = picks.parse_pick_line("[MLB] Philadelphia Phillies vs Miami Marlins - Over 2.5 1st Inning")
        self.assertEqual(pick, {
            "kind": "inning_runs", "sport": "baseball", "team": "Philadelphia Phillies",
            "pick_type": "INNING1_TOTAL_OVER", "line": 2.5,
        })


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

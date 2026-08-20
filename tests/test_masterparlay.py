#!/usr/bin/env python3
"""
Regression tests for masterparlay.py - the "MASTER PARLAYS" slip parser
and its leg-resolution-against-dailylog logic. Network calls
(scores365.find_match_for_team/espn.find_player/espn.find_current_event_id)
are mocked with deterministic fake IDs; dailylog itself runs against a
temp file (same isolation pattern as test_dailylog.py) seeded directly
with the exact keys resolve_leg should compute.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dailylog
import f5tracker
import masterparlay
import proptracker
import state
import tracker


class ParseMasterParlays(unittest.TestCase):
    def test_real_message_parses_all_four_parlays(self):
        text = (
            "🎟️ MASTER PARLAYS\n"
            "🎟️ The Daily Double (+115)\n"
            "• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)\n"
            "• Leg 2: Atlanta Dream ML (-350 | 87% Conf)\n"
            "\n"
            "🎟️ The Triple Threat (+170)\n"
            "• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)\n"
            "• Leg 2: Las Vegas Aces - A'ja Wilson Over 21.5 Points (-240 | 90% Conf)\n"
            "• Leg 3: Las Vegas Aces ML (-1050 | 92% Conf)"
        )
        parlays = masterparlay.parse_master_parlays(text)
        self.assertEqual(len(parlays), 2)
        self.assertEqual(parlays[0]["name"], "The Daily Double")
        self.assertEqual(parlays[0]["odds"], "+115")
        self.assertEqual(parlays[0]["legs"], ["Tampa Bay Rays ML", "Atlanta Dream ML"])
        self.assertEqual(parlays[1]["name"], "The Triple Threat")
        self.assertEqual(len(parlays[1]["legs"]), 3)
        self.assertEqual(parlays[1]["legs"][1], "Las Vegas Aces - A'ja Wilson Over 21.5 Points")

    def test_banner_line_is_not_treated_as_its_own_parlay(self):
        text = "🎟️ MASTER PARLAYS\n🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)"
        parlays = masterparlay.parse_master_parlays(text)
        self.assertEqual(len(parlays), 1)
        self.assertEqual(parlays[0]["name"], "The Daily Double")

    def test_leg_lines_outside_any_parlay_header_are_ignored(self):
        text = "• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)\n🎟️ The Daily Double (+115)\n• Leg 1: Atlanta Dream ML (-350 | 87% Conf)"
        parlays = masterparlay.parse_master_parlays(text)
        self.assertEqual(len(parlays), 1)
        self.assertEqual(parlays[0]["legs"], ["Atlanta Dream ML"])


class GradeParlay(unittest.TestCase):
    def test_all_won_is_won(self):
        legs = [{"status": "won"}, {"status": "won"}]
        self.assertEqual(masterparlay.grade_parlay(legs), "won")

    def test_push_counts_toward_won(self):
        legs = [{"status": "won"}, {"status": "push"}]
        self.assertEqual(masterparlay.grade_parlay(legs), "won")

    def test_any_lost_leg_loses_the_whole_parlay(self):
        legs = [{"status": "won"}, {"status": "lost"}, {"status": "pending"}]
        self.assertEqual(masterparlay.grade_parlay(legs), "lost")

    def test_unresolved_leg_is_pending_not_lost(self):
        # The bot genuinely doesn't know this leg's outcome - guessing
        # "lost" would be actively wrong, not conservative.
        legs = [{"status": "won"}, {"status": "unresolved"}]
        self.assertEqual(masterparlay.grade_parlay(legs), "pending")

    def test_still_live_legs_are_pending(self):
        legs = [{"status": "won"}, {"status": "pending"}]
        self.assertEqual(masterparlay.grade_parlay(legs), "pending")


class ResolveLeg(unittest.TestCase):
    """Each test mocks the network lookup with a deterministic fake game/
    event/entity id, then seeds dailylog with the exact key resolve_leg
    should independently compute for that leg shape - if the key-building
    ever drifts from each tracker's own track_key/prop_key signature, the
    lookup silently misses and these fall back to "unresolved" instead of
    finding the seeded entry, catching the drift."""

    def setUp(self):
        self._real_path = state.DAILY_LOG_FILE
        fd, self._tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._tmp_path)
        state.DAILY_LOG_FILE = self._tmp_path

    def tearDown(self):
        state.DAILY_LOG_FILE = self._real_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def _seed(self, module: str, key: str, status: str, label: str, detail: str):
        data = state.load_daily_log()
        data[dailylog._key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, module, key)] = {
            "channel_id": masterparlay.PREMIUM_SCORES_CHANNEL_ID, "module": module,
            "status": status, "label": label, "detail": detail,
        }
        state.save_daily_log(data)

    def test_moneyline_leg_matches_a_seeded_win(self):
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        self._seed("tracker", key, "won", "Tampa Bay Rays ML", "WON")
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)):
            result = _run(masterparlay.resolve_leg("Tampa Bay Rays ML"))
        self.assertEqual(result["status"], "won")
        self.assertEqual(result["label"], "Tampa Bay Rays ML")

    def test_f5_moneyline_leg_matches_a_seeded_loss(self):
        key = f5tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614787, picked_team="Houston Astros")
        self._seed("f5tracker", key, "lost", "Houston Astros F5 ML", "LOST")
        with patch("scores365.find_match_for_team", return_value=({"id": 4614787}, 100)):
            result = _run(masterparlay.resolve_leg("Houston Astros F5 ML"))
        self.assertEqual(result["status"], "lost")

    def test_spread_leg_matches_a_seeded_pending(self):
        key = tracker.track_key(
            masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4659892, team_total="Indiana Fever", total_direction="spread", total_line=3.5,
        )
        self._seed("tracker", key, "pending", "Indiana Fever +3.5", "LIVE, Q3")
        with patch("scores365.find_match_for_team", return_value=({"id": 4659892}, 101)):
            result = _run(masterparlay.resolve_leg("Indiana Fever +3.5 (Alt Spread)"))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["detail"], "LIVE, Q3")

    def test_player_prop_leg_with_single_team_prefix_matches(self):
        stat_key = ("PTS", None)
        key = proptracker.prop_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, "401857159", "3149391", stat_key, "over", 21.5)
        self._seed("proptracker", key, "won", "A'ja Wilson Over 21.5 Points", "WON")
        fake_entity = {"id": "3149391", "team_id": "16"}
        with patch("espn.STAT_CATALOG", {"wnba": {"Points": stat_key}}), \
             patch("masterparlay.picks._match_stat_label", side_effect=lambda sport, raw: "Points" if sport == "wnba" and raw == "Points" else None), \
             patch("espn.find_player", return_value=fake_entity), \
             patch("espn.find_current_event_id", return_value="401857159"):
            result = _run(masterparlay.resolve_leg("Las Vegas Aces - A'ja Wilson Over 21.5 Points"))
        self.assertEqual(result["status"], "won")

    def test_unmatched_team_is_unresolved_not_a_guess(self):
        with patch("scores365.find_match_for_team", return_value=None):
            result = _run(masterparlay.resolve_leg("Nonexistent Team ML"))
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["detail"], "Not currently tracked")

    def test_resolved_game_but_no_matching_dailylog_entry_is_unresolved(self):
        # The team/game itself resolves fine, but this exact market was
        # never actually tracked - still "unresolved", not a crash or a
        # false "lost".
        with patch("scores365.find_match_for_team", return_value=({"id": 9999999}, 100)):
            result = _run(masterparlay.resolve_leg("Tampa Bay Rays ML"))
        self.assertEqual(result["status"], "unresolved")


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()

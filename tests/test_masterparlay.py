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

import datetime
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
import settracker
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


class ParseRecommendedFormat(unittest.TestCase):
    """GreenFox's wording drifted to a second slip shape with no named
    parlay/combo odds at all - confirmed live, a whole day's worth of
    these silently fell back to an older parseable message since nothing
    recognized them at all. Real message content (channel
    1538412823250608300, 2026-08-24)."""

    _REAL_MESSAGE = (
        "🎯 RECOMMENDED 1 GAME + 1 PROP DOUBLE LOCK\n"
        "Leg 1 (Game): [WNBA] Golden State Valkyries at Minnesota Lynx - Minnesota Lynx ML (FanDuel -100)\n"
        "Leg 2 (Prop): [WNBA Props] Rhyne Howard Over 14.5 Points (Alt Line) [Atlanta Dream at Los Angeles Sparks] (PrizePicks -205)\n"
        "\n"
        "🔒 RECOMMENDED 2-GAME OUTCOME DOUBLE\n"
        "Leg 1 (Game 1): [WNBA] Golden State Valkyries at Minnesota Lynx - Minnesota Lynx ML (FanDuel -100)\n"
        "Leg 2 (Game 2): [WNBA] Atlanta Dream at Los Angeles Sparks - Atlanta Dream ML (FanDuel -100)\n"
        "\n"
        "⚡ RECOMMENDED 1 GAME + 2 PROPS POWER TICKET\n"
        "Leg 1 (Game): [WNBA] Golden State Valkyries at Minnesota Lynx - Minnesota Lynx ML (FanDuel -100)\n"
        "Leg 2 (Prop 1): [WNBA Props] Rhyne Howard Over 14.5 Points (Alt Line) [Atlanta Dream at Los Angeles Sparks] (PrizePicks -205)\n"
        "Leg 3 (Prop 2): [MLB] Player - Under 9.5 Runs (Alt Line) [Philadelphia Phillies @ Seattle Mariners] (DraftKings -100)"
    )

    def test_real_message_parses_all_three_parlays_with_blank_odds(self):
        parlays = masterparlay.parse_master_parlays(self._REAL_MESSAGE)
        self.assertEqual(len(parlays), 3)
        self.assertEqual(parlays[0]["name"], "RECOMMENDED 1 GAME + 1 PROP DOUBLE LOCK")
        self.assertEqual(parlays[0]["odds"], "")
        self.assertEqual(parlays[1]["name"], "RECOMMENDED 2-GAME OUTCOME DOUBLE")
        self.assertEqual(parlays[2]["name"], "RECOMMENDED 1 GAME + 2 PROPS POWER TICKET")

    def test_game_leg_strips_matchup_prefix_leg_type_sport_tag_and_odds(self):
        parlays = masterparlay.parse_master_parlays(self._REAL_MESSAGE)
        self.assertEqual(parlays[0]["legs"][0], "Minnesota Lynx ML")

    def test_prop_leg_strips_bracketed_matchup_and_odds_but_keeps_alt_line(self):
        parlays = masterparlay.parse_master_parlays(self._REAL_MESSAGE)
        self.assertEqual(parlays[0]["legs"][1], "Rhyne Howard Over 14.5 Points (Alt Line)")

    def test_second_game_leg_also_strips_its_own_matchup_prefix(self):
        parlays = masterparlay.parse_master_parlays(self._REAL_MESSAGE)
        self.assertEqual(parlays[1]["legs"][1], "Atlanta Dream ML")

    def test_malformed_third_party_leg_is_manually_corrected(self):
        # GreenFox's own template left "Player" unfilled for what should
        # have been a team-total leg - no algorithm can recover the
        # intended teams from that, so it's corrected on sight via
        # _LEG_TEXT_CORRECTIONS instead (see that table's own docstring).
        parlays = masterparlay.parse_master_parlays(self._REAL_MESSAGE)
        self.assertEqual(parlays[2]["legs"][2], "Philadelphia Phillies vs Seattle Mariners - Under 9.5 Runs")

    def test_cleaned_legs_match_the_existing_resolver_regexes(self):
        # The whole point of the cleanup - resolve_leg's own regexes never
        # needed to change, only the format-specific text feeding into them.
        self.assertIsNotNone(masterparlay._ML_RE.match("Minnesota Lynx ML"))
        self.assertIsNotNone(masterparlay._PLAYER_PROP_RE.match("Rhyne Howard Over 14.5 Points (Alt Line)"))


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

    def test_full_word_moneyline_leg_also_matches(self):
        # Confirmed live: a real slip's "New York Yankees Moneyline"/
        # "Taylor Fritz Moneyline"/etc. legs all reported "Not currently
        # tracked" for picks that were genuinely already tracked, because
        # only the abbreviated "ML" trailer was recognized here (unlike
        # picks.py's own _parse_team_pick, which always accepted both).
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614781, picked_team="New York Yankees")
        self._seed("tracker", key, "pending", "New York Yankees ML", "NOT STARTED")
        with patch("scores365.find_match_for_team", return_value=({"id": 4614781}, 100)):
            result = _run(masterparlay.resolve_leg("New York Yankees Moneyline"))
        self.assertEqual(result["status"], "pending")

    def test_already_finished_game_still_resolves(self):
        # Confirmed live: a leg whose underlying game had already finished
        # (a real graded "lost" sitting in dailylog for it) reported "not
        # currently tracked" - the default fresh-pick lookup bounds
        # (allow_finished=False) can't find an already-decided game at
        # all, even though the pick was tracked and graded hours ago.
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        self._seed("tracker", key, "lost", "Tampa Bay Rays ML", "LOST")
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)) as mock_find:
            result = _run(masterparlay.resolve_leg("Tampa Bay Rays ML"))
        mock_find.assert_called_once_with("Tampa Bay Rays", None, 0, 1, True)
        self.assertEqual(result["status"], "lost")

    def test_dirty_dailylog_label_still_gets_cleaned_for_display(self):
        # Confirmed live: a pick tracked before picks.clean_label existed
        # still has the raw "(Bookmaker odds)" annotation baked into its
        # stored dailylog label - the report must not surface that as-is.
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        self._seed("tracker", key, "won", "Tampa Bay Rays ML (Bet365 -148)", "WON")
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)):
            result = _run(masterparlay.resolve_leg("Tampa Bay Rays ML"))
        self.assertEqual(result["label"], "Tampa Bay Rays ML")

    def test_unresolved_legs_own_text_is_also_cleaned(self):
        # _PARLAY_LEG_RE only strips the trailing "(odds | NN% Conf)" at
        # parse time - a leftover "(Alt Line)" on an otherwise-unresolved
        # leg still needs cleaning before it's shown as a label. Uses a
        # shape none of the three resolvers even attempt (no ML/spread/F5/
        # Over-Under wording) so this never touches the network.
        result = _run(masterparlay.resolve_leg("Some Unsupported Bet Type (Alt Line)"))
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["label"], "Some Unsupported Bet Type")

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

    def test_combined_game_total_leg_matches_a_seeded_pending(self):
        # Confirmed live: a manually-corrected leg ("Philadelphia Phillies
        # vs Seattle Mariners - Under 9.5 Runs" - see
        # masterparlay._LEG_TEXT_CORRECTIONS) reported "not currently
        # tracked" for a genuinely-tracked combined game total, because
        # no resolver here handled that market shape at all (only a
        # team's own total, via _resolve_ml_or_spread's spread path).
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4659920, total_direction="under", total_line=9.5)
        self._seed("tracker", key, "pending", "Under 9.5 Total Runs", "NOT STARTED")
        with patch("scores365.find_match_for_team", return_value=({"id": 4659920}, 100)) as mock_find:
            result = _run(masterparlay.resolve_leg("Philadelphia Phillies vs Seattle Mariners - Under 9.5 Runs"))
        mock_find.assert_called_once_with("Philadelphia Phillies", None, 0, 1, True)
        self.assertEqual(result["status"], "pending")

    def test_combined_game_total_leg_with_over_direction(self):
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4659921, total_direction="over", total_line=8.0)
        self._seed("tracker", key, "won", "Over 8.0 Total Runs", "WON")
        with patch("scores365.find_match_for_team", return_value=({"id": 4659921}, 100)):
            result = _run(masterparlay.resolve_leg("New York Yankees at Boston Red Sox - Over 8.0 Runs"))
        self.assertEqual(result["status"], "won")

    def test_games_handicap_leg_matches_a_seeded_win(self):
        key = settracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4819545, "games_handicap", "Sara Bejlek")
        self._seed("settracker", key, "won", "Sara Bejlek +5.5 Games", "WON")
        with patch("scores365.find_match_for_team", return_value=({"id": 4819545}, 103)) as mock_find:
            result = _run(masterparlay.resolve_leg("Sara Bejlek +5.5 Games"))
        mock_find.assert_called_once_with("Sara Bejlek", "tennis", 0, 1, True)
        self.assertEqual(result["status"], "won")

    def test_win_a_set_leg_matches_a_seeded_pending(self):
        key = settracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4819538, "win_a_set", "Frances Tiafoe")
        self._seed("settracker", key, "pending", "Frances Tiafoe to Win 1+ Set", "LIVE, Set 1")
        with patch("scores365.find_match_for_team", return_value=({"id": 4819538}, 103)):
            result = _run(masterparlay.resolve_leg("Frances Tiafoe to Win 1+ Set"))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["detail"], "LIVE, Set 1")

    def test_player_prop_leg_with_single_team_prefix_matches(self):
        stat_key = ("PTS", None)
        key = proptracker.prop_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, "401857159", "3149391", stat_key, "over", 21.5)
        self._seed("proptracker", key, "won", "A'ja Wilson Over 21.5 Points", "WON")
        fake_entity = {"id": "3149391", "team_id": "16"}
        with patch("espn.STAT_CATALOG", {"wnba": {"Points": stat_key}}), \
             patch("masterparlay.picks._match_stat_label", side_effect=lambda sport, raw: "Points" if sport == "wnba" and raw == "Points" else None), \
             patch("espn.find_player", return_value=fake_entity), \
             patch("espn.find_current_event_id", return_value="401857159") as mock_event:
            result = _run(masterparlay.resolve_leg("Las Vegas Aces - A'ja Wilson Over 21.5 Points"))
        self.assertEqual(result["status"], "won")
        # Same allow_finished=True reasoning as the team-based resolvers -
        # an already-finished game's prop must still be findable.
        mock_event.assert_called_once_with("wnba", "16", 0, 1, True)

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


class ResolveParlaysAndBuildReport(unittest.TestCase):
    """resolve_parlays backs /premiumparlay's per-parlay publish checklist
    (name/odds/outcome per parlay, before anything is published);
    build_report's only_names lets a user publish just the parlays they
    picked from a multi-parlay slip instead of the whole thing."""

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

    def _seed(self, module, key, status, label, detail):
        data = state.load_daily_log()
        data[dailylog._key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, module, key)] = {
            "channel_id": masterparlay.PREMIUM_SCORES_CHANNEL_ID, "module": module,
            "status": status, "label": label, "detail": detail,
        }
        state.save_daily_log(data)

    _TWO_PARLAY_TEXT = (
        "🎟️ The Daily Double (+115)\n"
        "• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)\n"
        "\n"
        "🎟️ The Triple Threat (+170)\n"
        "• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)"
    )

    def test_resolve_parlays_returns_name_odds_status_per_parlay(self):
        rays_key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        self._seed("tracker", rays_key, "won", "Tampa Bay Rays ML", "WON")
        astros_key = f5tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614787, picked_team="Houston Astros")
        self._seed("f5tracker", astros_key, "lost", "Houston Astros F5 ML", "LOST")
        with patch("scores365.find_match_for_team", side_effect=[({"id": 4614731}, 100), ({"id": 4614787}, 100)]):
            parlays = _run(masterparlay.resolve_parlays(self._TWO_PARLAY_TEXT))
        self.assertEqual([p["name"] for p in parlays], ["The Daily Double", "The Triple Threat"])
        self.assertEqual([p["odds"] for p in parlays], ["+115", "+170"])
        self.assertEqual([p["status"] for p in parlays], ["won", "lost"])

    def test_build_report_only_names_publishes_just_the_selected_parlay(self):
        rays_key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        self._seed("tracker", rays_key, "won", "Tampa Bay Rays ML", "WON")
        astros_key = f5tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614787, picked_team="Houston Astros")
        self._seed("f5tracker", astros_key, "lost", "Houston Astros F5 ML", "LOST")
        with patch("scores365.find_match_for_team", side_effect=[({"id": 4614731}, 100), ({"id": 4614787}, 100)]):
            embeds = _run(masterparlay.build_report(self._TWO_PARLAY_TEXT, only_names={"The Triple Threat"}))
        self.assertEqual(len(embeds), 1)
        self.assertIn("The Triple Threat", embeds[0].title)

    def test_build_report_omitting_only_names_publishes_everything(self):
        with patch("scores365.find_match_for_team", return_value=None):
            embeds = _run(masterparlay.build_report(self._TWO_PARLAY_TEXT))
        self.assertEqual(len(embeds), 2)


class _FakeAuthor:
    def __init__(self, author_id):
        self.id = author_id


class _FakeMessage:
    def __init__(self, id_, content, author_id, created_at, embeds=None):
        self.id = id_
        self.content = content
        self.author = _FakeAuthor(author_id)
        self.created_at = created_at
        self.embeds = embeds or []


class _FakeChannel:
    """after/before/oldest_first are honored the same way discord.py's
    real Messageable.history() does (including its own default-flipping
    quirk: passing after= alone without an explicit oldest_first flips
    the default to oldest-first) - _find_slip_in_window depends on this
    filtering, not just ordering."""

    def __init__(self, messages_oldest_first):
        self._messages = list(messages_oldest_first)
        self.sent: list[list] = []  # each entry is one send(embeds=...) call's embed list

    async def send(self, embeds=None, **kwargs):
        self.sent.append(embeds or [])

    async def history(self, limit=50, after=None, before=None, oldest_first=None):
        messages = self._messages
        if after is not None:
            messages = [m for m in messages if m.created_at.timestamp() > after.timestamp()]
        if before is not None:
            messages = [m for m in messages if m.created_at.timestamp() < before.timestamp()]
        if oldest_first is None:
            oldest_first = after is not None
        ordered = messages if oldest_first else list(reversed(messages))
        for message in ordered[:limit]:
            yield message


class FindLatestSlip(unittest.TestCase):
    """GreenFox posts a given parlay day's slate the night before -
    confirmed live, as early as 10:00 PM through 3:00 AM Eastern - so the
    live view shows whichever posting window (D-1's 3am cutoff through
    D's 3am cutoff) most recently closed, not whatever's accumulated
    since the last cutoff (GreenFox doesn't even start posting the next
    slate until 10pm regardless of how long ago the last cutoff was)."""

    # 2026-08-25 11:00:00 UTC = 2026-08-25 07:00:00 EDT - a normal
    # daytime check, well after that morning's 3am cutoff. The live
    # window this resolves to is everything posted from 2026-08-24
    # 07:00 UTC (2026-08-24 03:00 EDT) through 2026-08-25 07:00 UTC
    # (2026-08-25 03:00 EDT, inclusive) - GreenFox's real 10pm-3am
    # posting pattern for "the 25th's" slate.
    NOW = datetime.datetime(2026, 8, 25, 11, 0, 0, tzinfo=datetime.timezone.utc)

    def _find(self, channel):
        return _run(masterparlay.find_latest_slip(channel, now=self.NOW.timestamp()))

    def test_two_messages_close_together_same_author_combine(self):
        # 10:00:00pm and 10:00:20pm EDT the night before - well inside
        # the live window.
        older = _FakeMessage(1, "🎟️ MASTER PARLAYS 🎟️\n🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        newer = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 20, tzinfo=datetime.timezone.utc))
        channel = _FakeChannel([older, newer])
        run = self._find(channel)
        self.assertEqual([m.id for m in run], [1, 2])  # oldest first

    def test_combined_text_carries_both_messages_parlays(self):
        older = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        newer = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 20, tzinfo=datetime.timezone.utc))
        combined = masterparlay.combine_slip_text([older, newer])
        parlays = masterparlay.parse_master_parlays(combined)
        self.assertEqual([p["name"] for p in parlays], ["The Daily Double", "The Four-Fold Fortress"])

    def test_same_window_despite_a_big_gap_still_combines(self):
        # 10:00pm, 10:30pm, 11:00pm EDT the night before - a follow-up
        # slip 30 minutes later, still well inside the same posting
        # window, well before the 3am cutoff.
        first = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        second = _FakeMessage(2, "🎟️ The Triple Threat (+170)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 30, 0, tzinfo=datetime.timezone.utc))
        third = _FakeMessage(3, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Atlanta Dream ML (-350 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 3, 0, 0, tzinfo=datetime.timezone.utc))
        channel = _FakeChannel([first, second, third])
        run = self._find(channel)
        self.assertEqual([m.id for m in run], [1, 2, 3])

    def test_different_author_breaks_the_run(self):
        slip = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        other = _FakeMessage(2, "unrelated chat", 99, datetime.datetime(2026, 8, 25, 2, 10, 0, tzinfo=datetime.timezone.utc))
        newer_slip = _FakeMessage(3, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 20, 0, tzinfo=datetime.timezone.utc))
        channel = _FakeChannel([slip, other, newer_slip])
        run = self._find(channel)
        self.assertEqual([m.id for m in run], [3])

    def test_message_from_a_previous_parlay_day_is_excluded(self):
        # 2026-08-23 20:00 UTC = 2026-08-23 16:00 EDT - the previous
        # afternoon, well before the live window even opens
        # (2026-08-24 07:00 UTC).
        old_slip = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 23, 20, 0, 0, tzinfo=datetime.timezone.utc))
        new_slip = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        channel = _FakeChannel([old_slip, new_slip])
        run = self._find(channel)
        self.assertEqual([m.id for m in run], [2])

    def test_message_right_at_the_closing_cutoff_is_included(self):
        # 2026-08-25 07:00:00 UTC = 2026-08-25 03:00:00 EDT exactly - the
        # live window's own closing cutoff, inclusive.
        at_cutoff = datetime.datetime(2026, 8, 25, 7, 0, 0, tzinfo=datetime.timezone.utc)
        channel = _FakeChannel([_FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, at_cutoff)])
        run = self._find(channel)
        self.assertEqual([m.id for m in run], [1])

    def test_message_just_after_the_closing_cutoff_is_excluded(self):
        # 2026-08-25 07:00:01 UTC = 2026-08-25 03:00:01 EDT - one second
        # past the cutoff that closes the live window, belongs to the
        # *next* parlay day instead.
        after_cutoff = datetime.datetime(2026, 8, 25, 7, 0, 1, tzinfo=datetime.timezone.utc)
        channel = _FakeChannel([_FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, after_cutoff)])
        self.assertIsNone(self._find(channel))

    def test_no_slip_in_history_returns_none(self):
        channel = _FakeChannel([_FakeMessage(1, "just chatting", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))])
        self.assertIsNone(self._find(channel))

    def test_nothing_posted_in_the_live_window_returns_none_even_with_older_slip(self):
        # The old slip genuinely exists in the channel's history (a
        # previous parlay day's slate), but nothing's posted in the live
        # window yet - must not silently fall back to the older one
        # (that's the archive's job, via a date the user explicitly
        # picks).
        old_slip = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, datetime.datetime(2026, 8, 23, 20, 0, 0, tzinfo=datetime.timezone.utc))
        channel = _FakeChannel([old_slip])
        self.assertIsNone(self._find(channel))


class SlipDateStr(unittest.TestCase):
    """A slip's archived date always comes from when it was actually
    posted, looking FORWARD to the next 3am Eastern cutoff (GreenFox
    posts a day's slate the night before, 10pm-3am - confirmed live) -
    not whenever /premiumparlay happens to get run or published."""

    def test_evening_post_tags_the_upcoming_days_slate(self):
        # 2026-08-25 02:00 UTC = 2026-08-24 22:00 EDT (10pm) - the start
        # of GreenFox's usual posting window for "the 25th's" slate.
        posted = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(masterparlay.slip_date_str([posted]), "2026-08-25")

    def test_post_just_before_3am_cutoff_still_tags_that_mornings_date(self):
        # 2026-08-25 06:30 UTC = 2026-08-25 02:30 EDT - already past
        # midnight locally, but still before that morning's 3am cutoff,
        # so it's still "the 25th's" slate, not the 26th's.
        posted = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 25, 6, 30, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(masterparlay.slip_date_str([posted]), "2026-08-25")

    def test_uses_earliest_message_regardless_of_list_order(self):
        early = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 25, 2, 0, 0, tzinfo=datetime.timezone.utc))  # 10pm EDT the 24th
        late = _FakeMessage(2, "b", 42, datetime.datetime(2026, 8, 25, 6, 30, 0, tzinfo=datetime.timezone.utc))  # 2:30am EDT the 25th
        self.assertEqual(masterparlay.slip_date_str([late, early]), "2026-08-25")

    def test_post_right_at_the_cutoff_still_belongs_to_that_mornings_date(self):
        posted = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 25, 7, 0, 0, tzinfo=datetime.timezone.utc))  # exactly 3am EDT
        self.assertEqual(masterparlay.slip_date_str([posted]), "2026-08-25")

    def test_post_just_after_the_cutoff_tags_the_next_days_slate(self):
        posted = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 25, 7, 0, 1, tzinfo=datetime.timezone.utc))  # 3:00:01am EDT
        self.assertEqual(masterparlay.slip_date_str([posted]), "2026-08-26")


class ParlayDayWindow(unittest.TestCase):
    """previous_parlay_day_str/seconds_until_next_parlay_day_cutoff back
    the nightly auto-archive loop - it needs to know both which day just
    closed and exactly how long to sleep until the next one does."""

    def test_previous_parlay_day_before_cutoff_is_two_days_back(self):
        # 2026-08-20 05:00 UTC = 2026-08-20 01:00 EDT - before that day's
        # 3am cutoff, so "now" is still in the 19th's window, and the
        # previous (already-closed) window is the 18th's.
        now = datetime.datetime(2026, 8, 20, 5, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(masterparlay.previous_parlay_day_str(now.timestamp()), "2026-08-18")

    def test_previous_parlay_day_after_cutoff_is_yesterday(self):
        # 2026-08-20 16:39 UTC = 2026-08-20 12:39 EDT - well past that
        # day's 3am cutoff, so "now" is in the 20th's window and the
        # previous one is the 19th's.
        now = datetime.datetime(2026, 8, 20, 16, 39, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(masterparlay.previous_parlay_day_str(now.timestamp()), "2026-08-19")

    def test_seconds_until_next_cutoff_matches_a_known_gap(self):
        # 2026-08-20 07:00 UTC = 2026-08-20 03:00 EDT exactly - the next
        # cutoff is exactly 24h away (no DST edge in this window).
        now = datetime.datetime(2026, 8, 20, 7, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertAlmostEqual(masterparlay.seconds_until_next_parlay_day_cutoff(now.timestamp()), 24 * 3600, delta=1)

    def test_seconds_until_next_cutoff_is_never_negative_right_after_a_cutoff(self):
        now = datetime.datetime(2026, 8, 20, 7, 0, 1, tzinfo=datetime.timezone.utc)
        self.assertGreater(masterparlay.seconds_until_next_parlay_day_cutoff(now.timestamp()), 0)


class ArchiveFooterTagging(unittest.TestCase):
    def test_build_parlay_embed_tags_footer_with_date(self):
        legs = [{"raw": "x", "status": "won", "label": "x", "detail": "Won"}]
        embed = masterparlay.build_parlay_embed("The Daily Double", "+115", legs, date_str="2026-08-20")
        self.assertEqual(masterparlay.archived_date_from_embed(embed), "2026-08-20")

    def test_no_date_str_leaves_footer_untagged(self):
        legs = [{"raw": "x", "status": "won", "label": "x", "detail": "Won"}]
        embed = masterparlay.build_parlay_embed("The Daily Double", "+115", legs)
        self.assertIsNone(masterparlay.archived_date_from_embed(embed))

    def test_unrelated_footer_text_is_not_mistaken_for_a_date_tag(self):
        import discord
        embed = discord.Embed(title="unrelated")
        embed.set_footer(text="Some other footer")
        self.assertIsNone(masterparlay.archived_date_from_embed(embed))

    def test_blank_odds_title_has_no_dangling_parens(self):
        # A "RECOMMENDED ..." parlay never states its own odds - the
        # title must read as just the name, not "name ()".
        legs = [{"raw": "x", "status": "won", "label": "x", "detail": "Won"}]
        embed = masterparlay.build_parlay_embed("RECOMMENDED 1 GAME + 1 PROP DOUBLE LOCK", "", legs)
        self.assertTrue(embed.title.startswith("RECOMMENDED 1 GAME + 1 PROP DOUBLE LOCK —"))
        self.assertNotIn("()", embed.title)


class FindArchivedDatesAndReport(unittest.TestCase):
    def _tagged_message(self, id_, date_str, count=1):
        legs = [{"raw": "x", "status": "won", "label": f"leg{id_}", "detail": "Won"}]
        embeds = [masterparlay.build_parlay_embed(f"Parlay {id_}-{i}", "+115", legs, date_str=date_str) for i in range(count)]
        return _FakeMessage(id_, "archived report", 42, datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc), embeds=embeds)

    def test_find_archived_dates_returns_distinct_dates_most_recent_first(self):
        oldest = self._tagged_message(1, "2026-08-18")
        middle = self._tagged_message(2, "2026-08-19")
        newest = self._tagged_message(3, "2026-08-20")
        channel = _FakeChannel([oldest, middle, newest])
        dates = _run(masterparlay.find_archived_dates(channel))
        self.assertEqual(dates, ["2026-08-20", "2026-08-19", "2026-08-18"])

    def test_find_archived_report_returns_only_matching_date_oldest_first(self):
        first = self._tagged_message(1, "2026-08-20")
        other_day = self._tagged_message(2, "2026-08-19")
        second = self._tagged_message(3, "2026-08-20")
        channel = _FakeChannel([first, other_day, second])
        embeds = _run(masterparlay.find_archived_report(channel, "2026-08-20"))
        self.assertEqual([e.title.split(" (")[0] for e in embeds], ["Parlay 1-0", "Parlay 3-0"])

    def test_find_archived_report_handles_multiple_embeds_per_message(self):
        message = self._tagged_message(1, "2026-08-20", count=3)
        channel = _FakeChannel([message])
        embeds = _run(masterparlay.find_archived_report(channel, "2026-08-20"))
        self.assertEqual(len(embeds), 3)

    def test_find_archived_report_no_match_returns_empty_list(self):
        channel = _FakeChannel([self._tagged_message(1, "2026-08-20")])
        embeds = _run(masterparlay.find_archived_report(channel, "2099-01-01"))
        self.assertEqual(embeds, [])


class AutoArchive(unittest.TestCase):
    """The nightly auto-archive job (see bot.py's
    _masterparlay_auto_archive_loop) - archives a full day's slip with no
    curation (nobody's around at 3am to click checkboxes), but only once,
    never duplicating over a manual Publish that already covered the same
    date."""

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

    def _seed_rays_win(self):
        key = tracker.track_key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, 4614731, picked_team="Tampa Bay Rays")
        data = state.load_daily_log()
        data[dailylog._key(masterparlay.PREMIUM_SCORES_CHANNEL_ID, "tracker", key)] = {
            "channel_id": masterparlay.PREMIUM_SCORES_CHANNEL_ID, "module": "tracker",
            "status": "won", "label": "Tampa Bay Rays ML", "detail": "WON",
        }
        state.save_daily_log(data)

    def _slip_message_for_2026_08_20(self):
        # 2026-08-20 02:00 UTC = 2026-08-19 22:00 EDT (10pm) - GreenFox's
        # real posting pattern for "the 20th's" slate, inside the window
        # (2026-08-19 07:00 UTC, 2026-08-20 07:00 UTC] that
        # _find_slip_for_parlay_day looks at for date_str "2026-08-20".
        posted = datetime.datetime(2026, 8, 20, 2, 0, 0, tzinfo=datetime.timezone.utc)
        return _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, posted)

    def test_auto_archive_parlay_day_publishes_and_returns_count(self):
        self._seed_rays_win()
        slip_channel = _FakeChannel([self._slip_message_for_2026_08_20()])
        archive_channel = _FakeChannel([])
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)):
            count = _run(masterparlay.auto_archive_parlay_day(slip_channel, archive_channel, "2026-08-20"))
        self.assertEqual(count, 1)
        self.assertEqual(len(archive_channel.sent), 1)
        self.assertEqual(len(archive_channel.sent[0]), 1)
        self.assertEqual(masterparlay.archived_date_from_embed(archive_channel.sent[0][0]), "2026-08-20")

    def test_auto_archive_parlay_day_returns_none_when_no_slip_for_that_date(self):
        slip_channel = _FakeChannel([])
        archive_channel = _FakeChannel([])
        result = _run(masterparlay.auto_archive_parlay_day(slip_channel, archive_channel, "2026-08-20"))
        self.assertIsNone(result)
        self.assertEqual(archive_channel.sent, [])

    def test_auto_archive_if_needed_skips_when_already_archived(self):
        self._seed_rays_win()
        slip_channel = _FakeChannel([self._slip_message_for_2026_08_20()])
        legs = [{"raw": "x", "status": "won", "label": "x", "detail": "Won"}]
        already_archived = masterparlay.build_parlay_embed("Manual Publish", "+100", legs, date_str="2026-08-20")
        archive_channel = _FakeChannel([_FakeMessage(2, "", 7, datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc), embeds=[already_archived])])
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)):
            result = _run(masterparlay.auto_archive_if_needed(slip_channel, archive_channel, "2026-08-20"))
        self.assertIsNone(result)
        self.assertEqual(archive_channel.sent, [])  # nothing new sent - skipped

    def test_auto_archive_if_needed_archives_when_nothing_published_yet(self):
        self._seed_rays_win()
        slip_channel = _FakeChannel([self._slip_message_for_2026_08_20()])
        archive_channel = _FakeChannel([])
        with patch("scores365.find_match_for_team", return_value=({"id": 4614731}, 100)):
            result = _run(masterparlay.auto_archive_if_needed(slip_channel, archive_channel, "2026-08-20"))
        self.assertEqual(result, 1)
        self.assertEqual(len(archive_channel.sent), 1)


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()

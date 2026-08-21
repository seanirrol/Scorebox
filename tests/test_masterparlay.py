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
    """Newest-first history, matching discord.py's own ordering - the
    thing find_latest_slip actually depends on."""

    def __init__(self, messages_oldest_first):
        self._messages = list(reversed(messages_oldest_first))

    async def history(self, limit=50):
        for message in self._messages[:limit]:
            yield message


class FindLatestSlip(unittest.TestCase):
    """GreenFox's real slips split across multiple messages, sometimes a
    few seconds apart (Discord's own per-message length cap - confirmed
    live, a real 4-parlay slip landed as two messages 20 seconds apart),
    sometimes tens of minutes apart (a follow-up slip posted later the
    same day) - grouping is per Eastern calendar date (matching
    dailylog's own date convention), not proximity, so everything posted
    on the same day combines into one slip regardless of the gap between
    messages, and a new day's slip never merges with the previous one's."""

    def _t(self, seconds_ago):
        import datetime
        # 2026-08-20 20:39:19 UTC = 2026-08-20 16:39:19 EDT - comfortably
        # mid-day Eastern, nowhere near a midnight boundary.
        base = datetime.datetime(2026, 8, 20, 20, 39, 19, tzinfo=datetime.timezone.utc)
        return base - datetime.timedelta(seconds=seconds_ago)

    def test_two_messages_close_together_same_author_combine(self):
        older = _FakeMessage(1, "🎟️ MASTER PARLAYS 🎟️\n🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, self._t(20))
        newer = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, self._t(0))
        channel = _FakeChannel([older, newer])
        run = _run(masterparlay.find_latest_slip(channel))
        self.assertEqual([m.id for m in run], [1, 2])  # oldest first

    def test_combined_text_carries_both_messages_parlays(self):
        older = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, self._t(20))
        newer = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, self._t(0))
        combined = masterparlay.combine_slip_text([older, newer])
        parlays = masterparlay.parse_master_parlays(combined)
        self.assertEqual([p["name"] for p in parlays], ["The Daily Double", "The Four-Fold Fortress"])

    def test_same_day_despite_a_big_gap_still_combines(self):
        # A follow-up slip posted 30 minutes after the first two, same
        # Eastern day - per explicit request, this now combines instead
        # of being treated as a separate, unreachable-once-superseded slip.
        first = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, self._t(35 * 60))
        second = _FakeMessage(2, "🎟️ The Triple Threat (+170)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, self._t(34 * 60))
        third = _FakeMessage(3, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Atlanta Dream ML (-350 | 87% Conf)", 42, self._t(0))
        channel = _FakeChannel([first, second, third])
        run = _run(masterparlay.find_latest_slip(channel))
        self.assertEqual([m.id for m in run], [1, 2, 3])

    def test_different_author_breaks_the_run(self):
        slip = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, self._t(20))
        other = _FakeMessage(2, "unrelated chat", 99, self._t(10))
        newer_slip = _FakeMessage(3, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, self._t(0))
        channel = _FakeChannel([slip, other, newer_slip])
        run = _run(masterparlay.find_latest_slip(channel))
        self.assertEqual([m.id for m in run], [3])

    def test_different_calendar_day_breaks_the_run(self):
        old_slip = _FakeMessage(1, "🎟️ The Daily Double (+115)\n• Leg 1: Tampa Bay Rays ML (-150 | 88% Conf)", 42, self._t(24 * 3600))
        new_slip = _FakeMessage(2, "🎟️ The Four-Fold Fortress (+345)\n• Leg 1: Houston Astros F5 ML (-135 | 87% Conf)", 42, self._t(0))
        channel = _FakeChannel([old_slip, new_slip])
        run = _run(masterparlay.find_latest_slip(channel))
        self.assertEqual([m.id for m in run], [2])

    def test_no_slip_in_history_returns_none(self):
        channel = _FakeChannel([_FakeMessage(1, "just chatting", 42, self._t(0))])
        self.assertIsNone(_run(masterparlay.find_latest_slip(channel)))


class SlipDateStr(unittest.TestCase):
    """The archived date always comes from when the slip was actually
    posted, not whenever /premiumparlay happens to get run or published -
    confirmed as the whole point of the archive redesign, so a slip
    posted late at night still files under the day it was posted even if
    the last leg (or the Publish click) lands after midnight."""

    def test_uses_earliest_message_regardless_of_list_order(self):
        import datetime
        early = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 20, 16, 0, 0, tzinfo=datetime.timezone.utc))
        late = _FakeMessage(2, "b", 42, datetime.datetime(2026, 8, 21, 2, 0, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(masterparlay.slip_date_str([late, early]), "2026-08-20")

    def test_late_night_post_that_crosses_midnight_utc_stays_eastern_day(self):
        import datetime
        # 2026-08-21 02:00 UTC = 2026-08-20 22:00 EDT - still the 20th
        # locally even though it's already the 21st in UTC.
        posted = _FakeMessage(1, "a", 42, datetime.datetime(2026, 8, 21, 2, 0, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(masterparlay.slip_date_str([posted]), "2026-08-20")


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


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()

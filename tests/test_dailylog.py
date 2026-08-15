#!/usr/bin/env python3
"""
Regression tests for dailylog.py - the data behind /summary and the win
rate line. Explicitly prioritized (per the user): this and test_summary_
embeds.py are the two files most directly tied to /summary and win-rate
correctness, which matter more than tracker mechanics.

Each test runs against a temporary daily_log_state.json (never the real
one) via setUp/tearDown patching state.DAILY_LOG_FILE - safe to run
against a live deployment without touching production data.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dailylog
import state


class DailyLogTestCase(unittest.TestCase):
    """Base class that isolates every test from the real daily_log_state.json
    and winlossgraph_overrides_state.json."""

    def setUp(self):
        self._real_path = state.DAILY_LOG_FILE
        self._real_overrides_path = state.WINLOSSGRAPH_OVERRIDES_FILE
        fd, self._tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._tmp_path)  # _load() treats a missing file as {}
        state.DAILY_LOG_FILE = self._tmp_path
        fd2, self._tmp_overrides_path = tempfile.mkstemp(suffix=".json")
        os.close(fd2)
        os.remove(self._tmp_overrides_path)
        state.WINLOSSGRAPH_OVERRIDES_FILE = self._tmp_overrides_path

    def tearDown(self):
        state.DAILY_LOG_FILE = self._real_path
        state.WINLOSSGRAPH_OVERRIDES_FILE = self._real_overrides_path
        for path in (self._tmp_path, self._tmp_overrides_path):
            if os.path.exists(path):
                os.remove(path)


class RecordPickAndResult(DailyLogTestCase):
    def test_record_pick_then_won_result(self):
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2, sport="MLB")
        dailylog.record_result(1, "tracker", "k1", "won")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "won")
        self.assertEqual(entries[0]["detail"], "WON")

    def test_record_pick_with_no_section_is_a_no_op(self):
        # Manual /track usage (no section) deliberately never makes it
        # into the daily report - dailylog.record_pick's own contract.
        dailylog.record_pick(1, "tracker", "k1", None, "Team A ML", message_id=100, origin_channel_id=2)
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries, [])

    def test_void_without_reason_is_a_bare_mark(self):
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.record_result(1, "tracker", "k1", "void")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["detail"], "VOID")

    def test_void_with_reason_is_tagged(self):
        # Confirmed live: a bare "Voided" with no explanation was
        # confusing once several different give-up paths all collapsed
        # into the same mark - every void must carry its actual reason.
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.record_result(1, "tracker", "k1", "void", "Game not found on 365scores")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["detail"], "VOID - Game not found on 365scores")

    def test_reason_ignored_for_non_void_status(self):
        # A reason passed for won/lost/push is always self-explanatory
        # from the result mark alone - not something a caller should be
        # doing, but it must never leak into the detail text if it happens.
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.record_result(1, "tracker", "k1", "won", "irrelevant")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["detail"], "WON")

    def test_unknown_status_is_rejected(self):
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.record_result(1, "tracker", "k1", "not-a-real-status")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["status"], "pending")  # untouched

    def test_touch_updates_detail_while_pending(self):
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.touch(1, "tracker", "k1", "LIVE, Top 3rd 2-1")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["detail"], "LIVE, Top 3rd 2-1")

    def test_touch_never_overwrites_a_final_result(self):
        # A final result always wins over a racing touch() call.
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Team A ML", message_id=100, origin_channel_id=2)
        dailylog.record_result(1, "tracker", "k1", "won")
        dailylog.touch(1, "tracker", "k1", "LIVE, Top 9th")
        entries = dailylog.picks_for_date([2], dailylog.today_str())
        self.assertEqual(entries[0]["detail"], "WON")


class Forget(DailyLogTestCase):
    """dailylog.forget() - removes a placeholder entry once a real one
    takes over (e.g. pendingsoccerprops' "still queued" placeholder)."""

    def test_forget_removes_the_entry(self):
        dailylog.record_pick(1, "pendingsoccerprops", "k1", "Soccer", "Player X Over 0.5 Shots", message_id=0, origin_channel_id=2)
        self.assertEqual(len(dailylog.picks_for_date([2], dailylog.today_str())), 1)
        dailylog.forget(1, "pendingsoccerprops", "k1")
        self.assertEqual(dailylog.picks_for_date([2], dailylog.today_str()), [])

    def test_forget_a_nonexistent_key_does_not_raise(self):
        dailylog.forget(999, "nonexistent", "nope")  # must not raise


class WinRateAggregation(DailyLogTestCase):
    """daily_win_loss backs /winlossgraph - the exact same "decided only"
    rule /summary's own win-rate line uses (push/void/pending excluded
    from both the numerator and denominator)."""

    def _log(self, channel_id, key, status, date_str, origin=2):
        dailylog.record_pick(channel_id, "tracker", key, "MLB", f"Pick {key}", message_id=1, origin_channel_id=origin)
        entry_key = f"{channel_id}:tracker:{key}"
        data = state.load_daily_log()
        data[entry_key]["date"] = date_str
        data[entry_key]["status"] = status
        state.save_daily_log(data)

    def test_push_and_void_excluded_from_both_counts(self):
        self._log(1, "k1", "won", "2026-08-13")
        self._log(1, "k2", "lost", "2026-08-13")
        self._log(1, "k3", "push", "2026-08-13")
        self._log(1, "k4", "void", "2026-08-13")
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual(rows, [("2026-08-13", 1, 1)])

    def test_pending_picks_excluded_entirely(self):
        self._log(1, "k1", "won", "2026-08-13")
        dailylog.record_pick(1, "tracker", "k2", "MLB", "Still pending", message_id=1, origin_channel_id=2)
        data = state.load_daily_log()
        data["1:tracker:k2"]["date"] = "2026-08-13"
        state.save_daily_log(data)
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual(rows, [("2026-08-13", 1, 0)])

    def test_a_day_with_only_push_or_void_has_no_row_at_all(self):
        self._log(1, "k1", "push", "2026-08-13")
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual(rows, [])

    def test_multiple_days_sorted_oldest_first(self):
        self._log(1, "k1", "won", "2026-08-13")
        self._log(1, "k2", "won", "2026-08-11")
        self._log(1, "k3", "lost", "2026-08-12")
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual([r[0] for r in rows], ["2026-08-11", "2026-08-12", "2026-08-13"])

    def test_different_origin_channels_are_isolated(self):
        self._log(1, "k1", "won", "2026-08-13", origin=2)
        self._log(1, "k2", "lost", "2026-08-13", origin=3)
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 1, 0)])
        self.assertEqual(dailylog.daily_win_loss([3], "2026-08"), [("2026-08-13", 0, 1)])
        self.assertEqual(dailylog.daily_win_loss([2, 3], "2026-08"), [("2026-08-13", 1, 1)])

    def test_reliable_from_floor_excludes_earlier_days(self):
        # August 2026 specifically has a floor at Aug 10 - days before it
        # are known-unreliable (see dailylog._RELIABLE_FROM) and must
        # never appear in the chart, even though they're otherwise
        # perfectly valid decided picks.
        self._log(1, "k1", "won", "2026-08-05")
        self._log(1, "k2", "won", "2026-08-10")
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual(rows, [("2026-08-10", 1, 0)])

    def test_months_without_a_floor_are_unaffected(self):
        self._log(1, "k1", "won", "2026-09-01")
        rows = dailylog.daily_win_loss([2], "2026-09")
        self.assertEqual(rows, [("2026-09-01", 1, 0)])


class WinLossGraphOverrides(DailyLogTestCase):
    """set_winlossgraph_override lets /winlossgraph show custom counts for
    a date without touching any individual pick's dailylog entry - real
    computed rows still come through unaffected for every date except the
    overridden one."""

    def _log(self, channel_id, key, status, date_str, origin=2):
        dailylog.record_pick(channel_id, "tracker", key, "MLB", f"Pick {key}", message_id=1, origin_channel_id=origin)
        entry_key = f"{channel_id}:tracker:{key}"
        data = state.load_daily_log()
        data[entry_key]["date"] = date_str
        data[entry_key]["status"] = status
        state.save_daily_log(data)

    def test_override_replaces_the_real_computed_row(self):
        self._log(1, "k1", "won", "2026-08-13")
        self._log(1, "k2", "lost", "2026-08-13")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 1, 1)])
        dailylog.set_winlossgraph_override([2], "2026-08-13", 26, 14)
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 26, 14)])

    def test_override_does_not_affect_other_dates(self):
        self._log(1, "k1", "won", "2026-08-12")
        dailylog.set_winlossgraph_override([2], "2026-08-13", 26, 14)
        rows = dailylog.daily_win_loss([2], "2026-08")
        self.assertEqual(rows, [("2026-08-12", 1, 0), ("2026-08-13", 26, 14)])

    def test_override_is_scoped_to_its_own_origin_route(self):
        # premium (origin {2}) and free (origin {3, 4}) must never leak
        # into each other's overrides.
        dailylog.set_winlossgraph_override([2], "2026-08-13", 26, 14)
        dailylog.set_winlossgraph_override([3, 4], "2026-08-13", 19, 10)
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 26, 14)])
        self.assertEqual(dailylog.daily_win_loss([3, 4], "2026-08"), [("2026-08-13", 19, 10)])

    def test_clear_override_restores_the_real_computed_row(self):
        self._log(1, "k1", "won", "2026-08-13")
        dailylog.set_winlossgraph_override([2], "2026-08-13", 26, 14)
        dailylog.clear_winlossgraph_override([2], "2026-08-13")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 1, 0)])

    def test_hide_drops_the_date_entirely_even_with_real_decided_picks(self):
        self._log(1, "k1", "won", "2026-08-15")
        self._log(1, "k2", "lost", "2026-08-15")
        dailylog.hide_winlossgraph_date([2], "2026-08-15")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [])

    def test_hide_does_not_affect_other_dates(self):
        self._log(1, "k1", "won", "2026-08-13")
        self._log(1, "k2", "won", "2026-08-15")
        dailylog.hide_winlossgraph_date([2], "2026-08-15")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-13", 1, 0)])

    def test_hide_is_scoped_to_its_own_origin_route(self):
        self._log(1, "k1", "won", "2026-08-15", origin=2)
        self._log(2, "k2", "lost", "2026-08-15", origin=3)
        dailylog.hide_winlossgraph_date([2], "2026-08-15")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [])
        self.assertEqual(dailylog.daily_win_loss([3], "2026-08"), [("2026-08-15", 0, 1)])

    def test_clearing_a_hide_reveals_whatever_has_been_decided_since(self):
        dailylog.hide_winlossgraph_date([2], "2026-08-15")
        self._log(1, "k1", "won", "2026-08-15")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [])
        dailylog.clear_winlossgraph_override([2], "2026-08-15")
        self.assertEqual(dailylog.daily_win_loss([2], "2026-08"), [("2026-08-15", 1, 0)])


class AvailableDatesAndMonths(DailyLogTestCase):
    def test_available_months_only_counts_decided_picks(self):
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Pick", message_id=1, origin_channel_id=2)
        data = state.load_daily_log()
        data["1:tracker:k1"]["date"] = "2026-08-13"
        state.save_daily_log(data)
        # still pending - no decided pick yet, month shouldn't appear
        self.assertEqual(dailylog.available_months([2]), [])
        dailylog.record_result(1, "tracker", "k1", "won")
        self.assertEqual(dailylog.available_months([2]), ["2026-08"])

    def test_available_dates_includes_pending_picks(self):
        # Unlike available_months, available_dates is used by /summary's
        # own preview picker, which should show a date even if nothing on
        # it has resolved yet.
        dailylog.record_pick(1, "tracker", "k1", "MLB", "Pick", message_id=1, origin_channel_id=2)
        data = state.load_daily_log()
        data["1:tracker:k1"]["date"] = "2026-08-13"
        state.save_daily_log(data)
        self.assertEqual(dailylog.available_dates([2]), ["2026-08-13"])


if __name__ == "__main__":
    unittest.main()

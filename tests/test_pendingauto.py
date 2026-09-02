#!/usr/bin/env python3
"""
Regression tests for pendingauto.py - the generic queue-and-retry module
extending pendingtrack.py's "match not published yet, retry later" pattern
to every other auto-track pick kind (F5, 1H, tennis extra-markets/props,
YRFI/NRFI, 1st-inning-result, ESPN player props).

Run with: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pendingauto
import state


class PendingAutoTestCase(unittest.TestCase):
    def setUp(self):
        self._real_path = state.PENDING_AUTO_FILE
        fd, self._tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._tmp_path)
        state.PENDING_AUTO_FILE = self._tmp_path

    def tearDown(self):
        state.PENDING_AUTO_FILE = self._real_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def _queue(self, kind, payload, resolve=None):
        # pendingauto.queue() itself calls asyncio.create_task, which needs
        # a running event loop - wraps the call in one for every test below
        # rather than requiring each test to do it individually.
        resolve = resolve or (lambda p: asyncio.sleep(0, result=False))

        async def do():
            return pendingauto.queue(kind, payload, resolve)

        return self._run(do())


class IsQueued(PendingAutoTestCase):
    def test_matching_kind_and_payload_is_queued(self):
        payload = {"channel_id": 1, "team": "Denver Broncos"}
        self._queue("f5", payload)
        self.assertTrue(pendingauto.is_queued("f5", payload))

    def test_different_kind_same_payload_not_queued(self):
        payload = {"channel_id": 1, "team": "Denver Broncos"}
        self._queue("f5", payload)
        self.assertFalse(pendingauto.is_queued("1h", payload))

    def test_different_payload_same_kind_not_queued(self):
        self._queue("f5", {"channel_id": 1, "team": "Denver Broncos"})
        self.assertFalse(pendingauto.is_queued("f5", {"channel_id": 1, "team": "Buffalo Bills"}))

    def test_nothing_queued_returns_false(self):
        self.assertFalse(pendingauto.is_queued("f5", {"channel_id": 1, "team": "Denver Broncos"}))


class Queue(PendingAutoTestCase):
    def test_queue_persists_kind_and_payload(self):
        payload = {"channel_id": 1, "player": "Fernando Tatis Jr."}
        self._queue("playerprops", payload)
        entries = pendingauto.list_pending()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "playerprops")
        self.assertEqual(entries[0]["payload"], payload)

    def test_retry_loop_forgets_entry_once_resolve_returns_true(self):
        # Calls _retry_loop directly (rather than through queue()'s
        # background task) so this can be awaited deterministically instead
        # of guessing at real-time scheduling - RETRY_INTERVAL_SECONDS is
        # patched down so the loop's first sleep resolves almost instantly.
        entry_id, entry = "e1", {"kind": "f5", "payload": {"team": "Denver Broncos"}, "queued_at": time.time()}
        pendingauto._persist(entry_id, entry)
        orig_interval = pendingauto.RETRY_INTERVAL_SECONDS
        pendingauto.RETRY_INTERVAL_SECONDS = 0.01
        try:
            self._run(pendingauto._retry_loop(entry_id, entry, lambda p: asyncio.sleep(0, result=True)))
        finally:
            pendingauto.RETRY_INTERVAL_SECONDS = orig_interval
        self.assertEqual(pendingauto.list_pending(), [])

    def test_retry_loop_keeps_retrying_until_max_wait_elapses(self):
        entry_id, entry = "e1", {"kind": "f5", "payload": {"team": "Denver Broncos"}, "queued_at": time.time()}
        pendingauto._persist(entry_id, entry)
        orig_interval, orig_max_wait = pendingauto.RETRY_INTERVAL_SECONDS, pendingauto.MAX_WAIT_SECONDS
        pendingauto.RETRY_INTERVAL_SECONDS = 0.01
        pendingauto.MAX_WAIT_SECONDS = 0.02
        try:
            self._run(pendingauto._retry_loop(entry_id, entry, lambda p: asyncio.sleep(0, result=False)))
        finally:
            pendingauto.RETRY_INTERVAL_SECONDS, pendingauto.MAX_WAIT_SECONDS = orig_interval, orig_max_wait
        # Gives up (removes the entry) once MAX_WAIT_SECONDS elapses without
        # ever resolving, same as pendingtrack's own bounded-wait rule.
        self.assertEqual(pendingauto.list_pending(), [])


class ResumeAll(PendingAutoTestCase):
    def test_resume_all_drops_entries_with_no_registered_resolver(self):
        pendingauto._persist("stale1", {"kind": "no_longer_exists", "payload": {}, "queued_at": 0})
        self._run(pendingauto.resume_all({}))
        self.assertEqual(pendingauto.list_pending(), [])

    def test_resume_all_keeps_entries_with_a_registered_resolver(self):
        # queued_at must be a real current timestamp, not 0 (the Unix
        # epoch) - _retry_loop's own expiry check would otherwise treat
        # this entry as already 50+ years past MAX_WAIT_SECONDS and drop it
        # immediately, unrelated to the "no resolver" case this test means
        # to isolate from test_resume_all_drops_entries_with_no_registered_
        # resolver above.
        pendingauto._persist("live1", {"kind": "f5", "payload": {}, "queued_at": time.time()})

        async def resolve(p):
            return False  # never resolves, but that's fine - just confirming it wasn't dropped

        async def run_and_cancel():
            await pendingauto.resume_all({"f5": resolve})
            # resume_all spins up a background task per entry - give it one
            # tick to start, then confirm the entry is still on disk (it's
            # only removed once resolve() returns True or MAX_WAIT_SECONDS
            # elapses, neither of which happens here).
            await asyncio.sleep(0.01)

        self._run(run_and_cancel())
        self.assertEqual(len(pendingauto.list_pending()), 1)


class FindMatchingAndCancel(PendingAutoTestCase):
    """The /untrack no-game_id branch cancels a still-queued pick this way -
    it never has a game_id to look up by (that's exactly why it's queued),
    so player/team name is the only handle available."""

    def test_find_matching_scopes_to_the_given_channel(self):
        self._queue("playerprops", {"channel_id": 1, "player": "Fernando Tatis Jr."})
        self._queue("playerprops", {"channel_id": 2, "player": "Fernando Tatis Jr."})
        matches = pendingauto.find_matching(1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1]["payload"]["channel_id"], 1)

    def test_find_matching_filters_by_name_case_insensitively(self):
        self._queue("playerprops", {"channel_id": 1, "player": "Fernando Tatis Jr."})
        self._queue("playerprops", {"channel_id": 1, "player": "Casey Mize"})
        matches = pendingauto.find_matching(1, "tatis")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1]["payload"]["player"], "Fernando Tatis Jr.")

    def test_find_matching_falls_back_to_team_for_non_prop_kinds(self):
        self._queue("f5", {"channel_id": 1, "team": "Denver Broncos"})
        matches = pendingauto.find_matching(1, "broncos")
        self.assertEqual(len(matches), 1)

    def test_no_name_filter_returns_every_entry_in_the_channel(self):
        self._queue("playerprops", {"channel_id": 1, "player": "Fernando Tatis Jr."})
        self._queue("f5", {"channel_id": 1, "team": "Denver Broncos"})
        self.assertEqual(len(pendingauto.find_matching(1)), 2)

    def test_cancel_removes_the_entry_and_stops_its_task(self):
        self._queue("playerprops", {"channel_id": 1, "player": "Fernando Tatis Jr."})
        entry_id = next(iter(state.load_pending_auto()))
        self.assertIn(entry_id, pendingauto._active_tasks)
        self.assertTrue(pendingauto.cancel(entry_id))
        self.assertNotIn(entry_id, pendingauto._active_tasks)
        self.assertEqual(pendingauto.list_pending(), [])

    def test_cancel_on_an_unknown_entry_id_returns_false(self):
        self.assertFalse(pendingauto.cancel("not-a-real-id"))


class DisplayName(unittest.TestCase):
    def test_prefers_player(self):
        self.assertEqual(pendingauto.display_name({"player": "Fernando Tatis Jr.", "team": "San Diego Padres"}), "Fernando Tatis Jr.")

    def test_falls_back_to_team(self):
        self.assertEqual(pendingauto.display_name({"team": "Denver Broncos"}), "Denver Broncos")

    def test_falls_back_to_unknown(self):
        self.assertEqual(pendingauto.display_name({}), "?")


if __name__ == "__main__":
    unittest.main()

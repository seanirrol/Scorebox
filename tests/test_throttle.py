#!/usr/bin/env python3
"""
Regression tests for throttle.py's sliding-window rate limiter -
_wait_for_rate_slot must actually space requests out over real time, not
just cap how many are in flight at once (see the module's own docstring
for the live incident this was built to stop).

Run with: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import throttle


class WaitForRateSlot(unittest.TestCase):
    def setUp(self):
        self._orig_max = throttle._MAX_REQUESTS_PER_WINDOW
        self._orig_window = throttle._WINDOW_SECONDS
        throttle._recent_requests.clear()
        throttle._rate_locks.clear()

    def tearDown(self):
        throttle._MAX_REQUESTS_PER_WINDOW = self._orig_max
        throttle._WINDOW_SECONDS = self._orig_window
        throttle._recent_requests.clear()
        throttle._rate_locks.clear()

    def test_requests_within_the_budget_dont_wait(self):
        throttle._MAX_REQUESTS_PER_WINDOW = 3
        throttle._WINDOW_SECONDS = 5.0

        async def go():
            start = time.monotonic()
            for _ in range(3):
                await throttle._wait_for_rate_slot(555)
            return time.monotonic() - start

        elapsed = asyncio.run(go())
        self.assertLess(elapsed, 0.5)

    def test_exceeding_the_budget_blocks_until_a_slot_frees_up(self):
        throttle._MAX_REQUESTS_PER_WINDOW = 2
        throttle._WINDOW_SECONDS = 0.2

        async def go():
            start = time.monotonic()
            for _ in range(3):  # 2 free immediately, 3rd must wait out the window
                await throttle._wait_for_rate_slot(555)
            return time.monotonic() - start

        elapsed = asyncio.run(go())
        self.assertGreaterEqual(elapsed, 0.15)

    def test_different_channels_have_independent_budgets(self):
        throttle._MAX_REQUESTS_PER_WINDOW = 1
        throttle._WINDOW_SECONDS = 5.0

        async def go():
            start = time.monotonic()
            await throttle._wait_for_rate_slot(555)
            await throttle._wait_for_rate_slot(777)  # different channel - shouldn't wait
            return time.monotonic() - start

        elapsed = asyncio.run(go())
        self.assertLess(elapsed, 0.5)

    def test_run_still_executes_the_callable_and_returns_its_result(self):
        async def go():
            return await throttle.run(555, lambda: asyncio.sleep(0, result="done"))

        self.assertEqual(asyncio.run(go()), "done")


if __name__ == "__main__":
    unittest.main()

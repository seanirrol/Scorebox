#!/usr/bin/env python3
"""
Regression tests for /summary's own rendering logic in bot.py - the win
rate line, per-pick status lines, and the multi-embed packer. Explicitly
prioritized (per the user): getting /summary and the win rate right
matters more than tracker mechanics.

_pack_summary_blocks_into_embeds exists specifically because the original
code joined every section into one string and hard-sliced it to Discord's
4096-char embed limit, silently dropping whatever came after - including
the Win Rate line, always appended last. That's the single highest-value
thing to guard against regressing.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import dailylog


class WinRateLine(unittest.TestCase):
    def test_no_decided_picks_shows_an_em_dash(self):
        picks_list = [{"status": "pending"}, {"status": "push"}, {"status": "void"}]
        self.assertEqual(bot._win_rate_line(picks_list), "**Win Rate:** —")

    def test_only_won_and_lost_count_toward_the_rate(self):
        picks_list = [
            {"status": "won"}, {"status": "won"}, {"status": "won"},
            {"status": "lost"},
            {"status": "push"}, {"status": "void"}, {"status": "pending"},
        ]
        self.assertEqual(bot._win_rate_line(picks_list), "**Win Rate:** 3-1 (75.0%)")

    def test_all_losses(self):
        picks_list = [{"status": "lost"}, {"status": "lost"}]
        self.assertEqual(bot._win_rate_line(picks_list), "**Win Rate:** 0-2 (0.0%)")


class SummaryStatusLine(unittest.TestCase):
    def _entry(self, **overrides):
        base = {"status": "pending", "detail": "⏳ Not Started", "label": "Team A ML"}
        base.update(overrides)
        return base

    def test_won_shows_the_win_mark(self):
        line = bot._summary_status_line(self._entry(status="won", detail="WON"))
        self.assertTrue(line.startswith(dailylog.WINMARK))
        self.assertIn("Team A ML", line)

    def test_push_shows_tie_suffix(self):
        line = bot._summary_status_line(self._entry(status="push", detail="PUSH"))
        self.assertIn("Push - Tie", line)

    def test_void_with_reason_shows_the_reason(self):
        line = bot._summary_status_line(self._entry(status="void", detail="VOID - Game not found on 365scores"))
        self.assertIn("Void - Game not found on 365scores", line)

    def test_void_without_reason_shows_no_dash_suffix(self):
        line = bot._summary_status_line(self._entry(status="void", detail="VOID"))
        self.assertNotIn(" — Void - ", line)

    def test_live_detail_uses_the_live_marker(self):
        line = bot._summary_status_line(self._entry(detail="LIVE, Top 3rd 2-1"))
        self.assertTrue(line.startswith("🟡"))

    def test_not_started_detail_uses_pause_marker(self):
        line = bot._summary_status_line(self._entry(detail="NOT STARTED - <t:1786665600:f>"))
        self.assertTrue(line.startswith("⏸️"))
        self.assertIn("Not Started", line)

    def test_unrecognized_detail_falls_back_to_hourglass(self):
        # The default placeholder before a tracker's first status poll.
        line = bot._summary_status_line(self._entry(detail="⏳ Not Started"))
        self.assertTrue(line.startswith("⏳"))


class PackSummaryBlocksIntoEmbeds(unittest.TestCase):
    def test_small_report_produces_exactly_one_embed(self):
        blocks = ["**MLB**\nWon: Team A ML", "**Win Rate:** 1-0 (100.0%)"]
        embeds = bot._pack_summary_blocks_into_embeds("2026-08-13", blocks)
        self.assertEqual(len(embeds), 1)
        self.assertIn("Win Rate", embeds[0].description)
        self.assertEqual(embeds[0].title, "Summary Report (2026-08-13)")

    def test_oversized_report_splits_and_never_drops_the_win_rate_line(self):
        # This is the exact bug: a big slate used to get hard-sliced to
        # 4096 chars, silently losing the Win Rate line (always last).
        blocks = []
        for section in ("MLB", "NFL", "WNBA", "Tennis", "Soccer", "Baseball"):
            lines = [
                f"⏸️ {section} Player {i} Over 75.5 Some Stat (Alt Line) (DraftKings +765) "
                f"— Not Started - August 13, 2026 7:00 PM"
                for i in range(15)
            ]
            blocks.append(f"**{section}**\n" + "\n".join(lines))
        blocks.append("**Win Rate:** 10-8 (55.6%)")

        total_len = sum(len(b) for b in blocks) + 2 * (len(blocks) - 1)
        self.assertGreater(total_len, 4096, "test setup should exceed the old single-embed limit")

        embeds = bot._pack_summary_blocks_into_embeds("2026-08-13", blocks)
        self.assertGreater(len(embeds), 1)
        for e in embeds:
            self.assertLessEqual(len(e.description), 4096)
        # every single one of the 90 lines must survive somewhere
        combined = "\n".join(e.description for e in embeds)
        for section in ("MLB", "NFL", "WNBA", "Tennis", "Soccer", "Baseball"):
            for i in range(15):
                self.assertIn(f"{section} Player {i}", combined)
        self.assertIn("**Win Rate:** 10-8 (55.6%)", combined)

    def test_only_first_embed_gets_the_plain_title(self):
        blocks = ["x" * 4000, "y" * 4000, "**Win Rate:** 1-0 (100.0%)"]
        embeds = bot._pack_summary_blocks_into_embeds("2026-08-13", blocks)
        self.assertGreater(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Summary Report (2026-08-13)")
        for e in embeds[1:]:
            self.assertEqual(e.title, "Summary Report (2026-08-13) (cont'd)")

    def test_a_single_section_bigger_than_the_limit_splits_by_line(self):
        # Falls back to splitting within one oversized block by line,
        # rather than ever emitting an embed over Discord's own limit.
        huge_section = "**MLB**\n" + "\n".join(f"line {i} " + "x" * 100 for i in range(60))
        self.assertGreater(len(huge_section), 4096)
        embeds = bot._pack_summary_blocks_into_embeds("2026-08-13", [huge_section])
        self.assertGreater(len(embeds), 1)
        for e in embeds:
            self.assertLessEqual(len(e.description), 4096)
        combined = "\n".join(e.description for e in embeds)
        for i in range(60):
            self.assertIn(f"line {i} ", combined)


if __name__ == "__main__":
    unittest.main()

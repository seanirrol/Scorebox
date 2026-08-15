#!/usr/bin/env python3
"""
Regression tests for boxing.py's grading logic - against constructed
fight_data dicts, not live BoxingScene scraping (confirmed live/manually
during development instead - see boxing.py's own module docstring for how
that was verified: BoxRec is Cloudflare-blocked, BoxingScene's schedule/
results pages stream real fighter/winner data via the same Next.js flight
format GosuGamers already uses).

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boxing


def _fight(status, winning_fighter_id=None):
    return {
        "sport": "boxing", "fight_id": 10758,
        "fighter1_name": "Troy Williamson", "fighter1_id": 1693,
        "fighter2_name": "Callum Simpson", "fighter2_id": 4524,
        "event_name": "Troy Williamson vs Callum Simpson 2",
        "start_epoch": 1786212000.0, "status": status,
        "page_url": "https://www.boxingscene.com/events/troy-williamson-vs-callum-simpson-2",
        "winning_fighter_id": winning_fighter_id, "decision": "Unanimous Decision" if winning_fighter_id else None,
    }


class GradeBoxingMoneyline(unittest.TestCase):
    def test_not_finished_returns_none(self):
        fight = _fight("notstarted")
        self.assertIsNone(boxing.grade_boxing_moneyline(fight, "Troy Williamson"))

    def test_winner_grades_won(self):
        fight = _fight("finished", winning_fighter_id=4524)
        self.assertEqual(boxing.grade_boxing_moneyline(fight, "Callum Simpson"), "won")

    def test_loser_grades_lost(self):
        fight = _fight("finished", winning_fighter_id=4524)
        self.assertEqual(boxing.grade_boxing_moneyline(fight, "Troy Williamson"), "lost")

    def test_no_winner_id_is_a_push(self):
        # A draw or no-contest - confirmed live a real BoxingScene result
        # (Connor Coyle vs Mark Beuke) had a decision_description but no
        # winning_fighter_id at all.
        fight = _fight("finished", winning_fighter_id=None)
        self.assertEqual(boxing.grade_boxing_moneyline(fight, "Troy Williamson"), "push")
        self.assertEqual(boxing.grade_boxing_moneyline(fight, "Callum Simpson"), "push")

    def test_fighter_not_in_the_fight_returns_none(self):
        fight = _fight("finished", winning_fighter_id=4524)
        self.assertIsNone(boxing.grade_boxing_moneyline(fight, "Claressa Shields"))


class IsFinished(unittest.TestCase):
    def test_notstarted_is_not_finished(self):
        self.assertFalse(boxing.is_finished(_fight("notstarted")))

    def test_finished_status_is_finished(self):
        self.assertTrue(boxing.is_finished(_fight("finished", winning_fighter_id=4524)))


if __name__ == "__main__":
    unittest.main()

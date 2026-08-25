#!/usr/bin/env python3
"""
Regression tests for espn.py's computed-stat conversions.

Run with: python -m unittest discover -s tests -t .
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import espn


def _boxscore_event(ip_raw, entity_id="1", home=True):
    team = {"id": "10" if home else "20"}
    other_team = {"id": "20" if home else "10"}
    row = {
        "team": team,
        "statistics": [{
            "labels": ["IP", "H", "R", "ER", "BB", "K", "HR", "PC-ST", "ERA", "PC"],
            "athletes": [{"athlete": {"id": entity_id}, "stats": [ip_raw, "3", "2", "2", "2", "6", "1", "102-59", "2.59", "102"]}],
        }],
    }
    other_row = {"team": other_team, "statistics": []}
    return {
        "header": {"competitions": [{"status": {"type": {"state": "post"}}}]},
        "boxscore": {"players": [row, other_row] if home else [other_row, row]},
    }


class PitchingOutsConversion(unittest.TestCase):
    """ESPN's raw "IP" boxscore field uses baseball notation - "5.2" means
    5 full innings plus 2 outs (17 outs total), NOT 5.2 decimal innings -
    confirmed live against a real finished game's boxscore. Pitching Outs
    is a real, distinct prop market that needs this converted properly."""

    def test_partial_inning_two_outs(self):
        event = _boxscore_event("5.2")
        value, is_home, team = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 17)

    def test_partial_inning_one_out(self):
        event = _boxscore_event("0.1")
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 1)

    def test_exact_inning_no_partial(self):
        event = _boxscore_event("6.0")
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(value, 18)

    def test_player_not_in_boxscore_returns_none(self):
        event = _boxscore_event("5.2")
        value, _, _ = espn.get_stat_value(event, "does-not-exist", espn.PITCHING_OUTS_KEY)
        self.assertIsNone(value)

    def test_grades_correctly_against_an_over_line(self):
        event = _boxscore_event("5.2")  # 17 outs
        value, _, _ = espn.get_stat_value(event, "1", espn.PITCHING_OUTS_KEY)
        self.assertEqual(espn.grade_over_under(value, "over", 17.5), "lost")
        self.assertEqual(espn.grade_over_under(value, "over", 16.5), "won")


class FindCurrentEventId(unittest.TestCase):
    """find_current_event_id backs every auto-tracked player prop's event
    lookup - monkeypatches _get so this exercises the real date/status
    bounding + tie-break logic without a live ESPN request. Same
    reference_date fix as scores365.find_match_for_team (see that
    module's own tests) - "most recent finished" and "closest to
    reference_date" are the same ordering for an already-finished event,
    so this is provably a no-op for every caller that doesn't pass one."""

    def setUp(self):
        self._orig_get = espn._get
        self._events: list = []
        espn._get = lambda url, **params: {"events": self._events}

    def tearDown(self):
        espn._get = self._orig_get

    def _event(self, event_id, team_id, state, days_offset, hour=18):
        now = datetime.datetime.now(tz=espn.scores365.EASTERN)
        dt = (now + datetime.timedelta(days=days_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return {
            "id": event_id,
            "competitions": [{
                "date": dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "competitors": [{"team": {"id": team_id}}],
            }],
            "status": {"type": {"state": state}},
        }

    def test_reference_date_picks_the_older_event_over_the_one_closest_to_now(self):
        # Confirmed live (see masterparlay.py's own _resolve_team fix): a
        # re-resolution days later against a team that's since played
        # again silently picked up the newer event instead of the one
        # the pick was actually tracked against.
        self._events = [
            self._event("100", "10", "post", days_offset=0),
            self._event("200", "10", "post", days_offset=-2),
        ]
        older_date = (datetime.datetime.now(tz=espn.scores365.EASTERN) - datetime.timedelta(days=2)).date()
        event_id = espn.find_current_event_id(
            "baseball", "10", days_ahead=0, days_back=3, allow_finished=True, reference_date=older_date,
        )
        self.assertEqual(event_id, "200")

    def test_without_reference_date_still_prefers_the_event_closest_to_now(self):
        self._events = [
            self._event("100", "10", "post", days_offset=0),
            self._event("200", "10", "post", days_offset=-2),
        ]
        event_id = espn.find_current_event_id(
            "baseball", "10", days_ahead=0, days_back=3, allow_finished=True,
        )
        self.assertEqual(event_id, "100")


if __name__ == "__main__":
    unittest.main()

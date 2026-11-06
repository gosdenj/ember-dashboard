"""Tests for how much history Ember keeps, and what it throws away.

The question this file answers is "is the database going to fill the
card?" - so every claim here is about a number of rows or bytes, not
about how it feels.

Two things are being defended:

  * every table has a retention window, and the window is enforced;
  * the room log records changes, not every poll, so it stays small.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History


def days_ago(count: int) -> float:
    return (datetime.now(timezone.utc) - timedelta(days=count)).timestamp()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        self.history = History(self.path)

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    # -- every table is covered ---------------------------------------------

    def test_pruning_removes_old_rows_from_every_table(self):
        # The gas readings used to be left behind: prune() only touched
        # polls and the room log, so the table the dashboard is built on
        # grew forever.
        self.history.record_poll(days_ago(500), True, 19.0, 4.0)
        self.history.record_trvs(days_ago(500), [
            {"room": "Lounge", "target": 20.0, "current": 18.0, "valve_open": True},
        ])
        self.history.record_gas([{"ts": days_ago(500), "value": 2.5}])

        self.history.prune(400)

        self.assertIsNone(self.history.latest_poll())
        self.assertEqual(self.history.latest_trvs(), {})
        self.assertEqual(self.history.gas_readings_between(0, days_ago(1)), [])

    def test_rows_inside_the_window_are_kept(self):
        self.history.record_poll(days_ago(10), True, 19.0, 4.0)
        self.history.record_gas([{"ts": days_ago(10), "value": 2.5}])
        self.history.prune(400)
        self.assertIsNotNone(self.history.latest_poll())
        self.assertEqual(len(self.history.gas_readings_between(0, days_ago(1))), 1)

    def test_the_room_log_has_its_own_shorter_window(self):
        # Sixty days old: too old for the room log, well inside the
        # window for the gas and the polls.
        self.history.record_poll(days_ago(60), True, 19.0, 4.0)
        self.history.record_gas([{"ts": days_ago(60), "value": 2.5}])
        self.history.record_trvs(days_ago(60), [
            {"room": "Lounge", "target": 20.0, "current": 18.0, "valve_open": True},
        ])
        self.history.prune(400, trv_days=30)
        self.assertIsNotNone(self.history.latest_poll())
        self.assertEqual(len(self.history.gas_readings_between(0, days_ago(1))), 1)
        self.assertEqual(self.history.latest_trvs(), {})

    def test_pruning_reports_how_many_rows_it_dropped(self):
        self.history.record_poll(days_ago(500), True, 19.0, 4.0)
        self.history.record_gas([{"ts": days_ago(500), "value": 2.5}])
        removed = self.history.prune(400)
        self.assertEqual(removed, 2)

    # -- the room log only records changes ---------------------------------

    def test_the_room_log_only_records_a_change(self):
        # Twelve rooms, polled every two minutes, all saying the same
        # thing: that is eight thousand rows a day saying "the Lounge is
        # still off". A change log says the same thing in a few dozen.
        for index in range(10):
            self.history.record_trvs(1000.0 + index * 120, [
                {"room": "Lounge", "target": 20.0, "current": 19.5,
                 "valve_open": False},
            ])
        self.assertEqual(self.history.stats()["rows"]["trv"], 1)

    def test_a_change_is_recorded(self):
        self.history.record_trvs(1000.0, [
            {"room": "Lounge", "target": 20.0, "current": 19.5, "valve_open": False},
        ])
        self.history.record_trvs(1120.0, [
            {"room": "Lounge", "target": 20.0, "current": 19.1, "valve_open": True},
        ])
        self.assertEqual(self.history.stats()["rows"]["trv"], 2)
        latest = self.history.latest_trvs()
        self.assertEqual(latest["Lounge"]["valve_open"], 1)
        self.assertAlmostEqual(latest["Lounge"]["current"], 19.1)

    def test_a_changed_target_is_a_change(self):
        self.history.record_trvs(1000.0, [
            {"room": "Lounge", "target": 20.0, "current": 19.5, "valve_open": False},
        ])
        self.history.record_trvs(1120.0, [
            {"room": "Lounge", "target": 21.0, "current": 19.5, "valve_open": False},
        ])
        self.assertEqual(self.history.stats()["rows"]["trv"], 2)

    def test_each_room_is_counted_separately(self):
        self.history.record_trvs(1000.0, [
            {"room": "Lounge", "target": 20.0, "current": 19.5, "valve_open": False},
            {"room": "Kitchen", "target": 20.0, "current": 21.0, "valve_open": False},
        ])
        self.history.record_trvs(1120.0, [
            {"room": "Lounge", "target": 20.0, "current": 19.5, "valve_open": False},
            {"room": "Kitchen", "target": 20.0, "current": 19.0, "valve_open": True},
        ])
        self.assertEqual(self.history.stats()["rows"]["trv"], 3)

    def test_pruning_forgets_what_it_remembered_about_the_rooms(self):
        # After a prune the remembered state no longer matches the
        # database, so the next poll has to write it again - otherwise
        # a room would vanish from the log until it changed.
        self.history.record_trvs(days_ago(60), [
            {"room": "Lounge", "target": 20.0, "current": 18.0, "valve_open": True},
        ])
        self.history.prune(400, trv_days=30)
        self.history.record_trvs(1000.0, [
            {"room": "Lounge", "target": 20.0, "current": 18.0, "valve_open": True},
        ])
        self.assertEqual(self.history.stats()["rows"]["trv"], 1)
        self.assertIn("Lounge", self.history.latest_trvs())

    # -- telling the user about it -----------------------------------------

    def test_stats_count_the_rows_and_the_size(self):
        self.history.record_poll(1000.0, True, 19.0, 4.0)
        self.history.record_gas([{"ts": 1000.0, "value": 2.5}])
        stats = self.history.stats()
        self.assertEqual(stats["rows"]["polls"], 1)
        self.assertEqual(stats["rows"]["gas"], 1)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(stats["path"], str(self.path))


class TidyUpTests(unittest.TestCase):
    """The poller has to actually do the pruning."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.settings.update({"history_days": 400, "trv_days": 30,
                              "poll_seconds": 120, "demo": True})
        self.history = History(self.path)
        self.dashboard = Dashboard(self.settings, self.history)

    def tearDown(self):
        self.dashboard.stop()
        self.history.close()
        self.tmp.cleanup()

    def test_the_history_is_pruned_once_a_day_not_once_a_poll(self):
        self.history.record_poll(days_ago(500), True, 19.0, 4.0)
        self.dashboard._tidy()
        # Gone.
        self.assertIsNone(self.history.latest_poll())
        # And the second tidy today does not repeat the work.
        self.history.record_poll(days_ago(500), True, 19.0, 4.0)
        self.dashboard._tidy()
        self.assertIsNotNone(self.history.latest_poll())

    def test_the_growth_rate_is_reported(self):
        stats = self.dashboard.history_stats()
        self.assertEqual(stats["history_days"], 400)
        self.assertEqual(stats["trv_days"], 30)
        self.assertEqual(stats["rows_per_day"]["polls"], 720)
        # The live fetch asks Octopus for hourly figures, so a day is 24
        # rows. (The demo seeds half-hourly ones, which is 48 - but it
        # is the live number that fills the card.)
        self.assertEqual(stats["rows_per_day"]["gas"], 24)
        # The room log is a change log, so a day of it is a handful of
        # rows, not twelve rooms times every poll.
        self.assertLess(stats["rows_per_day"]["trv_changes"],
                        stats["rows_per_day"]["trv_if_every_poll"])

    def test_the_poller_prunes_as_it_goes(self):
        """The pruning happens in the poll loop, not only when asked.

        Calling the tidy-up directly and calling it from the loop are
        different things: only the second one means the database is
        kept small while nobody is watching.
        """
        self.history.record_poll(days_ago(500), True, 19.0, 4.0)

        class OnePass:
            """A stop event that lets the loop run exactly once."""

            def __init__(self):
                self.done = False

            def is_set(self):
                return self.done

            def wait(self, _seconds):
                self.done = True
                return False

            def set(self):
                self.done = True

        self.dashboard._stop = OnePass()
        self.dashboard.poll = lambda: {}
        self.dashboard._run()
        self.assertIsNone(self.history.latest_poll(),
                          "the poll loop did not prune the old history")


if __name__ == "__main__":
    unittest.main()

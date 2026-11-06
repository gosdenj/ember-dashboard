"""Tests for the gap between the meter and the dashboard.

Octopus does not deliver readings as they happen. The readings arrive
with a lag, and when a meter is exchanged the old meter's history and
the new meter's present do not overlap at all.

That creates a trap: a day with no readings looks exactly like a day
with no gas in it. The dashboard must never show 0.00 for a day the
meter has simply not reported yet, and the forecast must never fit a
zero into the model.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), "ember-test-lag.db")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.history = History(self.path)

    def tearDown(self):
        self.history.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _record(self, moment: datetime, m3: float) -> None:
        self.history.record_gas([{"ts": moment.timestamp(), "m3": m3,
                                  "provisional": False}])

    @staticmethod
    def _days_ago(days: int, hour: int = 12) -> datetime:
        """A moment `days` days back, at the given hour."""
        today = datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0,
                                                   microsecond=0)
        return today - timedelta(days=days)

    def test_the_newest_reading_is_found(self):
        self._record(self._days_ago(3), 0.2)
        self._record(self._days_ago(1), 0.3)
        newest = self.history.newest_gas_reading()
        self.assertAlmostEqual(newest, self._days_ago(1).timestamp())

    def test_no_readings_means_no_newest(self):
        self.assertIsNone(self.history.newest_gas_reading())

    def _record_a_whole_day(self, days_ago: int, m3: float = 0.05) -> None:
        """Every hour of a day, the way the meter reports them.

        Octopus returns a row for every interval, zeros included, so a
        fully reported day holds one row per hour.
        """
        for hour in range(24):
            self._record(self._days_ago(days_ago, hour), m3)

    def test_a_day_the_data_has_reached_is_complete(self):
        """The data covers the whole of that day."""
        self._record_a_whole_day(2)
        days = self.history.gas_by_day(7, timezone.utc)
        by_date = {entry["date"]: entry for entry in days}
        target = self._days_ago(2).strftime("%Y-%m-%d")
        self.assertIs(by_date[target]["complete"], True)

    def test_a_day_with_a_handful_of_readings_is_not_complete(self):
        """The meter exchange case: the data has reached past the end of
        the day, but the meter itself did not report it.

        Two readings and a day of silence is not a day of no gas, and a
        forecast fitted on it would be fitted on a lie.
        """
        self._record(self._days_ago(2, 9), 0.2)
        self._record(self._days_ago(2, 23), 0.2)
        # A whole day afterwards, so the data has reached past the
        # sparse one. Without this the day would be incomplete for the
        # trivial reason that the data had not arrived yet.
        self._record_a_whole_day(1)
        days = self.history.gas_by_day(7, timezone.utc)
        by_date = {entry["date"]: entry for entry in days}
        target = self._days_ago(2).strftime("%Y-%m-%d")
        self.assertIs(by_date[target]["complete"], False)

    def test_a_day_the_data_has_not_reached_is_incomplete(self):
        """Today, with the readings still coming in."""
        self._record(self._days_ago(2, 12), 0.2)
        days = self.history.gas_by_day(7, timezone.utc)
        by_date = {entry["date"]: entry for entry in days}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIs(by_date[today]["complete"], False)

    def test_the_days_after_a_gap_are_incomplete(self):
        """The meter exchange case: the old meter's history stops, and
        the days after it have no readings at all."""
        self._record_a_whole_day(1)
        days = self.history.gas_by_day(10, timezone.utc)
        by_date = {entry["date"]: entry for entry in days}
        yesterday = self._days_ago(1).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIs(by_date[yesterday]["complete"], True)
        self.assertIs(by_date[today]["complete"], False)

    def test_a_gap_day_still_reports_its_zero(self):
        """The number is zero, but the flag says do not trust it."""
        self._record_a_whole_day(1)
        days = self.history.gas_by_day(10, timezone.utc)
        by_date = {entry["date"]: entry for entry in days}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertAlmostEqual(by_date[today]["value"], 0.0)
        self.assertIs(by_date[today]["complete"], False)


if __name__ == "__main__":
    unittest.main()


class DashboardHonestyTests(unittest.TestCase):
    """What the dashboard shows when the meter has not reported today."""

    def setUp(self):
        from ember.config import Settings
        from ember.dashboard import Dashboard
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.dir, "settings.json"))
        self.history = History(os.path.join(self.dir, "history.db"))
        self.dashboard = Dashboard(self.settings, self.history, None)

    def tearDown(self):
        self.history.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _fill(self, newest: datetime | None) -> dict:
        if newest is not None:
            self.history.record_gas([{"ts": newest.timestamp(), "m3": 1.5,
                                      "provisional": False}])
        result: dict = {}
        self.dashboard._fill_gas(result, "m3", 11.16,
                                 {"unit_rate_p": 5.63, "standing_charge_p": 28.8},
                                 timezone.utc)
        return result

    def test_a_day_that_has_not_been_reported_is_not_zero(self):
        """Showing 0.00 for a day the meter has not reported would read
        as 'no gas used', which is a lie."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        result = self._fill(yesterday)
        self.assertIsNone(result["today_m3"])
        self.assertIsNone(result["today_kwh"])
        self.assertIsNone(result["cost_today"])
        self.assertFalse(result["today_reported"])
        self.assertIn("no reading for today yet", result["today_note"])

    def test_the_lag_is_reported(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        result = self._fill(yesterday)
        self.assertIsNotNone(result["data_lag_hours"])
        self.assertGreater(result["data_lag_hours"], 12)

    def test_a_reported_day_shows_its_number(self):
        result = self._fill(datetime.now(timezone.utc))
        self.assertTrue(result["today_reported"])
        self.assertAlmostEqual(result["today_m3"], 1.5)
        self.assertIsNotNone(result["cost_today"])

    def test_no_readings_at_all_is_not_a_zero_day(self):
        result = self._fill(None)
        self.assertIsNone(result["today_m3"])
        self.assertFalse(result["today_reported"])


class MeterExchangeNoteTests(unittest.TestCase):
    """A gap that is a meter exchange is not a fault.

    The gas meter was replaced: the old one's readings stop on the day
    it was retired, and the new one takes a fortnight to start. The
    graph is blank for a fortnight, and a blank with no explanation
    looks like something is broken.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.history = History(Path(self.tmp.name) / "history.db")

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    FOUND = {
        "live_serial": "E6S13783422161",
        "meters": [
            {"serial": "E6E09927562343", "active_to": "2026-11-03T00:00:00Z",
             "active_from": "2019-04-01T00:00:00Z"},
            {"serial": "E6S13783422161", "active_to": "",
             "active_from": "2026-11-03T00:00:00Z"},
        ],
    }

    def test_a_gap_that_matches_the_exchange_is_explained(self):
        dashboard = Dashboard(self.settings, self.history, None)
        # The last reading from the old meter: the day it was retired.
        self.history.record_gas([{"ts": 1793664000.0, "value": 0.05}])
        newest = self.history.newest_gas_reading()
        note = dashboard._exchange_note(self.FOUND, newest)
        self.assertIn("meter was replaced", note)
        self.assertIn("3 November 2026", note)

    def test_the_reassurance_is_separate(self):
        """The short note goes on the Today panel; the long one goes on
        the panel with room for it. Three lines of explanation pushed
        the Today panel off the bottom of the screen."""
        dashboard = Dashboard(self.settings, self.history, None)
        self.history.record_gas([{"ts": 1793664000.0, "value": 0.05}])
        newest = self.history.newest_gas_reading()
        detail = dashboard._exchange_detail(self.FOUND, newest)
        self.assertIn("fortnight", detail)
        self.assertIn("backfills", detail)

    def test_a_gap_that_is_not_the_exchange_is_not_explained(self):
        dashboard = Dashboard(self.settings, self.history, None)
        # A reading from well before the exchange.
        self.history.record_gas([{"ts": 1700000000.0, "value": 0.05}])
        newest = self.history.newest_gas_reading()
        self.assertEqual(dashboard._exchange_note(self.FOUND, newest), "")

    def test_no_readings_at_all_says_nothing(self):
        dashboard = Dashboard(self.settings, self.history, None)
        self.assertEqual(dashboard._exchange_note(self.FOUND, None), "")

    def test_an_account_with_one_meter_says_nothing(self):
        dashboard = Dashboard(self.settings, self.history, None)
        found = {"live_serial": "E6E09927562343",
                 "meters": [{"serial": "E6E09927562343", "active_to": ""}]}
        self.history.record_gas([{"ts": 1793664000.0, "value": 0.05}])
        newest = self.history.newest_gas_reading()
        self.assertEqual(dashboard._exchange_note(found, newest), "")

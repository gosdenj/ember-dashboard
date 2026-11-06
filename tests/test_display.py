"""What the panels show, and what they refuse to show.

Three things came out of looking at the live screen:

* the Outside ring was always empty, because the weather reading was
  never copied into the heating result - Hive has no outside
  temperature, so the ring had nothing to show;
* the peak hour read midnight when the meter had reported nothing at
  all, because the hourly array was filled with zeros;
* when today has no reading - a new meter waiting to start - the gas
  panels were a row of dashes with nothing in them, so the last day
  the meter *did* report is now shown, clearly labelled.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History
from ember.weather import WeatherError


def _settings_and_history():
    tmp = tempfile.TemporaryDirectory()
    data = Path(tmp.name)
    settings = Settings(data / "settings.json")
    settings.update({"latitude": 50.78, "longitude": -1.07,
                     "postcode": "PO4 0PL", "timezone": "Europe/London"})
    return tmp, settings, History(data / "history.db")


class OutsideTemperatureTests(unittest.TestCase):
    """The Outside ring has to have something in it.

    Hive reports the house temperature and the boiler, and nothing
    about the weather. The outside figure comes from the weather
    reading - and it was never copied across, so the ring showed a
    dash on a perfectly working dashboard.
    """

    def setUp(self):
        self.tmp, self.settings, self.history = _settings_and_history()

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _poll(self, weather):
        with mock.patch("ember.dashboard.Dashboard._poll_heating",
                        return_value={"boiler_on": False, "rooms": [],
                                      "outside_temp": None}), \
                mock.patch("ember.dashboard.Dashboard._poll_gas",
                           return_value={}), \
                mock.patch("ember.dashboard.weather_module.fetch",
                           return_value=weather):
            dashboard = Dashboard(self.settings, self.history, None)
            return dashboard.poll()

    def test_the_weather_reaches_the_heating_panel(self):
        payload = self._poll({"current": 6.4, "daily": [],
                              "timezone": "Europe/London"})
        self.assertEqual(payload["heating"]["outside_temp"], 6.4)

    def test_no_weather_leaves_it_alone(self):
        with mock.patch("ember.dashboard.Dashboard._poll_heating",
                        return_value={"boiler_on": False, "rooms": [],
                                      "outside_temp": None}), \
                mock.patch("ember.dashboard.Dashboard._poll_gas",
                           return_value={}), \
                mock.patch("ember.dashboard.weather_module.fetch",
                           side_effect=WeatherError("no weather")):
            dashboard = Dashboard(self.settings, self.history, None)
            payload = dashboard.poll()
        self.assertIsNone(payload["heating"]["outside_temp"])


class PeakHourTests(unittest.TestCase):
    """A window with no readings has no peak hour."""

    def setUp(self):
        self.tmp, self.settings, self.history = _settings_and_history()

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _fill(self):
        dashboard = Dashboard(self.settings, self.history, None)
        result = {}
        dashboard._fill_gas(result, "m3", 11.16,
                            {"unit_rate_p": 8.4378,
                             "standing_charge_p": 28.388535},
                            timezone.utc)
        return result

    def test_the_count_is_published(self):
        """How many readings fell in the window.

        The hourly array is always 24 long, with zeros for the hours
        with nothing in them - so the array alone cannot say whether
        there was any data at all, and the peak hour read midnight.
        """
        now = datetime.now(timezone.utc)
        self.history.record_gas([{"ts": now.timestamp(), "value": 0.05}])
        result = self._fill()
        self.assertEqual(result["hourly_count"], 1)
        self.assertEqual(len(result["hourly"]), 24)

    def test_a_window_with_nothing_in_it_says_zero(self):
        result = self._fill()
        self.assertEqual(result["hourly_count"], 0)
        self.assertEqual(len(result["hourly"]), 24)


class LastReportedDayTests(unittest.TestCase):
    """The last day the meter actually reported.

    When today has no reading, the panels would otherwise be dashes
    with nothing in them. This is the most recent real number - and it
    is always labelled as the last reported day, never as today's.
    """

    def setUp(self):
        self.tmp, self.settings, self.history = _settings_and_history()

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _seed(self):
        stop = datetime(2026, 11, 3, tzinfo=timezone.utc)
        for day in range(30, 0, -1):
            date = stop - timedelta(days=day)
            self.history.record_gas([
                {"ts": (date + timedelta(minutes=30 * i)).timestamp(),
                 "value": 2.433 / 48}
                for i in range(48)])

    def test_it_is_found(self):
        self._seed()
        dashboard = Dashboard(self.settings, self.history, None)
        last = dashboard._last_reported_day("m3", 11.16, 8.4378, 28.388535,
                                             None)
        self.assertIsNotNone(last)
        self.assertEqual(last["date"], "2026-11-02")
        self.assertEqual(last["label"], "2 November")
        self.assertAlmostEqual(last["m3"], 2.43, places=2)
        self.assertAlmostEqual(last["kwh"], 27.2, places=1)
        self.assertIsNotNone(last["cost"])

    def test_no_history_at_all_says_nothing(self):
        dashboard = Dashboard(self.settings, self.history, None)
        self.assertIsNone(dashboard._last_reported_day(
            "m3", 11.16, 8.4378, 28.388535, None))

    def test_it_is_not_offered_when_today_is_reported(self):
        """Today's own number is better than yesterday's."""
        self._seed()
        now = datetime.now(timezone.utc)
        self.history.record_gas([{"ts": now.timestamp(), "value": 0.05}])
        dashboard = Dashboard(self.settings, self.history, None)
        result = {}
        dashboard._fill_gas(result, "m3", 11.16,
                            {"unit_rate_p": 8.4378,
                             "standing_charge_p": 28.388535},
                            timezone.utc)
        self.assertTrue(result["today_reported"])
        self.assertNotIn("last_reported", result)

    def test_it_is_offered_when_today_has_nothing(self):
        self._seed()
        dashboard = Dashboard(self.settings, self.history, None)
        result = {}
        dashboard._fill_gas(result, "m3", 11.16,
                            {"unit_rate_p": 8.4378,
                             "standing_charge_p": 28.388535},
                            timezone.utc)
        self.assertFalse(result["today_reported"])
        self.assertIn("last_reported", result)
        self.assertEqual(result["last_reported"]["date"], "2026-11-02")


if __name__ == "__main__":
    unittest.main()

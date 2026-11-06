"""Tests for the estimate of today's gas.

Octopus delivers gas readings with a lag, so "today" almost never has a
reading in it. That does not make today a mystery: the degree-day model
says what a day like today should use.

The rule this file defends is that an estimate is never dressed up as a
measurement. It is published as an estimate, with its band, and it
disappears the moment a real reading exists.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.demo import DemoProvider
from ember.history import History

LONDON = ZoneInfo("Europe/London")


def midnight_today(tz=LONDON) -> datetime:
    now = datetime.now(timezone.utc).astimezone(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class EstimateFixture(unittest.TestCase):
    """A dashboard with a month of gas history behind it.

    `drop_today` takes away the readings the meter has already made
    today, which is the state Ember is normally in: Octopus delivers
    yesterday's gas, not today's.
    """

    drop_today = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(root / "settings.json")
        self.settings.update({"demo": True, "timezone": "Europe/London",
                              "monthly_budget": 60, "base_temperature": 16,
                              "calorific_value": 11.16})
        self.history = History(root / "history.db")
        provider = DemoProvider()
        provider.seed(self.history)
        self.provider = provider
        self.dashboard = Dashboard(self.settings, self.history, provider)
        if self.drop_today:
            self.history.delete_gas_since(midnight_today().timestamp())
        self.payload = self.dashboard.poll()

    def tearDown(self):
        self.dashboard.stop()
        self.history.close()
        self.tmp.cleanup()


class TheComparisonTests(EstimateFixture):
    """"vs average" needs something to compare.

    On a meter that delivers gas a day or two late there is no reading
    for today - so the comparison used to have nothing to work with
    and the tile was empty. The estimate fills it, but only against
    something of the same kind: the estimate is a **whole day**, so it
    is compared with the average of whole days.
    """

    def test_the_comparison_is_not_empty(self):
        gas = self.payload["gas"]
        self.assertFalse(gas["today_reported"])
        self.assertIsNotNone(gas["vs_average_percent"],
                             "the comparison is empty on a lagging meter")

    def test_it_compares_the_estimate_with_the_whole_day_average(self):
        gas = self.payload["gas"]
        estimate = gas["today_estimate"]
        average = gas["average_m3"]
        self.assertGreater(average, 0)
        expected = round((estimate["m3"] - average) / average * 100.0, 1)
        self.assertAlmostEqual(gas["vs_average_percent"], expected, places=1)

    def test_it_says_what_it_compared(self):
        """Whole days against whole days - not a day against a morning."""
        note = self.payload["gas"]["vs_average_note"]
        self.assertIn("whole days", note)


class TodayEstimateTests(EstimateFixture):
    def test_the_estimate_is_published_with_the_gas(self):
        """The key is always there, so the payload's shape is stable."""
        self.assertIn("today_estimate", self.payload["gas"])

    def test_today_has_no_reading(self):
        self.assertFalse(self.payload["gas"]["today_reported"])

    def test_there_is_an_estimate_for_today(self):
        estimate = self.payload["gas"]["today_estimate"]
        self.assertIsNotNone(estimate, "today has no reading and no estimate")
        self.assertGreater(estimate["m3"], 0)
        self.assertGreaterEqual(estimate["band"], 0)

    def test_the_estimate_says_what_the_day_was_like(self):
        estimate = self.payload["gas"]["today_estimate"]
        self.assertIsNotNone(estimate["outside_mean"])
        self.assertIsNotNone(estimate["degree_days"])
        self.assertGreaterEqual(estimate["degree_days"], 0)

    def test_the_estimate_says_how_sure_it_is(self):
        """How many days the fit is standing on has to be published."""
        estimate = self.payload["gas"]["today_estimate"]
        self.assertGreaterEqual(estimate["days_used"], 7)
        self.assertEqual(estimate["model"]["days_used"], estimate["days_used"])

    def test_the_estimate_is_in_the_metres_the_meter_uses(self):
        estimate = self.payload["gas"]["today_estimate"]
        self.assertEqual(self.payload["gas"]["unit"], "m3")
        self.assertIsNotNone(estimate["kwh"])
        self.assertAlmostEqual(estimate["kwh"], estimate["m3"] * 11.16, delta=0.5)

    def test_the_estimate_has_a_cost_because_the_rate_is_known(self):
        estimate = self.payload["gas"]["today_estimate"]
        self.assertIsNotNone(estimate["cost"])
        self.assertGreater(estimate["cost"], 0)

    def test_the_measured_today_is_still_absent(self):
        """The estimate must not fill in the measured fields.

        A panel that shows "today 2.43 m³" when the meter has said
        nothing is a panel making a claim. The measured fields stay
        empty and the estimate is a separate, labelled thing.
        """
        gas = self.payload["gas"]
        self.assertIsNone(gas.get("today_m3"))
        self.assertIsNone(gas.get("cost_today"))
        self.assertTrue(gas.get("today_note"))

    def test_the_estimate_uses_the_whole_day_mean(self):
        """The day's mean, not the mean of the hours so far.

        A half-finished day is colder than the day it is part of, so
        using the hours so far would under-predict every time - and
        the error would be invisible, because it is the kind of mistake
        that makes a number look plausible.
        """
        from ember import forecast
        daily = self.provider.weather()["daily"]
        today = midnight_today().strftime("%Y-%m-%d")
        expected = next(entry["mean"] for entry in daily
                        if entry["date"] == today)
        estimate = self.payload["gas"]["today_estimate"]
        self.assertAlmostEqual(estimate["outside_mean"], expected, places=3)
        self.assertAlmostEqual(estimate["degree_days"],
                               forecast.degree_days(expected, 16.0), places=1)

    def test_the_estimate_is_close_to_the_days_around_it(self):
        """It should be near the days either side of it, not wildly off."""
        estimate = self.payload["gas"]["today_estimate"]
        days = [day for day in self.history.gas_by_day(10, LONDON)
                if day.get("complete")]
        if not days:
            self.skipTest("no complete days to compare with")
        recent = [day["value"] for day in days[-7:]]
        average = sum(recent) / len(recent)
        self.assertLess(abs(estimate["m3"] - average), max(0.6, average * 0.7))


class ReadingBeatsEstimateTests(EstimateFixture):
    """When the meter has reported, there is nothing to estimate."""

    drop_today = False

    def test_today_is_reported(self):
        self.assertTrue(self.payload["gas"]["today_reported"])

    def test_there_is_no_estimate_over_a_real_reading(self):
        self.assertIsNone(self.payload["gas"]["today_estimate"])

    def test_the_measured_today_is_there(self):
        self.assertIsNotNone(self.payload["gas"]["today_m3"])


class NoModelTests(unittest.TestCase):
    """No history, no model, no estimate. A guess is not an estimate."""

    def test_no_estimate_without_a_model(self):
        with tempfile.TemporaryDirectory() as name:
            settings = Settings(Path(name) / "settings.json")
            settings.update({"demo": True, "timezone": "Europe/London"})
            history = History(Path(name) / "history.db")
            provider = DemoProvider()
            dashboard = Dashboard(settings, history, provider)
            try:
                payload = dashboard.poll()
            finally:
                dashboard.stop()
                history.close()
            self.assertIsNone(payload["gas"].get("today_estimate"))
            self.assertFalse(payload["forecast"]["available"])


class UnknownUnitTests(unittest.TestCase):
    """An unknown unit means no kWh and no cost - estimated or not."""

    def test_no_money_in_the_estimate_when_the_unit_is_unknown(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            settings = Settings(root / "settings.json")
            settings.update({"demo": True, "timezone": "Europe/London",
                             "gas_unit": "auto", "calorific_value": 0})
            history = History(root / "history.db")
            provider = DemoProvider()
            provider.seed(history)
            history.delete_gas_since(midnight_today().timestamp())
            dashboard = Dashboard(settings, history, provider)
            try:
                # The meter does not say what it reads in, and nothing
                # can be worked out from the numbers either.
                original = provider.gas
                provider.gas = lambda: dict(original(), unit="unknown", calorific=1.0)
                try:
                    payload = dashboard.poll()
                finally:
                    provider.gas = original
            finally:
                dashboard.stop()
                history.close()
            estimate = payload["gas"].get("today_estimate")
            if estimate is None:
                self.skipTest("no estimate to check")
            self.assertIsNotNone(estimate["m3"])
            self.assertIsNone(estimate["kwh"])
            self.assertIsNone(estimate["cost"])


if __name__ == "__main__":
    unittest.main()

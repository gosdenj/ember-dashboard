"""The week strip: what each day of the week cost.

The big number on each tile is the cost, because that is what the Today
slide is about. The gas that produced it sits underneath in small type,
with the heating hours beside it.

And the days the meter has not reported yet are **estimated** - the
same model, the same band, in the same plum as every other estimate in
Ember. Octopus delivers gas a day or two late, so without this the last
two days of the week would be blank, and a blank is not a number.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.demo import DemoProvider
from ember.history import History
from ember.octopus import rate_at
from ember.server import EmberServer

CHROMIUM = "/root/.cache/ms-playwright/chromium-1243/chrome-linux/chrome"


def _chromium_path():
    if os.path.isfile(CHROMIUM):
        return CHROMIUM
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if os.path.isfile(candidate):
            return candidate
    return None


def midnight_today():
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class RateAtTests(unittest.TestCase):
    """The rate that was in force on a day, not today's rate."""

    def test_the_rate_covering_the_moment_is_found(self):
        rows = [
            {"valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-06-01T00:00:00Z",
             "value": 5.0},
            {"valid_from": "2026-06-01T00:00:00Z", "valid_to": "",
             "value": 8.4},
        ]
        self.assertEqual(
            rate_at(rows, datetime(2026, 3, 1, tzinfo=timezone.utc)), 5.0)
        self.assertEqual(
            rate_at(rows, datetime(2026, 8, 1, tzinfo=timezone.utc)), 8.4)

    def test_a_day_on_the_boundary_belongs_to_the_new_rate(self):
        rows = [
            {"valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-06-01T00:00:00Z",
             "value": 5.0},
            {"valid_from": "2026-06-01T00:00:00Z", "valid_to": "",
             "value": 8.4},
        ]
        self.assertEqual(
            rate_at(rows, datetime(2026, 6, 1, tzinfo=timezone.utc)), 8.4)

    def test_no_rows_means_no_answer(self):
        self.assertIsNone(rate_at([], datetime.now(timezone.utc)))
        self.assertIsNone(rate_at(None, datetime.now(timezone.utc)))


class _WeekFixture:
    """The Today slide, served in a browser.

    The meter's readings for the last two days are taken away, because
    that is the state Octopus usually leaves the dashboard in.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        chromium = _chromium_path()
        if not chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.update({"demo": True, "timezone": "Europe/London",
                             "monthly_budget": 60, "base_temperature": 16,
                             "calorific_value": 11.16})
        cls.history = History(data / "history.db")
        provider = DemoProvider()
        provider.seed(cls.history)
        # Two days with nothing in them: today and yesterday.
        cls.history.delete_gas_since(
            (midnight_today() - timedelta(days=1)).timestamp())
        cls.dashboard = Dashboard(cls.settings, cls.history, provider)
        cls.dashboard.poll()

        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.dashboard.stop()
        cls.tmp.cleanup()

    def _page(self, slide="today"):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1400)
        return page

    def _tiles(self, page):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('.weekstrip > .tile'))"
            ".map((tile) => ({"
            "  label: tile.querySelector('.k').innerText,"
            "  big: tile.querySelector('.v').innerText,"
            "  small: Array.from(tile.querySelectorAll('.s')).map((n) => n.innerText),"
            "  colour: getComputedStyle(tile.querySelector('.v')).color,"
            "  bars: tile.querySelectorAll('.bar').length,"
            "}))")


class WeekStripTests(_WeekFixture, unittest.TestCase):
    """The tiles of the week."""

    def test_the_big_number_is_the_cost(self):
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        for tile in tiles:
            # "~£1.19" is an estimate of the day's cost.
            self.assertTrue(tile["big"].startswith("£")
                            or tile["big"].startswith("~£")
                            or tile["big"] == "—", tile["big"])
            self.assertNotIn("m³", tile["big"])

    def test_the_gas_is_in_the_small_line(self):
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        gas_lines = [line for tile in tiles for line in tile["small"]
                     if "m³" in line]
        self.assertEqual(len(gas_lines), len(tiles))

    def test_the_heating_hours_are_still_there(self):
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        for tile in tiles:
            self.assertTrue(any("heat" in line for line in tile["small"]),
                            tile)

    def test_the_bar_is_gone(self):
        """Nobody knew what it meant, and the cost says it better."""
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        for tile in tiles:
            self.assertEqual(tile["bars"], 0)

    def test_the_days_the_meter_has_not_reported_are_estimated(self):
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        estimated = [tile for tile in tiles if tile["big"].startswith("~")]
        self.assertGreaterEqual(len(estimated), 2)
        for tile in estimated:
            self.assertIn("176, 122, 214", tile["colour"], tile["colour"])

    def test_the_days_that_were_reported_keep_their_measured_cost(self):
        """An estimate never replaces a reading."""
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        measured = [tile for tile in tiles if not tile["big"].startswith("~")]
        self.assertGreaterEqual(len(measured), 5)
        for tile in measured:
            self.assertNotIn("est", " ".join(tile["small"]))

    def test_the_panel_says_how_many_days_are_estimated(self):
        page = self._page()
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("estimated until the meter reports", text)


class DayCostTests(unittest.TestCase):
    """What a day cost, and at what rate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.settings.update({"demo": True, "timezone": "Europe/London",
                               "calorific_value": 11.16})
        self.history = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.history.close)
        self.dashboard = Dashboard(self.settings, self.history, None)

    def test_a_day_is_costed_at_the_rate_of_that_day(self):
        """Not today's rate: a tariff switch in the middle of a week
        would otherwise price the days before it at the rate after it."""
        from zoneinfo import ZoneInfo
        week = [{"date": "2026-11-01", "label": "Sun", "value": 2.0,
                 "complete": True, "reported": True}]
        rates = {
            "unit_rate_p": 8.4, "standing_charge_p": 28.4,
            "unit_rate_rows": [{"valid_from": "2026-01-01T00:00:00Z",
                                "valid_to": "", "value": 4.0}],
            "standing_charge_rows": [],
        }
        days = self.dashboard._day_costs(week, rates, "m3",
                                         ZoneInfo("Europe/London"))
        # 2.0 m3 x 11.16 kWh/m3 x 4.0p = 89.28p, plus the 28.4p standing
        # charge = 117.68p = £1.18
        self.assertAlmostEqual(days[0]["cost"], 1.18, places=2)
        self.assertEqual(days[0]["status"], "measured")

    def test_a_day_with_no_unit_has_no_cost(self):
        from zoneinfo import ZoneInfo
        week = [{"date": "2026-11-01", "label": "Sun", "value": 2.0,
                 "complete": True, "reported": True}]
        rates = {"unit_rate_p": 8.4, "standing_charge_p": 28.4}
        days = self.dashboard._day_costs(week, rates, "unknown",
                                         ZoneInfo("Europe/London"))
        self.assertIsNone(days[0]["cost"])
        self.assertEqual(days[0]["status"], "none")


class TheWeekCostsTests(unittest.TestCase):
    """The payload the week strip is built from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.settings.update({"demo": True, "timezone": "Europe/London",
                               "monthly_budget": 60, "base_temperature": 16,
                               "calorific_value": 11.16})
        self.history = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.history.close)

    def test_each_day_carries_a_status_and_a_cost(self):
        provider = DemoProvider()
        provider.seed(self.history)
        dashboard = Dashboard(self.settings, self.history, provider)
        dashboard.poll()
        days = dashboard.payload()["heating"]["days"]
        self.assertEqual(len(days), 7)
        for day in days:
            self.assertIn(day["status"], ("measured", "estimated", "none"))
            self.assertIn("estimated", day)
            if day["status"] == "measured":
                self.assertIsNotNone(day["cost"])
                self.assertFalse(day["estimated"])
            if day["status"] == "estimated":
                self.assertIsNotNone(day["cost"])
                self.assertTrue(day["estimated"])

    def test_a_day_the_meter_has_not_reported_is_estimated(self):
        provider = DemoProvider()
        provider.seed(self.history)
        self.history.delete_gas_since(
            (midnight_today() - timedelta(days=1)).timestamp())
        dashboard = Dashboard(self.settings, self.history, provider)
        dashboard.poll()
        days = dashboard.payload()["heating"]["days"]
        self.assertTrue(days[-1]["estimated"], "today was not estimated")
        self.assertTrue(days[-2]["estimated"], "yesterday was not estimated")

    def test_a_half_read_day_is_not_called_measured(self):
        """A day that is only partly reported is not a cheaper day.

        The week strip compares whole days with whole days, so a day
        the meter has not finished is estimated rather than measured.
        """
        provider = DemoProvider()
        provider.seed(self.history)
        # Today: three hours of readings and no more.
        self.history.delete_gas_since(midnight_today().timestamp())
        start = midnight_today()
        for hour in range(3):
            self.history.record_gas([{
                "ts": (start + timedelta(hours=hour)).timestamp(),
                "value": 0.05}])
        dashboard = Dashboard(self.settings, self.history, provider)
        dashboard.poll()
        days = dashboard.payload()["heating"]["days"]
        today = days[-1]
        self.assertEqual(today["status"], "estimated")
        self.assertTrue(today["estimated"])

    def test_the_internal_day_costs_do_not_reach_the_browser(self):
        provider = DemoProvider()
        provider.seed(self.history)
        dashboard = Dashboard(self.settings, self.history, provider)
        dashboard.poll()
        payload = dashboard.payload()
        self.assertNotIn("days", payload["gas"])
        for day in payload["heating"]["days"]:
            self.assertNotIn("rate_p", day)


if __name__ == "__main__":
    unittest.main()

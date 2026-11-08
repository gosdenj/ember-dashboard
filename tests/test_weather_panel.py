"""The forecast tile, and the weather that goes in it.

A forecast that says "four degrees" but not "rain" is half a forecast,
so the tile now carries the weather in words and in symbols - but only
**two days** of it, because the panel has room for two and a week of
weather on a dashboard whose job is heating and gas is a week of noise.

The other thing this file checks is where the week's heating hours come
from, because they used to look like a schedule: they are the time the
boiler was actually on, counted from Ember's own polls - and a day
Ember was not running is not a day with no heating, so it says so.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ember import weather as weather_module
from ember.config import Settings
from ember.dashboard import Dashboard
from ember.demo import DemoProvider
from ember.history import History
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


class TheWeatherCodeTests(unittest.TestCase):
    """A code means something, or it means nothing."""

    def test_a_code_becomes_words_and_a_symbol(self):
        self.assertEqual(weather_module.describe(0)["label"], "Clear")
        self.assertEqual(weather_module.describe(0)["icon"], "sun")
        self.assertEqual(weather_module.describe(63)["label"], "Rain")
        self.assertEqual(weather_module.describe(63)["icon"], "rain")
        self.assertEqual(weather_module.describe(95)["icon"], "storm")

    def test_an_unknown_code_is_not_guessed(self):
        """A code nobody has seen is "no idea", not "clear sky"."""
        described = weather_module.describe(999)
        self.assertEqual(described["label"], "")
        self.assertEqual(described["icon"], "thermometer")

    def test_no_code_at_all(self):
        described = weather_module.describe(None)
        self.assertEqual(described["label"], "")
        self.assertIsNone(described["code"])


class TheWeatherSummaryTests(unittest.TestCase):
    """Two days, and nothing that is not there."""

    def _outside(self):
        today = datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
        tomorrow = datetime.now(ZoneInfo("Europe/London")).date()
        tomorrow = tomorrow.fromordinal(tomorrow.toordinal() + 1).isoformat()
        return {"current": 6.4, "current_code": 3, "daily": [
            {"date": today, "mean": 6.4, "min": 3.1, "max": 9.2, "code": 3},
            {"date": tomorrow, "mean": 5.1, "min": 1.0, "max": 7.0, "code": 63},
        ]}

    def test_it_is_today_and_tomorrow(self):
        summary = weather_module.summary(self._outside(),
                                         ZoneInfo("Europe/London"))
        self.assertEqual(summary["today"]["label"], "Overcast")
        self.assertEqual(summary["tomorrow"]["label"], "Rain")
        self.assertEqual(summary["tomorrow"]["icon"], "rain")
        self.assertEqual(sorted(summary.keys()), ["current", "today", "tomorrow"])

    def test_a_day_that_is_not_there_stays_blank(self):
        outside = self._outside()
        outside["daily"] = outside["daily"][:1]
        summary = weather_module.summary(outside, ZoneInfo("Europe/London"))
        self.assertIsNone(summary["tomorrow"]["temp"])
        self.assertEqual(summary["tomorrow"]["label"], "")

    def test_thirty_days_of_weather_become_two(self):
        """The panel has room for two days, and only two."""
        base = datetime.now(ZoneInfo("Europe/London")).date()
        outside = {"current": 6.4, "current_code": 3, "daily": [
            {"date": base.fromordinal(base.toordinal() - offset).isoformat(),
             "mean": 6.0, "min": 2.0, "max": 9.0, "code": 3}
            for offset in range(30)]}
        summary = weather_module.summary(outside, ZoneInfo("Europe/London"))
        self.assertEqual(sorted(summary.keys()), ["current", "today", "tomorrow"])
        self.assertNotIn("week", summary)
        self.assertEqual(sorted(summary["today"].keys()),
                         ["code", "date", "icon", "label", "max", "min", "temp"])


class _PanelFixture:
    """The dashboard, served in a browser."""

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
        page.wait_for_timeout(1200)
        return page


class TheWeatherPanelTests(_PanelFixture, unittest.TestCase):
    """What the tile shows."""

    def test_there_are_two_weather_rows_and_no_more(self):
        page = self._page()
        rows = page.evaluate(
            "() => document.querySelectorAll('.slide .wx-row').length")
        page.close()
        self.assertEqual(rows, 2)

    def test_both_rows_have_a_symbol(self):
        page = self._page()
        icons = page.evaluate(
            "() => document.querySelectorAll('.slide .wx-row .wx-ico svg').length")
        page.close()
        self.assertEqual(icons, 2)

    def test_the_rows_are_today_and_tomorrow(self):
        page = self._page()
        labels = page.evaluate(
            "() => Array.from(document.querySelectorAll('.slide .wx-day'))"
            ".map((node) => node.innerText)")
        page.close()
        # The panel styles them in capitals; the words are the words.
        self.assertEqual([label.strip().lower() for label in labels],
                         ["today", "tomorrow"])

    def test_the_condition_is_in_words(self):
        """A temperature on its own is half a forecast."""
        page = self._page()
        words = page.evaluate(
            "() => Array.from(document.querySelectorAll('.slide .wx-cond'))"
            ".map((node) => node.innerText.trim()).filter(Boolean)")
        page.close()
        self.assertTrue(words, "the panel says nothing about the weather")
        for word in words:
            self.assertNotIn("°", word)

    def test_a_day_with_no_weather_is_not_given_one(self):
        """No code means no sky full of sun."""
        page = self._page()
        page.evaluate(
            "() => { state.payload.weather = {current: {temp: null},"
            " today: {temp: null, min: null, max: null, label: '',"
            " icon: 'thermometer'},"
            " tomorrow: {temp: null, min: null, max: null, label: '',"
            " icon: 'thermometer'}}; render(); }")
        page.wait_for_timeout(300)
        conditions = page.evaluate(
            "() => Array.from(document.querySelectorAll('.slide .wx-cond'))"
            ".map((node) => node.innerText.trim())")
        page.close()
        self.assertEqual(conditions, ["", ""])


class TheHeatingHoursTests(_PanelFixture, unittest.TestCase):
    """Where the week's heating hours come from."""

    def test_a_day_with_no_record_says_so(self):
        """Ember not running is not the same as the heating never
        coming on, so it does not say "0h 00m"."""
        page = self._page()
        page.evaluate(
            "() => { state.payload.heating.days = state.payload.heating.days"
            ".map((day) => ({...day, heat_known: false})); render(); }")
        page.wait_for_timeout(300)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("not recorded", text.lower())
        self.assertNotIn("0h 00m heating", text)

    def test_the_payload_says_whether_a_day_was_recorded(self):
        days = self.dashboard.payload()["heating"]["days"]
        self.assertTrue(days)
        for day in days:
            self.assertIn("heat_known", day)
            self.assertIsInstance(day["heat_known"], bool)

    def test_the_day_that_was_not_recorded_says_so_in_full(self):
        """The week strip is seven tiles wide, so the line is short.

        It used to say "heating not recorded", which was a character too
        long for a tile and came out as "heating not rec..." - a reader
        could not finish it. Nothing may be cut off in the week strip.
        """
        page = self._page()
        page.evaluate(
            "() => { state.payload.heating.days = state.payload.heating.days"
            ".map((day) => ({...day, heat_known: false})); render(); }")
        page.wait_for_timeout(300)
        cut = page.evaluate(
            """() => Array.from(document.querySelectorAll('.slide .weekstrip .s'))
                 .filter((el) => el.scrollWidth > el.clientWidth + 1)
                 .map((el) => el.innerText.trim())""")
        page.close()
        self.assertEqual(cut, [], f"cut off in the week strip: {cut}")


class TheRecordedFlagTests(unittest.TestCase):
    """The history store knows what it knows."""

    def test_a_day_with_no_polls_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as name:
            history = History(Path(name) / "history.db")
            try:
                days = history.heating_minutes_by_day(3, timezone.utc)
                self.assertTrue(days)
                for day in days:
                    self.assertIs(day["recorded"], False)
                    self.assertEqual(day["minutes"], 0.0)
            finally:
                history.close()

    def test_a_day_with_polls_is_recorded(self):
        with tempfile.TemporaryDirectory() as name:
            history = History(Path(name) / "history.db")
            try:
                now = datetime.now(timezone.utc).replace(minute=0, second=0,
                                                          microsecond=0)
                history.record_polls([(now.timestamp(), 1, 19.5, 4.0),
                                      (now.timestamp() + 60, 0, 19.5, 4.0)])
                days = history.heating_minutes_by_day(2, timezone.utc)
                today = days[-1]
                self.assertIs(today["recorded"], True)
                self.assertGreaterEqual(today["minutes"], 0)
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()

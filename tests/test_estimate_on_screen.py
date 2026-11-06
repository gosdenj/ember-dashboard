"""What the gas panel says when today has no reading.

The estimate is only useful if it is obviously an estimate. This file
checks it on the screen: in the predicted colour, with the word
"estimated" on it, and never in place of a reading.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

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


def midnight_today():
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class _GasPanelFixture:
    """The gas panel, served in a browser.

    A mixin rather than a test case, so each scenario gets its own
    server and its own browser.
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
        # Take away today's readings: this is the state Octopus usually
        # leaves the dashboard in.
        cls.history.delete_gas_since(midnight_today().timestamp())
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

    def _page(self, slide):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1500)
        return page

    def _text(self, slide):
        page = self._page(slide)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        return text

    def _kpi_colour(self, slide):
        """The colour of the big figure, as the browser computes it."""
        page = self._page(slide)
        colour = page.evaluate(
            "() => getComputedStyle(document.querySelector('.slide .big .kpi')).color")
        page.close()
        return colour

    # -- the estimate is on the panel ---------------------------------------

class EstimateOnScreenTests(_GasPanelFixture, unittest.TestCase):
    """The gas panel, with the meter a day behind."""

    def test_the_gas_panel_says_estimated(self):
        text = self._text("gas")
        self.assertIn("estimated", text.lower())

    def test_the_estimate_is_in_the_predicted_colour(self):
        """Teal is what the meter reports. Plum is what Ember predicts."""
        colour = self._kpi_colour("gas")
        self.assertIn("176, 122, 214", colour, colour)

    def test_the_today_panel_says_estimated(self):
        text = self._text("today")
        self.assertIn("estimated", text.lower())

    def test_the_estimate_says_how_sure_it_is(self):
        text = self._text("gas")
        self.assertIn("±", text)

    def test_the_panel_still_says_the_reading_is_missing(self):
        """An estimate does not make the missing reading go away."""
        text = self._text("gas")
        self.assertIn("no reading for today", text.lower())

    def test_the_glance_tiles_say_estimated(self):
        text = self._text("gas").lower()
        # The "Gas today" and "Cost" tiles carry the estimate, and the
        # panel says so more than once: an estimate that has to be
        # guessed at is not an estimate.
        self.assertGreaterEqual(text.count("estimated"), 3)


class NothingReportedTests(_GasPanelFixture, unittest.TestCase):
    """The chart with nothing in it has to say so.

    An empty grid of bars looks like a day of no gas. The meter has not
    reported, and those are different things.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Take away everything the meter has ever said, and ask again -
        # the payload is a snapshot, and deleting rows does not change
        # a snapshot that has already been taken.
        cls.history.delete_gas_since(0)
        cls.dashboard.poll()

    def test_the_chart_says_the_meter_has_not_reported(self):
        page = self._page("gas")
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("has not reported", text.lower())

    def test_the_chart_does_not_draw_an_empty_grid(self):
        """No bars, and no "gas used" axis pretending there were some."""
        page = self._page("gas")
        bars = page.evaluate(
            "() => document.querySelectorAll('.slide svg rect').length")
        page.close()
        self.assertEqual(bars, 0)


if __name__ == "__main__":
    unittest.main()

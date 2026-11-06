"""The layout check.

The panel is 800x480 and the text has to fit. A dashboard that overflows
its own panel is a dashboard you cannot read from across the room, so
this is a test rather than something to eyeball.

It needs Playwright and a Chromium. Where that is not available the test
skips rather than fails, so the rest of the suite still runs on a plain
machine.
"""

import os
import shutil
import socket
import tempfile
import threading
import unittest
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


class LayoutTests(unittest.TestCase):
    """Every panel must fit inside itself at 800x480."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        cls.chromium = _chromium_path()
        if not cls.chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
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
            executable_path=cls.chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def check(self, width, height, slide, selector=".panel"):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1800)
        overflow = page.evaluate(
            """(selector) => {
              const bad = [];
              document.querySelectorAll(selector).forEach((el, index) => {
                if (el.scrollHeight > el.clientHeight + 1 ||
                    el.scrollWidth > el.clientWidth + 1) {
                  bad.push({index: index, scrollH: el.scrollHeight,
                            clientH: el.clientHeight,
                            scrollW: el.scrollWidth,
                            clientW: el.clientWidth});
                }
              });
              return bad;
            }""", selector)
        clipped = page.evaluate(
            """() => {
              // Content can be clipped by an inner box without the panel
              // itself overflowing - a row scrolled out of sight inside a
              // fixed-height list looks perfect from the outside and is
              // missing entirely on the screen.
              //
              // The test is whether a child box actually falls outside the
              // box that hides the overflow. Comparing scrollHeight with
              // clientHeight is not enough: a line box with line-height 1
              // reports a few pixels of "overflow" from the font metrics
              // while nothing at all is hidden.
              const bad = [];
              document.querySelectorAll('*').forEach((el) => {
                const style = getComputedStyle(el);
                if (style.overflowY !== 'hidden' && style.overflowY !== 'clip') {
                  return;
                }
                const rect = el.getBoundingClientRect();
                if (rect.height === 0) {
                  return;
                }
                const bottom = rect.bottom - (parseFloat(style.paddingBottom) || 0);
                const top = rect.top + (parseFloat(style.paddingTop) || 0);
                for (const child of el.children) {
                  const childStyle = getComputedStyle(child);
                  if (childStyle.position === 'absolute' ||
                      childStyle.position === 'fixed') {
                    continue;
                  }
                  const cr = child.getBoundingClientRect();
                  if (cr.height === 0 || cr.width === 0) {
                    continue;
                  }
                  const over = Math.max(cr.bottom - bottom, top - cr.top);
                  if (over > 1) {
                    bad.push({box: el.className || el.tagName.toLowerCase(),
                              child: child.className || child.tagName.toLowerCase(),
                              over: Math.round(over),
                              text: (child.textContent || '').trim().slice(0, 40)});
                  }
                }
              });
              return bad;
            }""")
        page.close()
        self.assertEqual(errors, [], f"javascript errors on the {slide} slide")
        self.assertEqual(clipped, [],
                         f"content is clipped on the {slide} slide")
        return overflow

    def test_heating_slide_fits(self):
        self.assertEqual(self.check(800, 480, "heating"), [])

    def test_gas_slide_fits(self):
        self.assertEqual(self.check(800, 480, "gas"), [])

    def test_today_slide_fits(self):
        self.assertEqual(self.check(800, 480, "today"), [])

    def test_the_slide_itself_does_not_scroll(self):
        for slide in ("heating", "gas", "today"):
            page = self.browser.new_page(viewport={"width": 800, "height": 480})
            page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
            page.wait_for_timeout(1800)
            scroll = page.evaluate(
                """() => { const el = document.querySelector('.slide');
                   return [el.scrollHeight, el.clientHeight]; }""")
            page.close()
            self.assertLessEqual(scroll[0], scroll[1] + 1,
                                 f"the {slide} slide overflows")

    def test_all_nine_rooms_are_shown(self):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        for room in ("Dining Room", "Downstairs Hallway", "Kitchen", "Loft Room",
                     "Lounge", "Master Bedroom", "Matteo Bedroom", "Sofia Bedroom",
                     "Upstairs Hallway"):
            self.assertIn(room, text)

    def test_it_works_on_a_phone(self):
        """Below 800px the layout flows, so nothing may overflow sideways."""
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        sideways = page.evaluate(
            """() => document.documentElement.scrollWidth - document.documentElement.clientWidth""")
        page.close()
        self.assertLessEqual(sideways, 1, "the page scrolls sideways on a phone")

    def test_it_works_on_a_tablet(self):
        page = self.browser.new_page(viewport={"width": 768, "height": 1024})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=gas")
        page.wait_for_timeout(1800)
        sideways = page.evaluate(
            """() => document.documentElement.scrollWidth - document.documentElement.clientWidth""")
        page.close()
        self.assertLessEqual(sideways, 1, "the page scrolls sideways on a tablet")

    def test_it_scales_up_to_a_desktop(self):
        page = self.browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=today")
        page.wait_for_timeout(1800)
        scale = page.evaluate("() => getComputedStyle(document.getElementById('stage')).getPropertyValue('--k')")
        page.close()
        self.assertGreater(float(scale or 0), 1.0, "the canvas should fill a desktop")


class SettingsPageLayoutTests(unittest.TestCase):
    """The settings page is set up from a phone, usually.

    It is the one page that gets typed into, so it has to be readable
    and usable on a small screen and must not scroll sideways.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        cls.chromium = _chromium_path()
        if not cls.chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.set("hive_session_file", str(data / "nothing-here.json"))
        cls.history = History(data / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)
        cls.dashboard.poll()
        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=cls.chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def _sideways(self, width, height=844):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(900)
        sideways = page.evaluate(
            """() => document.documentElement.scrollWidth
                      - document.documentElement.clientWidth""")
        page.close()
        return sideways

    def test_it_does_not_scroll_sideways_on_a_phone(self):
        self.assertLessEqual(self._sideways(390), 1,
                             "the settings page scrolls sideways on a phone")

    def test_it_does_not_scroll_sideways_on_a_small_phone(self):
        self.assertLessEqual(self._sideways(320), 1,
                             "the settings page scrolls sideways on a 320px phone")

    def test_it_does_not_scroll_sideways_on_a_tablet(self):
        self.assertLessEqual(self._sideways(768), 1,
                             "the settings page scrolls sideways on a tablet")

    def test_every_field_is_reachable(self):
        """The things you need to type must actually be on the page."""
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(900)
        missing = page.evaluate(
            """() => ['hive_email', 'hive_password', 'octopus_api_key',
                       'postcode', 'monthly_budget', 'base_temperature',
                       'calorific_value', 'gas_unit', 'panel_name',
                       'hive_code_input', 'octopus_account']
                     .filter(id => !document.getElementById(id))""")
        page.close()
        # Every one of them, not just the first.
        self.assertEqual(missing, [])

    def test_the_password_field_is_a_password_field(self):
        """A shoulder-surfer should not get the password off the screen."""
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(600)
        kinds = page.evaluate(
            """() => ['hive_password', 'octopus_api_key'].map(
                 id => (document.getElementById(id) || {}).type)""")
        page.close()
        self.assertEqual(kinds, ["password", "password"])

    def test_the_form_is_remembered_when_the_page_reloads(self):
        """The point of local storage: a reload does not throw the work away."""
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(600)
        page.fill("#postcode", "SW1A 1AA")
        page.wait_for_timeout(300)
        page.reload()
        page.wait_for_timeout(700)
        value = page.input_value("#postcode")
        page.close()
        self.assertEqual(value, "SW1A 1AA")

    def test_the_forget_button_clears_what_the_browser_knew(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(600)
        page.fill("#postcode", "SW1A 1AA")
        page.wait_for_timeout(300)
        page.click("#forget")
        page.wait_for_timeout(400)
        value = page.input_value("#postcode")
        page.close()
        self.assertEqual(value, "")



if __name__ == "__main__":
    unittest.main()


class TroubleTests(unittest.TestCase):
    """An empty panel must say why it is empty.

    On 2026-11-04 the dashboard was showing an empty heating panel and
    the only sign of trouble was a small pill in the header. The reason
    was collected and never displayed, which is worse than no reason at
    all - it looks like there is nothing to report.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        cls.chromium = _chromium_path()
        if not cls.chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        # No Hive session anywhere, so the poll cannot sign in.
        cls.settings = Settings(data / "settings.json")
        cls.settings.set("hive_session_file", str(data / "nothing-here.json"))
        cls.history = History(data / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)
        cls.dashboard.poll()
        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=cls.chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def _text(self, slide):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1500)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        return text

    def test_the_heating_slide_says_why_it_is_empty(self):
        """An empty panel has to say why it is empty, and what to do."""
        text = self._text("heating")
        self.assertIn("not signed in", text.lower())
        self.assertIn("settings", text.lower())

    def test_the_gas_slide_says_why_it_is_empty(self):
        text = self._text("gas")
        self.assertIn("octopus", text.lower())

    def test_the_error_is_also_in_the_payload(self):
        payload = self.dashboard.payload()
        self.assertTrue(payload["errors"])
        self.assertEqual(payload["errors"][0]["source"], "hive")

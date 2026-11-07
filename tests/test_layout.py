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
from unittest import mock

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

    def test_the_slide_comes_in_from_the_right(self):
        """The next slide slides in, and the old one fades away under it.

        The panel is on a wall and nobody is going to press anything, so
        the rotation has to look like it means something: a card dealt
        onto a deck, not a flash.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)

        # Ask for the last slide - the one after the one on the screen -
        # and hold the animations still, so where the slide is can be
        # looked at rather than guessed at.
        during = page.evaluate(
            """() => {
              const dots = document.querySelectorAll('#dots button');
              dots[dots.length - 1].click();
              document.getAnimations().forEach((a) => a.pause());
              const canvas = document.getElementById('canvas')
                              .getBoundingClientRect();
              const slide = document.querySelector('#deck .slide:not(.ghost)')
                              .getBoundingClientRect();
              return {
                ghosts: document.querySelectorAll('#deck .slide.ghost').length,
                dealing: document.querySelectorAll('#deck .slide.dealing').length,
                offset: slide.left - canvas.left,
              };
            }""")
        self.assertEqual(during["ghosts"], 1,
                         "the leaving slide was not copied")
        self.assertEqual(during["dealing"], 1,
                         "the arriving slide is not marked as dealing")
        self.assertGreater(during["offset"], 0,
                           "the slide did not come in from the right")

        # And afterwards the copy is gone.
        page.evaluate("() => document.getAnimations().forEach((a) => a.play())")
        page.wait_for_timeout(1200)
        after = page.evaluate(
            """() => document.querySelectorAll('#deck .slide.ghost').length""")
        page.close()
        self.assertEqual(after, 0, "the copy was left behind")

    def test_going_back_comes_in_from_the_left(self):
        """The other way round, for the arrow key that goes back."""
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=gas")
        page.wait_for_timeout(1800)
        during = page.evaluate(
            """() => {
              document.dispatchEvent(new KeyboardEvent('keydown',
                                        {key: 'ArrowLeft'}));
              document.getAnimations().forEach((a) => a.pause());
              const canvas = document.getElementById('canvas')
                              .getBoundingClientRect();
              const slide = document.querySelector('#deck .slide:not(.ghost)')
                              .getBoundingClientRect();
              return slide.left - canvas.left;
            }""")
        page.close()
        self.assertLess(during, 0, "the slide did not come in from the left")

    def test_a_new_reading_does_not_move_the_slide(self):
        """The dashboard is rebuilt on every reading; that must not slide.

        The panel gets a new reading every two minutes. If every one of
        them sent the slide off again, the panel would be in motion all
        the time and nothing on it could be read.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        during = page.evaluate(
            """() => {
              render();
              return {
                ghosts: document.querySelectorAll('#deck .slide.ghost').length,
                dealing: document.querySelectorAll('#deck .slide.dealing').length,
              };
            }""")
        page.close()
        self.assertEqual(during["ghosts"], 0, "a reading sent the slide away")
        self.assertEqual(during["dealing"], 0, "a reading started a transition")

    def test_no_movement_when_less_movement_is_asked_for(self):
        """Someone who has asked the browser for less movement gets none.

        A screen on a wall that keeps moving is a screen nobody can read,
        and the setting is there for a reason.
        """
        context = self.browser.new_context(
            reduced_motion="reduce", viewport={"width": 800, "height": 480})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        during = page.evaluate(
            """() => {
              const dots = document.querySelectorAll('#dots button');
              dots[1].click();
              const canvas = document.getElementById('canvas')
                              .getBoundingClientRect();
              const slide = document.querySelector('#deck .slide:not(.ghost)')
                              .getBoundingClientRect();
              return {
                ghosts: document.querySelectorAll('#deck .slide.ghost').length,
                dealing: document.querySelectorAll('#deck .slide.dealing').length,
                offset: slide.left - canvas.left,
              };
            }""")
        context.close()
        self.assertEqual(during["ghosts"], 0, "a copy was left behind")
        self.assertEqual(during["dealing"], 0, "the slide was still dealt in")
        self.assertEqual(during["offset"], 0, "the slide moved")

    def test_the_header_stays_put(self):
        """The header does not move when the slide does.

        It carries the clock, and it is the one part of the screen that
        does not change - so it should not slide away with the slide
        underneath it.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        during = page.evaluate(
            """() => {
              const head = document.getElementById('head');
              const before = head.getBoundingClientRect().top;
              const dots = document.querySelectorAll('#dots button');
              dots[1].click();
              document.getAnimations().forEach((a) => a.pause());
              const deck = document.getElementById('deck')
                              .getBoundingClientRect();
              const slide = document.querySelector('#deck .slide:not(.ghost)')
                              .getBoundingClientRect();
              return {
                before: before,
                after: head.getBoundingClientRect().top,
                parent: head.parentElement.id,
                moved: slide.left - deck.left,
              };
            }""")
        page.close()
        self.assertEqual(during["parent"], "canvas",
                         "the header is inside the deck, so it slides")
        self.assertEqual(during["after"], during["before"],
                         "the header moved")
        self.assertGreater(during["moved"], 0,
                           "the slide did not move, so this proves nothing")

    def test_a_swipe_moves_to_the_next_slide(self):
        """A drag across the panel moves to the next slide.

        The panel is on a touch screen, so this is how it is actually
        driven. The mouse is used here because it goes through the same
        pointer events a finger does.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        before = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")

        # A drag to the left is the next slide.
        page.mouse.move(620, 300)
        page.mouse.down()
        page.mouse.move(400, 304, steps=15)
        page.mouse.up()
        page.wait_for_timeout(800)
        after = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")
        page.close()
        self.assertNotEqual(after, before, "the swipe did nothing")

    def test_a_swipe_the_other_way_goes_back(self):
        """And a drag to the right is the one before."""
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=gas")
        page.wait_for_timeout(1800)
        before = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")

        page.mouse.move(200, 300)
        page.mouse.down()
        page.mouse.move(420, 304, steps=15)
        page.mouse.up()
        page.wait_for_timeout(800)
        after = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")
        page.close()
        self.assertNotEqual(after, before, "the swipe did nothing")

    def test_a_drag_that_is_not_a_swipe_does_nothing(self):
        """A short drag, or one that is mostly downwards, is not a swipe.

        Somebody reaching for a control should not change the slide by
        accident, and a drag down the screen is somebody trying to
        scroll.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        before = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")

        # Too short to be a swipe.
        page.mouse.move(620, 300)
        page.mouse.down()
        page.mouse.move(600, 302, steps=5)
        page.mouse.up()
        # Mostly downwards.
        page.mouse.move(400, 200)
        page.mouse.down()
        page.mouse.move(430, 380, steps=15)
        page.mouse.up()
        page.wait_for_timeout(800)
        after = page.evaluate(
            """() => document.querySelector('#head .sub').textContent""")
        page.close()
        self.assertEqual(after, before, "something that was not a swipe changed the slide")

    def test_the_text_cannot_be_selected(self):
        """A finger dragged across the panel must not leave a selection.

        The panel is on a touch screen, and a drag that starts as a tap
        turns into a text selection - a blue smudge across the
        dashboard, on a screen on the wall that nobody is going to
        clear.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        spot = page.evaluate(
            """() => {
              for (const el of document.querySelectorAll('.slide *')) {
                if (el.children.length) continue;
                const text = (el.textContent || '').trim();
                if (text.length < 4) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 40 && r.height > 5) {
                  return {x: r.x, y: r.y, w: r.width, h: r.height};
                }
              }
              return null;
            }""")
        self.assertIsNotNone(spot, "there was no text on the slide to drag over")
        y = spot["y"] + spot["h"] / 2
        page.mouse.move(spot["x"] + 1, y)
        page.mouse.down()
        page.mouse.move(spot["x"] + min(spot["w"], 120), y, steps=12)
        page.mouse.up()
        selected = page.evaluate("() => window.getSelection().toString()")
        page.close()
        self.assertEqual(selected.strip(), "",
                         "the panel's text can be selected")

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
        # The app looks for a session in three places: the one named in
        # the settings, the shared data directory, and a copy the probe
        # leaves next to itself. On a machine that has ever signed in -
        # which is most machines that have run this - the test would
        # find that session, try to renew it against the real Hive, and
        # fail with a completely different reason. So the search is
        # pointed at an empty directory instead: no session anywhere,
        # which is the thing this test is about.
        cls._session_paths = mock.patch(
            "ember.dashboard.Dashboard._session_paths",
            return_value=[str(data / "nothing-here.json")])
        cls._session_paths.start()
        cls.addClassCleanup(cls._session_paths.stop)
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

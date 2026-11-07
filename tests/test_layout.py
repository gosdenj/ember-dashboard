"""The layout check.

The panel is 800x480 and the text has to fit. A dashboard that overflows
its own panel is a dashboard you cannot read from across the room, so
this is a test rather than something to eyeball.

It needs Playwright and a Chromium. Where that is not available the test
skips rather than fails, so the rest of the suite still runs on a plain
machine.
"""

import os
import re
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

# The gas panel when Octopus has not delivered today yet, which is most
# days. It is copied from a real reading rather than invented, because
# the point is the amount of text in it.
BEHIND_THE_METER = """() => {
  state.payload.gas.unit = 'm3';
  state.payload.gas.unit_label = 'm³';
  state.payload.gas.unit_reason = 'the meter is U, which reads cubic metres, '
    + 'so that is what is used - the readings alone would have suggested unknown';
  state.payload.gas.unit_unknown = null;
  state.payload.gas.calorific = 11.16;
  state.payload.gas.unit_rate_p = 8.4378;
  state.payload.gas.standing_p = 28.4;
  state.payload.gas.energy_p = null;
  state.payload.gas.cost_today = null;
  state.payload.gas.today_reported = false;
  state.payload.gas.today_m3 = null;
  state.payload.gas.today_kwh = null;
  state.payload.gas.today_note = 'no reading for today yet - 40 hours behind';
  state.payload.gas.meter_note = null;
  state.payload.gas.today_estimate = {
    m3: 4.62, band: 1.82, kwh: 51.5, cost: 4.63,
    outside_mean: 9.55, degree_days: 6.4, days_used: 19,
  };
  state.payload.gas.last_reported = {
    date: '2026-11-05', label: '5 November',
    m3: 5.1, kwh: 56.9, cost: 5.09,
  };
  render();
}"""


def _chromium_path():
    if os.path.isfile(CHROMIUM):
        return CHROMIUM
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if os.path.isfile(candidate):
            return candidate
    return None


def _covers(unicode_range, codepoint):
    """Does a CSS unicode-range cover a character?

    The browser rewrites the range it was given - U+0000-00FF comes back
    as U+0-FF - so the test cannot look for the text it wrote. It has to
    ask the question instead.
    """
    for part in unicode_range.split(","):
        part = part.strip()
        if part.upper().startswith("U+"):
            part = part[2:]
        if "-" in part:
            low, high = part.split("-", 1)
            if int(low, 16) <= codepoint <= int(high, 16):
                return True
        elif part and int(part, 16) == codepoint:
            return True
    return False


class FontTests(unittest.TestCase):
    """The panel must be set in its own fonts, not the machine's.

    Google Fonts serves each family in two files: a latin subset with the
    letters, the digits and the punctuation, and a latin-ext subset with
    the accented characters. A browser only uses a face for the
    characters its unicode-range covers, and the latin-ext range starts
    at U+0100 - so a face covering only that range has none of the
    letters, none of the digits and none of the punctuation the panel
    uses. Everything quietly falls back to whatever the machine has.

    On a laptop that is the system font and nobody notices. On a
    Raspberry Pi it is the system's own sans-serif, which is wider than
    Outfit, so every line that fitted on the desk does not fit on the
    wall - and the failure is invisible from the development machine.

    So the check is not that the CSS looks right. The check is that the
    right file is shipped, in the right order, and that the browser
    really does set the text in it.
    """

    WEB = Path(__file__).resolve().parent.parent / "ember" / "web"

    def test_the_latin_subset_is_shipped_and_came_first(self):
        """The letters have to be in a file, and in the right place.

        The latin-ext subset covers U+0100 and up: accented characters
        and no A-Z. A panel that ships that one alone is not using these
        fonts at all, it is using the machine's.
        """
        css = (self.WEB / "fonts" / "fonts.css").read_text()
        rules = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
        self.assertTrue(rules, "no @font-face rules in fonts.css")

        for family in ("Outfit", "JetBrains Mono"):
            mine = [r for r in rules if f"'{family}'" in r]
            self.assertTrue(mine, f"no rule for {family}")

            first = mine[0]
            self.assertIn("U+0000-00FF", first,
                          f"the first {family} rule does not cover the "
                          "letters, the digits or the punctuation")

            # The latin subset comes first because the two ranges overlap
            # in a few places, and where they do the browser takes the
            # first face that has the character.
            self.assertTrue(len(mine) > 1, f"{family} has no latin-ext rule")
            self.assertIn("U+0100-02BA", mine[1],
                          f"the second {family} rule is not the latin-ext one")

            urls = []
            for rule in mine:
                url = re.search(r"url\('([^']+)'\)", rule).group(1)
                path = self.WEB / "fonts" / url
                self.assertTrue(path.is_file(), f"missing font file: {url}")
                urls.append(url)
            self.assertEqual(len(set(urls)), len(urls),
                             f"{family} points both subsets at the same file")


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

    def check(self, width, height, slide, selector=".panel", inject=None):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1800)
        if inject:
            page.evaluate(inject)
            page.wait_for_timeout(300)
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

    def test_the_gas_slide_fits_when_the_meter_is_behind(self):
        """The live panel is fuller than the demo's, and it has to fit too.

        Octopus delivers a day or two late, so the day on the panel is
        usually an estimate rather than a reading - and the note about
        which unit the meter reads runs to two lines. That is the worst
        case the gas slide has, and the demo - which reports today and
        says its unit in one line - never reaches it.

        This is the panel's own worst case, written out: it has to fit
        without pushing the unit rate out of sight.
        """
        self.assertEqual(self.check(800, 480, "gas", inject=BEHIND_THE_METER), [])

    def test_the_unit_rate_is_on_the_screen_when_the_meter_is_behind(self):
        """The row that falls off is the bottom one.

        The rows sit in a box that hides its overflow, so when the
        panel is full the last row - the unit rate - is the one that
        disappears. The panel can look perfect from the outside while
        a number is missing, so the rows are checked one by one.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=gas")
        page.wait_for_timeout(1800)
        page.evaluate(BEHIND_THE_METER)
        page.wait_for_timeout(300)
        rows = page.evaluate(
            """() => {
              const box = document.querySelector('.rows');
              const bounds = box.getBoundingClientRect();
              return Array.from(box.children).map((row) => {
                const r = row.getBoundingClientRect();
                return {
                  text: row.innerText.replace(/\\s+/g, ' ').trim(),
                  cut: Math.round(Math.max(r.bottom - bounds.bottom,
                                           bounds.top - r.top)),
                };
              });
            }""")
        page.close()
        self.assertTrue(rows, "no rows in the today tile")
        cut = [row for row in rows if row["cut"] > 1]
        self.assertEqual(cut, [], f"cut off in the today tile: {cut}")
        self.assertTrue(any("Unit rate" in row["text"] for row in rows))

    def test_the_forecast_note_has_room_at_the_bottom_of_its_tile(self):
        """A line that ends on the edge is one line away from being cut.

        The TODAY & TOMORROW tile is exactly as tall as its content, and
        the forecast note is the last thing in it - so when the note ends
        on the panel's edge there is nothing left for the descenders,
        and one more line anywhere above it cuts the note off.

        The bigger type took this tile's slack away. The space came back
        out of the weather rows and the gaps around them.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=today")
        page.wait_for_timeout(1800)
        clearance = page.evaluate(
            """() => {
              const label = document.querySelector('.wx-gas-label');
              if (!label) return null;
              const panel = label.closest('.panel');
              const note = panel.querySelector('.fnote');
              const style = getComputedStyle(panel);
              const bounds = panel.getBoundingClientRect();
              const contentBottom = bounds.bottom - 1
                - (parseFloat(style.paddingBottom) || 0);
              return Math.round(contentBottom - note.getBoundingClientRect().bottom);
            }""")
        page.close()
        self.assertIsNotNone(clearance, "no forecast on the today slide")
        self.assertGreaterEqual(
            clearance, 8,
            "the forecast note has no room at the bottom of its tile")

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

    def test_the_panel_is_set_in_its_own_fonts(self):
        """The text has to be set in the panel's fonts, not the machine's.

        Asking whether a family is available is not enough: a browser
        will happily answer yes for a family it has fallen back for. The
        text has to measure differently from the fallback, and that is
        the only way to know the glyphs really came out of the file.

        The comparison is against 'serif' rather than against the whole
        stack, because the stack has fallbacks of its own: if Outfit were
        missing, 'Outfit', system-ui would still measure differently
        from serif and the panel would look fine here while being wrong
        on the wall. It is not 'monospace' either - every monospace font
        has the same advance width, so comparing JetBrains Mono with the
        generic monospace would compare two identical rulers.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=today")
        page.wait_for_timeout(1800)
        result = page.evaluate(
            """async () => {
              await document.fonts.ready;
              const faces = [];
              document.fonts.forEach((f) => faces.push({
                family: f.family, status: f.status, range: f.unicodeRange}));
              const style = getComputedStyle(document.documentElement);
              const sample = 'Forecast 8.4C and 7.6 degree-days at a 16C base';
              const width = (family, size) => {
                const span = document.createElement('span');
                span.style.cssText = 'position:absolute;visibility:hidden;' +
                  'white-space:nowrap;font-family:' + family +
                  ';font-size:' + size + 'px;font-weight:400;';
                span.textContent = sample;
                document.body.appendChild(span);
                const w = span.getBoundingClientRect().width;
                span.remove();
                return Math.round(w * 10) / 10;
              };
              return {
                faces,
                sans: style.getPropertyValue('--sans'),
                mono: style.getPropertyValue('--mono'),
                serif: width('serif', 11.5),
                outfit: width("'Outfit', serif", 11.5),
                monoFont: width("'JetBrains Mono', serif", 11.5),
              };
            }""")
        page.close()

        self.assertIn("Outfit", result["sans"],
                      "the panel is not set in Outfit")
        self.assertIn("JetBrains Mono", result["mono"],
                      "the numbers are not set in JetBrains Mono")

        for family in ("Outfit", "JetBrains Mono"):
            loaded = [f for f in result["faces"]
                      if family in f["family"] and f["status"] == "loaded"
                      and _covers(f["range"], 0x41) and _covers(f["range"], 0x30)]
            self.assertTrue(loaded, f"the {family} latin subset is not loaded")

        self.assertNotEqual(result["outfit"], result["serif"],
                            "the text is not being set in Outfit")
        self.assertNotEqual(result["monoFont"], result["serif"],
                            "the numbers are not being set in JetBrains Mono")

    def test_the_charts_are_drawn_at_the_size_of_the_panel(self):
        """A chart scaled down to fit is a chart with unreadable labels.

        A chart carries its own units, and the browser scales it to the
        box it has. When the viewBox is taller than that box, the whole
        chart - and every label on it - is shrunk to fit, and the chart
        floats in the middle of the panel instead of filling it. The
        hour numbers on the day chart came out at seven pixels that way.

        So the chart has to be drawn at the size of the space it has:
        the scale is the width of the box over the width of the
        viewBox, and it has to be very nearly one.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        charts = self._charts(page)
        # The demo's gas slide has room to spare. The live one does not:
        # the meter is behind, so the tiles carry a "last reported" line
        # under them, the row above them is shorter, and the chart has
        # less room. That is the case that has to hold.
        page.evaluate(BEHIND_THE_METER)
        page.wait_for_timeout(300)
        charts += self._charts(page)
        page.close()

        # Every slide has a chart, and every chart has to be checked -
        # a chart that has moved out of the way the test looks for it
        # would otherwise go unchecked without saying anything.
        for slide in ("heating", "gas", "today"):
            self.assertTrue([c for c in charts if c["slide"] == slide],
                            f"no chart was found on the {slide} slide")
        for chart in charts:
            where = f"{chart['slide']} chart, viewBox {chart['viewBox']}"
            self.assertGreaterEqual(chart["scale"], 0.95,
                                    f"the {where} is shrunk to fit, so its "
                                    "labels are smaller than the text "
                                    "around them")
            for label in chart["labels"]:
                self.assertGreaterEqual(label["onScreen"], 10.0,
                                        f"the {where} draws {label['text']!r} "
                                        f"at {label['onScreen']}px")

    def _charts(self, page):
        """Every chart on every slide, and how big its labels come out."""
        return page.evaluate(
            """() => {
              const out = [];
              for (const [fn, name] of [['slideHeating','heating'],
                                        ['slideGas','gas'],
                                        ['slideToday','today']]) {
                const holder = document.createElement('div');
                holder.style.cssText = 'position:absolute;left:0;top:0;' +
                  'width:800px;height:480px';
                document.body.appendChild(holder);
                holder.innerHTML = window[fn](state.payload);
                holder.querySelectorAll('.panel > svg, .svgwrap > svg').forEach((svg) => {
                  const vb = svg.viewBox.baseVal;
                  if (!vb || !vb.width) return;
                  const box = svg.getBoundingClientRect();
                  const scale = Math.min(box.width / vb.width, box.height / vb.height);
                  const labels = [];
                  svg.querySelectorAll('text').forEach((t) => {
                    const fs = parseFloat(getComputedStyle(t).fontSize);
                    labels.push({text: t.textContent.trim().slice(0, 14),
                                 onScreen: Math.round(fs * scale * 10) / 10});
                  });
                  out.push({slide: name, viewBox: `${vb.width}x${vb.height}`,
                            scale: Math.round(scale * 1000) / 1000,
                            fill: Math.round(100 * vb.width * scale / box.width),
                            labels});
                });
                holder.remove();
              }
              return out;
            }""")

    def test_nothing_on_the_panel_is_set_below_eleven_pixels(self):
        """The smallest type on the panel is the type on the wall.

        Ten pixels is the size at which the panel starts to be a
        picture of a dashboard rather than a dashboard. This is the
        floor, and the test is here so that a change to the type scale
        has to say out loud that it is going under it.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1800)
        small = page.evaluate(
            """() => {
              const out = [];
              for (const [fn, name] of [['slideHeating','heating'],
                                        ['slideGas','gas'],
                                        ['slideToday','today']]) {
                const holder = document.createElement('div');
                holder.style.cssText = 'position:absolute;left:0;top:0;' +
                  'width:800px;height:480px';
                document.body.appendChild(holder);
                holder.innerHTML = window[fn](state.payload);
                holder.querySelectorAll('*').forEach((el) => {
                  const text = (el.innerText || el.textContent || '').trim();
                  if (!text) return;
                  const fs = parseFloat(getComputedStyle(el).fontSize);
                  if (fs < 11) {
                    out.push({slide: name, size: fs,
                              cls: (el.className || '').toString().slice(0, 24),
                              text: text.replace(/\\s+/g, ' ').slice(0, 30)});
                  }
                });
                holder.remove();
              }
              return out;
            }""")
        page.close()

        self.assertEqual(small, [], "text on the panel is too small to read")


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

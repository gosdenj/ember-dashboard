"""The rings: what their length and their colour mean.

The home ring used to measure the distance to the setpoint - (home - 10)
over (target - 10) - which is not a temperature and is not even stable.
The heating schedule swings the setpoint from 7 degrees, while the
heating is off, to 21 while it is on, so the same house could look full
or three-quarters full with nothing in the world having changed. The
colour was a constant orange that had nothing to do with the temperature
at all, so there was no "colour temperature" to speak of.

It is now a thermometer: the length is the house's own temperature on a
fixed scale, and the colour is that same scale's ramp. Both come from
one number, and the setpoint is a mark on the scale rather than more of
the arc.

The outside ring is the same ramp on the same scale, but it stays a
solid ring: it is not a gauge, and its length would only compete with
the home ring's. Its colour says how cold it is out - so a blue ring
beside an orange one means what it looks like, cold out and warm in.

It needs Playwright and a Chromium. Where that is not available the test
skips rather than fails, so the rest of the suite still runs on a plain
machine.
"""

import os
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


def _rgb(colour):
    """The red, green and blue of an rgb() string."""
    parts = colour.replace("rgb(", "").replace(")", "").split(",")
    return tuple(int(float(part)) for part in parts)


class RingTests(unittest.TestCase):
    """The ring has to mean one thing, and mean it the same way twice."""

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

    def ring(self, home, target=20.0):
        """The home ring, drawn with the given temperatures.

        The ring is built by the page's own function, so this is what
        the panel shows - not a copy of the rules written out again here.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1200)
        result = page.evaluate(
            """([home, target]) => {
              const holder = document.createElement('div');
              holder.innerHTML = ringDiagram({
                home_temp: home, target_temp: target, outside_temp: 8.8,
                boiler_on: false, boosts: [],
              });
              const group = holder.querySelector('[data-ring="home"]');
              const arc = group.querySelector('circle[stroke-dasharray]');
              const tick = group.querySelector('[data-tick="target"]');
              const icon = group.querySelector('[data-icon="home"]');
              const number = group.querySelector('text');
              const dash = arc
                ? arc.getAttribute('stroke-dasharray').split(' ').map(Number)
                : null;
              return {
                dash: dash ? dash[0] : 0,
                circumference: dash ? dash[1] : 0,
                stroke: arc ? arc.getAttribute('stroke') : null,
                hasIcon: !!icon,
                iconColour: icon ? icon.getAttribute('fill') : null,
                hasTick: !!tick,
                tick: tick ? {
                  x1: parseFloat(tick.getAttribute('x1')),
                  y1: parseFloat(tick.getAttribute('y1')),
                  x2: parseFloat(tick.getAttribute('x2')),
                  y2: parseFloat(tick.getAttribute('y2')),
                } : null,
                number: number ? number.textContent : null,
                numberColour: number ? number.getAttribute('fill') : null,
              };
            }""", [home, target])
        page.close()
        return result

    def outside_ring(self, outside):
        """The outside ring, drawn with the given temperature.

        Like the home ring above, this is built by the page's own
        function, so this is what the panel shows.
        """
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1200)
        result = page.evaluate(
            """(outside) => {
              const holder = document.createElement('div');
              holder.innerHTML = ringDiagram({
                home_temp: 19.0, target_temp: 20.0, outside_temp: outside,
                boiler_on: false, boosts: [],
              });
              const group = holder.querySelector('[data-ring="outside"]');
              const circle = group.querySelector('circle');
              const icon = group.querySelector('[data-icon]');
              const number = group.querySelector('text');
              return {
                stroke: circle.getAttribute('stroke'),
                dash: circle.getAttribute('stroke-dasharray'),
                width: circle.getAttribute('stroke-width'),
                hasIcon: !!icon,
                iconName: icon ? icon.getAttribute('data-icon') : null,
                iconColour: icon ? icon.getAttribute('fill') : null,
                number: number ? number.textContent : null,
                numberColour: number ? number.getAttribute('fill') : null,
              };
            }""", outside)
        page.close()
        return result

    def test_the_home_ring_has_a_house_on_it(self):
        """The other rings say what they are with a symbol. So does this."""
        ring = self.ring(19.0)
        self.assertTrue(ring["hasIcon"], "the home ring has no icon")

    def test_a_warmer_house_has_more_of_the_ring(self):
        """The ring fills as the house gets warmer."""
        cold = self.ring(6.0)
        warm = self.ring(22.0)
        self.assertGreater(warm["dash"], cold["dash"],
                           "a warmer house is not more of the ring")

    def test_the_ring_is_full_at_the_top_of_the_scale(self):
        """A house at the top of the scale gets the whole ring."""
        ring = self.ring(25.0)
        self.assertAlmostEqual(ring["dash"], ring["circumference"], delta=1)

    def test_the_ring_is_empty_at_the_bottom_of_the_scale(self):
        ring = self.ring(5.0)
        self.assertAlmostEqual(ring["dash"], 0, delta=1)

    def test_a_cold_house_is_blue_and_a_warm_one_is_orange(self):
        """The colour's temperature matches the temperature."""
        cold = _rgb(self.ring(6.0)["stroke"])
        warm = _rgb(self.ring(22.0)["stroke"])
        self.assertGreater(cold[2], cold[0],
                           "a cold house is not blue")
        self.assertGreater(warm[0], warm[2],
                           "a warm house is not orange")

    def test_the_colour_moves_with_the_temperature(self):
        """Every step up the scale is a step towards the warm end."""
        previous = None
        for temp in (6.0, 12.0, 18.0, 24.0):
            red, green, blue = _rgb(self.ring(temp)["stroke"])
            warmth = red - blue
            if previous is not None:
                self.assertGreater(warmth, previous,
                                   f"{temp} degrees is not warmer than the last")
            previous = warmth

    def test_the_house_and_the_number_take_the_ring_colour(self):
        """One number, one colour - the whole ring agrees."""
        ring = self.ring(18.0)
        self.assertEqual(ring["iconColour"], ring["stroke"])
        self.assertEqual(ring["numberColour"], ring["stroke"])

    def test_the_ring_does_not_move_when_the_schedule_changes(self):
        """The setpoint must not change the length of the ring.

        This is what the old ring did wrong: it measured the distance to
        a setpoint that swung from 7 to 21 with the heating schedule, so
        the same house looked full or nearly empty twice a day.
        """
        frost = self.ring(18.1, target=7.0)
        heating = self.ring(18.1, target=21.0)
        self.assertEqual(frost["dash"], heating["dash"],
                         "the ring moved when the schedule changed")
        self.assertEqual(frost["stroke"], heating["stroke"],
                         "the ring changed colour when the schedule changed")

    def test_the_target_is_marked_on_the_ring(self):
        """The setpoint is a mark on the same scale."""
        ring = self.ring(18.1, target=21.0)
        self.assertTrue(ring["hasTick"], "the target is not marked")
        # 21 on a scale of 5 to 25 is three-quarters of the way round,
        # which is a quarter past the bottom - so the mark is below the
        # middle of the ring and to the left.
        self.assertLess(ring["tick"]["x1"], 214, "the mark is on the wrong side")

    def test_the_mark_moves_when_the_target_moves(self):
        cold = self.ring(18.1, target=10.0)
        hot = self.ring(18.1, target=24.0)
        self.assertNotEqual((cold["tick"]["x1"], cold["tick"]["y1"]),
                            (hot["tick"]["x1"], hot["tick"]["y1"]),
                            "the mark did not move")

    def test_no_reading_leaves_the_ring_empty(self):
        """Nothing to say means nothing drawn."""
        ring = self.ring(None)
        self.assertAlmostEqual(ring["dash"], 0, delta=1)
        self.assertIsNone(ring["stroke"], "the ring is drawn with no reading")
        self.assertEqual(ring["number"], "—")

    def test_the_house_does_not_sit_on_top_of_the_number(self):
        """They are in the same circle, so they have to share it."""
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide=heating")
        page.wait_for_timeout(1200)
        boxes = page.evaluate(
            """() => {
              const group = document.querySelector('[data-ring="home"]');
              const icon = group.querySelector('[data-icon="home"]').getBoundingClientRect();
              const texts = group.querySelectorAll('text');
              const number = texts[0].getBoundingClientRect();
              const label = texts[1].getBoundingClientRect();
              return {
                icon: {top: icon.top, bottom: icon.bottom},
                number: {top: number.top, bottom: number.bottom},
                label: {top: label.top, bottom: label.bottom},
              };
            }""")
        page.close()
        self.assertLessEqual(boxes["icon"]["bottom"], boxes["number"]["top"],
                             "the house is sitting on the number")
        self.assertLessEqual(boxes["number"]["bottom"], boxes["label"]["top"],
                             "the number is sitting on the label")

    # -- the outside ring ---------------------------------------------------

    def test_the_outside_ring_is_still_a_whole_ring(self):
        """It is not a gauge.

        The outside ring was a solid circle before it took a colour, and
        it has to stay one: a second arc on the panel would compete with
        the home ring's, and the home ring is the one that is a gauge.
        """
        ring = self.outside_ring(4.0)
        self.assertIsNone(ring["dash"],
                          "the outside ring is no longer a whole circle")

    def test_a_cold_outside_is_blue(self):
        """The colour follows the temperature, as the home ring's does."""
        cold = _rgb(self.outside_ring(2.0)["stroke"])
        self.assertGreater(cold[2], cold[0], "a cold outside is not blue")

    def test_a_warm_outside_is_orange(self):
        warm = _rgb(self.outside_ring(24.0)["stroke"])
        self.assertGreater(warm[0], warm[2], "a warm outside is not orange")

    def test_the_outside_colour_moves_with_the_temperature(self):
        """Every step up the scale is a step towards the warm end."""
        previous = None
        for temp in (2.0, 9.0, 16.0, 23.0):
            red, green, blue = _rgb(self.outside_ring(temp)["stroke"])
            warmth = red - blue
            if previous is not None:
                self.assertGreater(warmth, previous,
                                   f"{temp} degrees is not warmer than the last")
            previous = warmth

    def test_the_outside_and_the_home_share_one_scale(self):
        """The same temperature is the same colour, in or out.

        Two scales on one panel would mean two things to learn, and the
        eye would have to work out which is which.
        """
        for temp in (4.0, 12.0, 20.0):
            self.assertEqual(self.outside_ring(temp)["stroke"],
                             self.ring(temp)["iconColour"],
                             f"{temp} degrees is not the same colour in and out")

    def test_the_outside_symbol_and_the_number_take_the_ring_colour(self):
        """One number, one colour - the whole ring agrees."""
        ring = self.outside_ring(8.0)
        self.assertEqual(ring["iconColour"], ring["stroke"])
        self.assertEqual(ring["numberColour"], ring["stroke"])

    def test_the_outside_snowflake_sits_on_a_cold_ring(self):
        """The symbol and the colour have to tell the same story."""
        ring = self.outside_ring(1.0)
        self.assertEqual(ring["iconName"], "snowflake")
        red, green, blue = _rgb(ring["stroke"])
        self.assertGreater(blue, red, "a snowflake is not on a cold ring")

    def test_no_reading_leaves_the_outside_ring_quiet(self):
        """Nothing to say means no colour, not a wrong one."""
        ring = self.outside_ring(None)
        self.assertEqual(ring["stroke"], "var(--dimmer)")
        self.assertEqual(ring["number"], "—")
        self.assertIsNone(ring["dash"])


if __name__ == "__main__":
    unittest.main()

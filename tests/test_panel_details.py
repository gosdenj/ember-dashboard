"""The small things that make a panel trustworthy.

Three of them, all of them things a user would notice being wrong:

- **The status tile.** It used to promise a "next schedule block" that
  Ember never read. A row that cannot be filled is a row that has to go,
  and something real has to take its place.
- **The ring icons.** A flame on an idle boiler, a sun on a freezing
  morning: a picture that argues with the number beside it is worse
  than no picture.
- **The clock.** The pill in the corner says what day it is. The LIVE
  pill says whether Ember is talking to the real accounts. Two
  different questions, two different pills.
"""

import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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


class _PanelFixture:
    """Ember, served in a browser."""

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

    def _page(self, slide="heat"):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1200)
        return page

    def _text(self, slide="heat"):
        page = self._page(slide)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        return text


class StatusTileTests(_PanelFixture, unittest.TestCase):
    """The tile has to say something true."""

    def test_the_row_it_could_not_fill_is_gone(self):
        """Ember does not read Hive's schedule, so it cannot promise a
        next block. The row went rather than sit there showing a dash.
        """
        text = self._text()
        self.assertNotIn("next schedule block", text.lower())

    def test_the_row_that_took_its_place_is_there(self):
        text = self._text()
        self.assertIn("a day, on average", text.lower())

    def test_the_average_is_a_time(self):
        text = self._text()
        self.assertRegex(text, r"\d+h \d{2}m")

    def test_the_demo_does_not_invent_a_schedule(self):
        """The demo used to fake a 22:30 block, because a dash looked
        empty. A made-up time is a lie, and the demo is the first thing
        anybody looks at.
        """
        provider = DemoProvider()
        heating = provider.heating()
        self.assertIsNone(heating.get("next_block"))

    def test_the_payload_carries_the_average(self):
        heating = self.dashboard.payload()["heating"]
        self.assertIn("heating_daily_average_minutes", heating)
        minutes = heating["heating_daily_average_minutes"]
        self.assertTrue(minutes is None or minutes > 0)

    def test_todays_partial_day_is_left_out_of_the_average(self):
        """Today is half a day. Comparing it with whole days would
        always make the average look small.

        The window is the last seven *whole* days, so the figure does
        not wobble with the time of day either.
        """
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        heating = self.dashboard.payload()["heating"]
        days = self.history.heating_minutes_by_day(21, tz)
        finished = [entry["minutes"] for entry in days[-8:-1]
                    if entry["minutes"] > 0]
        if not finished:
            self.skipTest("no whole days of heating to average")
        expected = round(sum(finished) / len(finished), 1)
        self.assertEqual(heating["heating_daily_average_minutes"], expected)
        self.assertEqual(heating["heating_daily_average_days"], len(finished))

    def test_today_is_not_one_of_the_days(self):
        """The last day in the window is today, and today is half a
        day. It must not be in the average.
        """
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        days = self.history.heating_minutes_by_day(21, tz)
        today_minutes = days[-1]["minutes"]
        heating = self.dashboard.payload()["heating"]
        average = heating["heating_daily_average_minutes"]
        if average is None:
            self.skipTest("no whole days of heating to average")
        # Today is shorter than a whole day (it is only half past), so
        # a figure that used it would be smaller than the average of
        # the whole days.
        self.assertGreaterEqual(average, today_minutes)


class TheRoomsSummaryTests(_PanelFixture, unittest.TestCase):
    """The bottom of the rooms list, and the status tile above it."""

    def _rows(self, page):
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('.panel .row'))
                 .map((row) => row.innerText.replace(/\\s+/g, ' ').trim())""")

    def test_the_status_tile_has_no_boost_row(self):
        """The ring shows the boost, and says whose it is.

        The tile had a "Boost" row that said only "until 18:30" - the
        same fact, said twice, and the second time without saying what
        was boosted.
        """
        page = self._page()
        rows = self._rows(page)
        page.close()
        boosts = [row for row in rows if row.startswith("Boost")]
        self.assertEqual(boosts, [], f"the boost row is still there: {boosts}")

    def test_the_valve_count_is_a_pill(self):
        page = self._page()
        count = page.evaluate(
            """() => { const pill = Array.from(document.querySelectorAll('.pill'))
                 .find((node) => /valves/.test(node.innerText));
                 return pill ? pill.innerText.replace(/\\s+/g, ' ').trim() : null; }""")
        page.close()
        self.assertIsNotNone(count, "the valve count is not a pill")
        self.assertIn("12 valves", count)

    def test_a_room_asking_for_heat_gets_the_heating_pill(self):
        """Somebody is asking for heat, so it is worth a colour."""
        page = self._page()
        pill = page.evaluate(
            """() => { const node = document.querySelector('.pill.heating');
                 return node ? node.innerText.replace(/\\s+/g, ' ').trim() : null; }""")
        page.close()
        self.assertIsNotNone(pill, "no heating pill on the rooms list")
        self.assertIn("of 9", pill)


class TheDemoHeatingTests(_PanelFixture, unittest.TestCase):
    """The demo has to look like a dashboard that has been running.

    Its seeded polls were half an hour apart, which is not something a
    running dashboard does - and the heating hours are now only counted
    for the periods it was watching, so the demo's figures would have
    been a lower bound of nothing.
    """

    def test_the_demo_heating_hours_are_not_zero(self):
        page = self._page(slide="today")
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertNotIn("0h 00m heating", text)

    def test_the_demo_days_are_not_all_the_same(self):
        """The hours have to vary with the cold, or they look like a
        schedule - which is the thing Ember is not showing."""
        page = self._page(slide="today")
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        hours_found = set()
        for line in text.split("\n"):
            if "heating" in line and "h" in line:
                hours_found.add(line.strip())
        self.assertGreater(len(hours_found), 3,
                           "every day has the same heating time")


class ThePartialHeatingTests(_PanelFixture, unittest.TestCase):
    """When Ember was not watching all day, the heating is a lower bound.

    Hive measures the boiler's run time itself. Ember counts it from its
    own polls, so a period when Ember was not running is missing from
    the total - and the total looks like a whole day's heating.
    """

    def _slide(self, **heating):
        base = {
            "home_temp": 19.0, "target_temp": 20.0, "outside_temp": 5.0,
            "boiler_on": False, "boost_until": None, "boosts": [],
            "rooms": [], "heating_today_minutes": 120.0,
            "heating_week_minutes": 600.0,
            "heating_daily_average_minutes": 90.0,
        }
        base.update(heating)
        page = self._page()
        html = page.evaluate("(payload) => slideHeating(payload)",
                              {"heating": base, "gas": {}})
        page.close()
        return html

    def test_the_status_tile_says_at_least(self):
        """Each row has to say it for itself.

        A test that only looks for the word anywhere in the panel would
        pass on the week row's "at least" even if today's had been
        dropped - and then tomorrow's bug would be tomorrow's problem.
        """
        html = self._slide(heating_today_partial=True,
                            heating_week_partial=True)
        self.assertRegex(html, r"Heating today</span><span class=\"t\">at least")
        self.assertRegex(html, r"Heating this week</span><span class=\"t\">at least")

    def test_the_week_row_alone_says_at_least(self):
        html = self._slide(heating_today_partial=False,
                            heating_week_partial=True)
        self.assertRegex(html, r"Heating today</span><span class=\"t\">2h")
        self.assertRegex(html, r"Heating this week</span><span class=\"t\">at least")

    def test_a_fully_watched_day_plainly_states_the_time(self):
        html = self._slide(heating_today_partial=False,
                            heating_week_partial=False)
        self.assertNotIn("at least", html)

    def test_the_source_line_says_it_only_counts_what_it_watched(self):
        html = self._slide()
        self.assertIn("only while it was watching", html)

    def _week(self, **day):
        base = {"label": "Sat", "m3": 1.8, "cost": 1.4, "heat_minutes": 256,
                "heat_known": True, "heat_partial": False,
                "status": "measured"}
        base.update(day)
        page = self._page(slide="today")
        html = page.evaluate(
            "(payload) => slideToday(payload)",
            {"heating": {"days": [base]}, "gas": {}, "weather": {}})
        page.close()
        return html

    def test_a_day_it_did_not_watch_all_of_is_marked(self):
        """A lower bound has to be marked as one, wherever it appears."""
        html = self._week(heat_partial=True)
        self.assertIn("+ heating", html)

    def test_a_day_it_watched_all_of_is_not_marked(self):
        html = self._week(heat_partial=False)
        self.assertNotIn("+ heating", html)

    def test_the_week_says_how_many_days_were_only_partly_watched(self):
        page = self._page(slide="today")
        html = page.evaluate(
            "(payload) => slideToday(payload)",
            {"heating": {"days": [
                {"label": "Sat", "m3": 1.8, "cost": 1.4, "heat_minutes": 256,
                 "heat_known": True, "heat_partial": True,
                 "status": "measured"},
                {"label": "Sun", "m3": 1.7, "cost": 1.3, "heat_minutes": 200,
                 "heat_known": True, "heat_partial": False,
                 "status": "measured"},
            ]}, "gas": {}, "weather": {}})
        page.close()
        self.assertIn("lower bound", html)


class RingIconTests(_PanelFixture, unittest.TestCase):
    """The icons have to mean what the ring says."""

    def _ring(self, page, **overrides):
        heating = {"home_temp": 19.0, "target_temp": 20.0, "outside_temp": 10.0,
                   "boiler_on": False, "boost_until": None}
        heating.update(overrides)
        return page.evaluate("(heating) => ringDiagram(heating)", heating)

    def test_a_firing_boiler_has_a_flame(self):
        page = self._page()
        svg = self._ring(page, boiler_on=True)
        page.close()
        self.assertIn('data-icon="flame"', svg)

    def test_an_idle_boiler_has_no_flame(self):
        page = self._page()
        svg = self._ring(page, boiler_on=False)
        page.close()
        self.assertNotIn('data-icon="flame"', svg)

    def test_a_cold_morning_has_a_snowflake(self):
        page = self._page()
        svg = self._ring(page, outside_temp=2.0)
        page.close()
        self.assertIn('data-icon="snowflake"', svg)

    def test_a_warm_day_has_a_sun(self):
        page = self._page()
        svg = self._ring(page, outside_temp=25.0)
        page.close()
        self.assertIn('data-icon="sun"', svg)

    def test_an_ordinary_day_has_a_thermometer(self):
        page = self._page()
        svg = self._ring(page, outside_temp=12.0)
        page.close()
        self.assertIn('data-icon="thermometer"', svg)

    def test_an_unknown_temperature_has_a_thermometer(self):
        """Nothing is known, so nothing is claimed. The plain fact of a
        temperature is the honest icon for "no idea".
        """
        page = self._page()
        svg = self._ring(page, outside_temp=None)
        page.close()
        self.assertIn('data-icon="thermometer"', svg)

    def test_the_boost_ring_counts_the_minutes_that_are_left(self):
        page = self._page()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=18)).timestamp()
        svg = self._ring(page, boost_until=soon)
        page.close()
        self.assertIn("18m", svg)

    def test_a_rooms_boost_is_shown_and_named(self):
        """The ring used to show only the boiler's boost.

        A boost on a room's valve is a different thing, and it is the
        one a person in the house is thinking about. The ring shows
        whichever boost ends soonest, and says whose it is.
        """
        page = self._page()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=18)).timestamp()
        svg = self._ring(page, boosts=[
            {"room": "Kitchen", "until": soon, "source": "room"}])
        page.close()
        self.assertIn("18m", svg)
        self.assertIn("KITCHEN", svg)

    def test_the_boost_ring_names_the_boiler_when_it_is_the_boilers(self):
        page = self._page()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=18)).timestamp()
        svg = self._ring(page, boosts=[
            {"room": "Boiler", "until": soon, "source": "boiler"}])
        page.close()
        self.assertIn("BOILER", svg)

    def test_the_soonest_boost_is_the_one_that_is_shown(self):
        page = self._page()
        now = datetime.now(timezone.utc)
        svg = self._ring(page, boosts=[
            {"room": "Kitchen",
             "until": (now + timedelta(minutes=40)).timestamp(),
             "source": "room"},
            {"room": "Lounge",
             "until": (now + timedelta(minutes=12)).timestamp(),
             "source": "room"}])
        page.close()
        self.assertIn("12m", svg)
        self.assertIn("LOUNGE", svg)
        self.assertNotIn("KITCHEN", svg)


class TheRingIconSizeTests(_PanelFixture, unittest.TestCase):
    """The icons are big enough to read, and still inside their rings."""

    def _sizes(self, page):
        return page.evaluate("""() => {
          const out = {};
          document.querySelectorAll('[data-ring]').forEach((ring) => {
            const icon = ring.querySelector('[data-icon]');
            const circle = ring.querySelector('circle');
            if (!icon || !circle) return;
            const box = icon.getBoundingClientRect();
            const measured = circle.getBoundingClientRect();
            // The size the code asks for, which is what "bigger" means.
            // The painted size depends on the symbol: a snowflake does
            // not fill its box the way a flame does.
            const scale = /scale\(([\d.]+)\)/.exec(
              icon.getAttribute('transform') || '');
            out[ring.getAttribute('data-ring')] = {
              icon: Math.max(box.width, box.height),
              nominal: scale ? Number(scale[1]) * 24 : 0,
              ring: measured.width,
              iconCentre: box.left + box.width / 2,
              ringCentre: measured.left + measured.width / 2,
              ringCentreY: measured.top + measured.height / 2,
              iconCentreY: box.top + box.height / 2,
            };
          });
          return out;
        }""")

    def test_the_icons_are_big_enough_to_see(self):
        page = self._page()
        sizes = self._sizes(page)
        page.close()
        self.assertTrue(sizes, "no ring icons were found")
        for name, entry in sizes.items():
            self.assertGreaterEqual(entry["nominal"], 20,
                                    f"the {name} icon is only "
                                    f"{entry['nominal']:.0f}px")

    def test_the_icons_stay_inside_their_rings(self):
        """A bigger icon must still fit: it cannot touch the ring."""
        page = self._page()
        sizes = self._sizes(page)
        page.close()
        for name, entry in sizes.items():
            self.assertLess(entry["icon"], entry["ring"] - 8,
                            f"the {name} icon fills its ring")
            # The icon sits above the ring's centre - the word is
            # below it - so what matters is the clearance between the
            # icon's edge and the ring's edge.
            dx = entry["iconCentre"] - entry["ringCentre"]
            dy = entry["iconCentreY"] - entry["ringCentreY"]
            clearance = (entry["ring"] / 2
                         - (dx ** 2 + dy ** 2) ** 0.5
                         - entry["icon"] / 2)
            self.assertGreater(clearance, 2,
                               f"the {name} icon touches its ring")


class TheRingLayoutTests(_PanelFixture, unittest.TestCase):
    """The rings, and the space between them."""

    def test_nothing_joins_the_rings_up(self):
        """The dotted lines said "these are connected", which the
        layout already says, and they ran through the numbers.

        The setpoint's notch is a line too, but it is a mark on one
        ring and not a line joining the rings together, so it is
        counted apart.
        """
        page = self._page()
        lines = page.evaluate(
            "() => document.querySelectorAll('.slide svg line"
            ":not([data-tick=\"target\"])').length")
        page.close()
        self.assertEqual(lines, 0, "there are still lines between the rings")

    def test_the_boost_ring_is_not_under_the_home_ring(self):
        """It was crowding the temperature, so it went under the
        outside ring instead."""
        page = self._page()
        places = page.evaluate(
            "() => { const ring = (name) => { const node ="
            " document.querySelector(`[data-ring=\"${name}\"] circle`);"
            " return node ? {x: Number(node.getAttribute('cx')),"
            " y: Number(node.getAttribute('cy'))} : null; };"
            " return {home: ring('home'), boost: ring('boost'),"
            " outside: ring('outside')}; }")
        page.close()
        self.assertIsNotNone(places["boost"])
        self.assertGreater(places["boost"]["x"], places["home"]["x"],
                           "the boost ring is still under the home ring")
        self.assertAlmostEqual(places["boost"]["x"], places["outside"]["x"],
                               delta=2, msg="the boost ring is not under the "
                                           "outside ring")

    def test_the_boost_ring_has_room_to_its_label(self):
        page = self._page()
        gap = page.evaluate(
            "() => { const circle = document.querySelector"
            "('[data-ring=\"boost\"] circle');"
            " const label = Array.from(document.querySelectorAll"
            "('.slide svg text')).find((node) => node.textContent.startsWith('BOOST'));"
            " if (!circle || !label) return null;"
            " const r = Number(circle.getAttribute('r'));"
            " const cy = Number(circle.getAttribute('cy'));"
            " return Number(label.getAttribute('y')) - (cy + r); }")
        page.close()
        self.assertIsNotNone(gap)
        self.assertGreaterEqual(gap, 14, "the boost label is too close to its ring")

    def test_the_home_ring_has_room_for_the_temperature(self):
        """The number used to nearly touch the edge of its circle."""
        page = self._page()
        room = page.evaluate(
            "() => { const circle = document.querySelector"
            "('[data-ring=\"home\"] circle');"
            " const text = Array.from(document.querySelectorAll"
            "('.slide svg text')).find((node) => /°$/.test(node.textContent));"
            " if (!circle || !text) return null;"
            " const r = Number(circle.getAttribute('r'));"
            " const size = Number(text.getAttribute('font-size'));"
            " const width = text.textContent.length * size * 0.6;"
            " return (r * 2 - 7 - width); }")
        page.close()
        self.assertIsNotNone(room)
        self.assertGreater(room, 12, "the temperature is cramped in its ring")


class ClockTests(_PanelFixture, unittest.TestCase):
    """The two pills in the corner answer two different questions."""

    def test_the_clock_pill_says_the_day_and_the_time(self):
        page = self._page()
        text = page.evaluate(
            "() => document.querySelector('.pill.clock').innerText")
        page.close()
        self.assertRegex(text, r"\w{3}")
        self.assertRegex(text, r"\d{1,2}:\d{2}")

    def test_the_live_pill_has_no_time_in_it(self):
        """The LIVE pill says whether Ember is talking to the real
        accounts. The time is a separate question, with its own pill.

        The demo is not live, so the live header is built here directly
        - the same function the page uses, with the mode turned on.
        """
        page = self._page()
        html = page.evaluate(
            "() => { const saved = state.payload.mode;"
            " state.payload.mode = 'live';"
            " const out = head('x');"
            " state.payload.mode = saved;"
            " return out; }")
        page.close()
        self.assertIn("LIVE", html)
        # The clock has a time in it - that is its job. The live pill
        # must not: it answers a different question.
        pill = re.search(r'<span class="pill live">.*?</span>', html, re.S)
        self.assertIsNotNone(pill)
        self.assertNotRegex(pill.group(0), r"\d{1,2}:\d{2}")

    def test_the_demo_pill_says_demo(self):
        page = self._page()
        text = page.evaluate(
            "() => { const pill = document.querySelector('.pill.demo');"
            " return pill ? pill.innerText : ''; }")
        page.close()
        self.assertEqual(text.strip().upper(), "DEMO")

    def test_the_demo_does_not_claim_to_be_live(self):
        """The LIVE pill means "talking to the real accounts". In demo
        mode Ember is talking to nothing, so the pill must not be there.
        """
        page = self._page()
        live = page.evaluate(
            "() => document.querySelectorAll('.pill.live').length")
        page.close()
        self.assertEqual(live, 0)

    def test_there_is_no_empty_pill(self):
        """A pill with nothing in it is a gap in the layout."""
        page = self._page()
        empty = page.evaluate(
            "() => Array.from(document.querySelectorAll('.pill'))"
            ".filter((pill) => !pill.innerText.trim()).length")
        page.close()
        self.assertEqual(empty, 0)


if __name__ == "__main__":
    unittest.main()

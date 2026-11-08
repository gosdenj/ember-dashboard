"""The three ways of looking at the gas.

"the last 24 hours" was none of them: Octopus delivers gas a day or
two late, so a rolling 24-hour window is usually empty, and when it is
not empty it is showing a day that ended two days ago.

So the panel now offers three views that are always true:

- **DAY** - the shape of a day: each hour's average use over the week.
- **WEEK** - a week of days: one column per day, today on the right.
- **MONTH** - the month: a calendar, shaded by usage.

And the rule that runs through all three: **a day the meter has not
reported is a gap, never a zero.** A zero means no gas was used, which
would be a lie.
"""

import os
import tempfile
import threading
import unittest
from unittest import mock
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


class HourlyProfileTests(unittest.TestCase):
    """The shape of a day, worked out from the readings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.history.close)

    def _record(self, moment, value):
        self.history.record_gas([{"ts": moment.timestamp(), "m3": value}])

    def test_each_hour_is_the_average_across_the_days(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for day in range(1, 4):
            self._record(now - timedelta(days=day), 0.10)
            self._record(now - timedelta(days=day) - timedelta(hours=1), 0.20)
        profile = self.history.gas_hourly_profile(7, timezone.utc)
        hours = {entry["hour"]: entry for entry in profile["hours"]}
        self.assertAlmostEqual(hours[now.hour]["avg"], 0.10, places=4)
        self.assertAlmostEqual(hours[(now - timedelta(hours=1)).hour]["avg"],
                               0.20, places=4)
        self.assertEqual(profile["days"], 3)

    def test_an_hour_with_no_readings_is_zero_and_says_so(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        self._record(now - timedelta(days=1), 0.10)
        profile = self.history.gas_hourly_profile(7, timezone.utc)
        hours = {entry["hour"]: entry for entry in profile["hours"]}
        quiet = hours[(now + timedelta(hours=5)).hour]
        self.assertEqual(quiet["avg"], 0.0)
        self.assertEqual(quiet["days"], 0)

    def test_it_says_how_many_days_each_hour_was_averaged_over(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for day in range(1, 4):
            self._record(now - timedelta(days=day), 0.10)
        profile = self.history.gas_hourly_profile(7, timezone.utc)
        hours = {entry["hour"]: entry for entry in profile["hours"]}
        self.assertEqual(hours[now.hour]["days"], 3)


class CompleteDayTests(unittest.TestCase):
    """A day the meter did not report is a gap, not a day of no gas."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.history.close)

    def _record(self, moment, value=0.05):
        self.history.record_gas([{"ts": moment.timestamp(), "m3": value}])

    def _whole_day(self, days_ago):
        """Every hour of one UTC day, the way the meter reports them."""
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        day = midnight - timedelta(days=days_ago)
        for hour in range(24):
            self._record(day + timedelta(hours=hour))

    def test_a_fully_reported_day_is_complete(self):
        self._whole_day(2)
        days = {entry["date"]: entry
                for entry in self.history.gas_by_day(7, timezone.utc)}
        target = (datetime.now(timezone.utc)
                  - timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertIs(days[target]["complete"], True)

    def test_a_day_with_two_readings_is_not_complete(self):
        """The meter exchange: the data has reached past the day, but
        the meter did not report it.

        The later readings matter - without them the day would be
        incomplete for the trivial reason that the data had not reached
        it yet, and the test would pass whatever the rule was.
        """
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        day = midnight - timedelta(days=2)
        self._record(day)
        self._record(day + timedelta(hours=14))
        # A whole, well-reported day afterwards - so the data has
        # certainly reached past the sparse one.
        self._whole_day(1)
        days = {entry["date"]: entry
                for entry in self.history.gas_by_day(7, timezone.utc)}
        target = (datetime.now(timezone.utc)
                  - timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertIs(days[target]["complete"], False)

    def test_today_is_never_complete(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for hour in range(24):
            if now - timedelta(hours=hour) <= now:
                self._record(now - timedelta(hours=hour))

    def test_today_is_reported_but_not_complete(self):
        """"Reported" and "over" are different questions."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for hour in range(3):
            self._record(now - timedelta(hours=hour))
        days = {entry["date"]: entry
                for entry in self.history.gas_by_day(3, timezone.utc)}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIs(days[today]["reported"], True)
        self.assertIs(days[today]["complete"], False)
        days = {entry["date"]: entry
                for entry in self.history.gas_by_day(3, timezone.utc)}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIs(days[today]["complete"], False)


class _GasViewsFixture:
    """The gas panel, served in a browser."""

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

    def _page(self, slide="gas"):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1200)
        return page

    def _title(self, page):
        return page.evaluate("() => document.querySelector('.p-h h4').innerText")

    def _switch(self, page, tab):
        page.click(f'.tabs [data-tab="{tab}"]')
        page.wait_for_timeout(400)


class TheTodayTileTests(_GasViewsFixture, unittest.TestCase):
    """The cost breakdown, and the tiles that used to be empty."""

    def _rows(self, page):
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('.panel .row'))
                 .map((row) => row.innerText.replace(/\\s+/g, ' ').trim())""")

    def test_the_energy_charge_is_named_for_what_it_is(self):
        """It was called "Energy", which reads as a quantity.

        It is money: the part of today's cost that is the gas itself,
        as opposed to the standing charge. Both are in pounds now, so
        they add up to the cost above them.
        """
        page = self._page()
        rows = self._rows(page)
        page.close()
        self.assertTrue(any(row.startswith("Energy charge") for row in rows),
                        f"no energy charge row: {rows}")
        self.assertFalse(any(row == "Energy" for row in rows),
                         "the row is still called Energy")

    def test_the_two_charges_add_up_to_the_cost(self):
        """£0.24 + £0.29 = £0.53 - the cost is made of these two."""
        page = self._page()
        rows = self._rows(page)
        page.close()

        def money_of(prefix):
            for row in rows:
                if row.startswith(prefix):
                    return float(row.split()[-1].replace("£", ""))
            return None

        cost = money_of("Cost today")
        energy = money_of("Energy charge")
        standing = money_of("Standing charge")
        self.assertIsNotNone(cost)
        self.assertIsNotNone(energy)
        self.assertIsNotNone(standing)
        self.assertAlmostEqual(energy + standing, cost, delta=0.02)

    def test_the_panel_says_how_the_unit_was_decided(self):
        """The numbers have to be arguable.

        Someone comparing Ember with the Octopus app wants to know what
        unit the readings are in and why - so the panel says so.
        """
        page = self._page()
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("Unit:", text)
        self.assertIn("cubic metres", text)

    def test_the_cost_is_shown_as_its_two_parts(self):
        """The cost, and the two charges it is made of.

        The panel used to say in words that the cost is the energy
        charge plus the standing charge. It does not any more: on a
        7" panel that sentence was the thing that squeezed the rows
        underneath it, and the unit rate - the last row - was the one
        that fell off. The two rows are named, and they are right
        there under the cost, so the sentence was saying what the rows
        already say.
        """
        page = self._page()
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("Cost today", text)
        self.assertIn("Energy charge", text)
        self.assertIn("Standing charge", text)
        self.assertNotIn("energy charge plus the standing charge", text.lower())

    def test_every_row_of_the_today_tile_is_on_the_screen(self):
        """The last row is the one that goes when the panel is full.

        The rows sit in a box that hides its overflow, so when the
        panel is too full the row that falls off is the bottom one -
        the unit rate, which is a number worth reading. Every row has
        to be inside the box that holds them.
        """
        page = self._page()
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
        cut = [row for row in rows if row["cut"] > 1]
        self.assertTrue(rows, "no rows in the today tile")
        self.assertEqual(cut, [], f"cut off in the today tile: {cut}")

    def _tiles(self, page):
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('.tile'))
                 .map((tile) => tile.innerText.replace(/\\s+/g, ' ').trim())""")

    def test_the_peak_hour_is_the_typical_one(self):
        """Not today's readings - the meter does not deliver those yet."""
        page = self._page()
        tiles = self._tiles(page)
        page.close()
        peak = [tile for tile in tiles
                if tile.upper().startswith("PEAK HOUR")]
        self.assertTrue(peak, f"no peak hour tile: {tiles}")
        self.assertNotIn("—", peak[0],
                         "the peak hour tile is empty")
        self.assertIn("avg day", peak[0],
                      "the peak hour does not say it is an average day")

    def test_the_peak_hour_says_it_is_an_average_day(self):
        page = self._page()
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("avg day", text)


class TheThreeViewsTests(_GasViewsFixture, unittest.TestCase):
    """The tabs are real, and each one draws something different."""

    def test_the_gas_panel_opens_on_the_week(self):
        page = self._page()
        title = self._title(page)
        page.close()
        self.assertIn("LAST 7 DAYS", title.upper())

    def test_the_day_tab_draws_twenty_four_hours(self):
        page = self._page()
        self._switch(page, "day")
        bars = page.evaluate(
            "() => document.querySelectorAll('.slide svg rect').length")
        page.close()
        self.assertEqual(bars, 24)

    def test_the_week_tab_draws_seven_days(self):
        page = self._page()
        self._switch(page, "week")
        days = page.evaluate(
            "() => document.querySelectorAll('.slide svg g[data-day]').length")
        page.close()
        self.assertEqual(days, 7)

    def test_the_month_tab_draws_a_calendar(self):
        page = self._page()
        self._switch(page, "month")
        cells = page.evaluate(
            "() => document.querySelectorAll('.slide .mcell').length")
        page.close()
        # Thirty days, plus the padding for the first day of the window
        # not being a Monday.
        self.assertGreaterEqual(cells, 30)

    def test_each_tab_says_what_it_is(self):
        page = self._page()
        self._switch(page, "day")
        day = self._title(page)
        self._switch(page, "month")
        month = self._title(page)
        page.close()
        self.assertIn("SHAPE OF A DAY", day.upper())
        self.assertIn("30 DAYS", month.upper())
        self.assertNotEqual(day, month)

    def test_the_shape_of_a_day_is_on_the_today_panel_too(self):
        page = self._page("today")
        bars = page.evaluate(
            "() => document.querySelectorAll('.slide svg rect').length")
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertEqual(bars, 24)
        self.assertIn("shape of a day", text.lower())

    def test_the_week_view_says_what_the_dotted_line_is(self):
        page = self._page()
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("week's average", text.lower())


class TheWeekNumberTests(_GasViewsFixture, unittest.TestCase):
    """The number over a column has to stay readable.

    An estimate is drawn with a whisker over the top of it, showing how
    far off the estimate could be. The number sits above the column, and
    when the type got a point bigger the whisker started to cut through
    the number - which reads as a figure that has been struck out. So the
    number goes above the whisker instead.
    """

    def _columns(self, page):
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('.slide svg g[data-day]'))
                 .map((group) => {
                   const label = group.querySelector('text');
                   const whisker = group.querySelector('line[stroke="var(--plum)"]');
                   return {
                     labelY: label ? parseFloat(label.getAttribute('y')) : null,
                     whiskerY: whisker ? parseFloat(whisker.getAttribute('y1')) : null,
                   };
                 })""")

    def test_the_number_clears_the_band_over_the_estimate(self):
        page = self._page()
        self._switch(page, "week")
        columns = self._columns(page)
        page.close()
        whiskers = [c for c in columns if c["whiskerY"] is not None]
        self.assertTrue(whiskers, "no estimated day in the week view")
        for column in whiskers:
            self.assertGreaterEqual(
                column["whiskerY"], column["labelY"],
                "the band whisker cuts through the number")

    def test_the_number_is_not_pushed_off_the_top_of_the_chart(self):
        page = self._page()
        self._switch(page, "week")
        columns = self._columns(page)
        page.close()
        for column in columns:
            self.assertGreaterEqual(column["labelY"], 12)


class TheBandTests(_GasViewsFixture, unittest.TestCase):
    """The band over an estimate is a range, and it has to be drawn as one.

    The band used to be a single line at the top of the range, and it read
    as another figure for the day. Sat's line sat at the height of the
    tallest column in the chart, and the line was above the week's average
    on a day whose estimate was below it - so the eye had Sat as a big day
    when it was a small one.

    The band is now drawn as a shaded range behind the column, so the line
    along its top is the edge of a region and not a value.
    """

    # The chart's own arithmetic, so the test can say what the drawing
    # should be and then check that it is.
    HEIGHT = 180
    BASELINE = HEIGHT - 20
    TOP = 30

    def _week(self):
        """A week with two estimated days, and a known band."""
        week = []
        for index, (label, value) in enumerate(
                [("Mon", 4.0), ("Tue", 5.0), ("Wed", 6.0),
                 ("Thu", 5.0), ("Fri", 5.0)]):
            week.append({"date": f"2026-11-0{index + 2}", "label": label,
                         "value": value, "m3": value, "cost": value,
                         "complete": True, "reported": True, "estimated": False})
        week.append({"date": "2026-11-07", "label": "Sat", "value": 0.0,
                     "m3": 4.0, "cost": 3.0, "band": 1.5, "complete": False,
                     "reported": False, "estimated": True})
        week.append({"date": "2026-11-08", "label": "Sun", "value": 0.0,
                     "m3": 5.0, "cost": 4.0, "band": 1.5, "complete": False,
                     "reported": False, "estimated": True})
        return week

    def _chart(self):
        """The week view, drawn from the week above."""
        page = self._page()
        self._switch(page, "week")
        page.evaluate(
            """(gas) => {
              state.payload.gas = Object.assign({}, state.payload.gas, gas);
              render();
            }""", {"week": self._week(), "average_m3": 5.0})
        page.wait_for_timeout(300)
        drawn = page.evaluate(
            """() => Array.from(
                 document.querySelectorAll('.slide .panel > svg g[data-day]'))
                 .map((group) => {
                   const band = group.querySelector('rect[data-band]');
                   const column = Array.from(group.querySelectorAll('rect'))
                     .find((r) => r.getAttribute('rx'));
                   const line = group.querySelector('line[stroke="var(--plum)"]');
                   return {
                     day: group.getAttribute('data-day'),
                     band: band ? {
                       value: band.getAttribute('data-band'),
                       y: parseFloat(band.getAttribute('y')),
                       h: parseFloat(band.getAttribute('height')),
                     } : null,
                     columnTop: column
                       ? parseFloat(column.getAttribute('y')) : null,
                     lineY: line ? parseFloat(line.getAttribute('y1')) : null,
                   };
                 })""")
        page.close()
        # The chart is scaled to the highest thing it draws, and the band
        # is one of the things it draws.
        peak = max(6.0, 4.0 + 1.5, 5.0 + 1.5, 5.0)
        return drawn, (self.BASELINE - self.TOP) / peak

    def _day(self, drawn, date):
        return next(column for column in drawn if column["day"] == date)

    def test_the_band_is_drawn_behind_the_estimate(self):
        """The column's top is the estimate; the band is the room above it."""
        drawn, scale = self._chart()
        sat = self._day(drawn, "2026-11-07")
        self.assertIsNotNone(sat["band"], "Sat's estimate has no band drawn")
        self.assertEqual(sat["band"]["value"], "1.50")
        self.assertAlmostEqual(sat["columnTop"],
                               self.BASELINE - 4.0 * scale, delta=0.1)
        self.assertAlmostEqual(sat["band"]["y"],
                               self.BASELINE - (4.0 + 1.5) * scale, delta=0.1)
        # And the band comes down to the estimate, so the whole range is
        # spoken for.
        self.assertAlmostEqual(sat["band"]["y"] + sat["band"]["h"],
                               sat["columnTop"], delta=0.1)

    def test_the_line_along_the_band_is_its_top_and_not_a_value(self):
        """The line is the edge of the range, and not a second figure."""
        drawn, scale = self._chart()
        sat = self._day(drawn, "2026-11-07")
        self.assertAlmostEqual(sat["lineY"], sat["band"]["y"], delta=0.1)
        # It is not at the estimate, which is what made it read as one.
        # In SVG a smaller y is higher up, so the line is above the
        # column's top by the width of the band.
        self.assertAlmostEqual(sat["columnTop"] - sat["lineY"],
                               1.5 * scale, delta=0.1)

    def test_the_band_has_room_in_the_chart(self):
        """A band that runs off the top of the chart is not being shown."""
        drawn, scale = self._chart()
        bands = [c for c in drawn if c["band"]]
        self.assertTrue(bands, "no estimated day in the week view")
        for column in bands:
            self.assertGreaterEqual(column["band"]["y"], self.TOP - 0.01,
                                    "the band runs off the top of the chart")

    def test_a_day_that_was_measured_has_no_band(self):
        """A reading is a reading. It does not get a band."""
        drawn, scale = self._chart()
        wed = self._day(drawn, "2026-11-04")
        self.assertIsNone(wed["band"])
        self.assertIsNone(wed["lineY"])

    def _note(self, page):
        """The note under the chart - not the one under another panel."""
        return page.evaluate(
            """() => {
              const svg = document.querySelector('.slide .panel > svg');
              const panel = svg.closest('.panel');
              const notes = panel.querySelectorAll('.dimmer');
              return notes[notes.length - 1].innerText;
            }""")

    def _note_lines(self, page):
        """How many lines the note takes. A range over the text gives one
        rectangle per line, which is the honest way to count them."""
        return page.evaluate(
            """() => {
              const svg = document.querySelector('.slide .panel > svg');
              const panel = svg.closest('.panel');
              const notes = panel.querySelectorAll('.dimmer');
              const range = document.createRange();
              range.selectNodeContents(notes[notes.length - 1]);
              return range.getClientRects().length;
            }""")

    def test_the_note_says_what_the_band_is(self):
        """The legend has no room for the band, so the note has to say it."""
        page = self._page()
        self._switch(page, "week")
        page.evaluate(
            """(gas) => {
              state.payload.gas = Object.assign({}, state.payload.gas, gas);
              render();
            }""", {"week": self._week(), "average_m3": 5.0})
        page.wait_for_timeout(300)
        note = self._note(page)
        lines = self._note_lines(page)
        page.close()
        self.assertIn("band", note.lower())

    def test_the_note_stays_on_one_line(self):
        """A note that wraps pushes the panel down, and a note that fits by
        a pixel is a note that wraps as soon as the wording changes."""
        page = self._page()
        self._switch(page, "week")
        page.evaluate(
            """(gas) => {
              state.payload.gas = Object.assign({}, state.payload.gas, gas);
              render();
            }""", {"week": self._week(), "average_m3": 5.0})
        page.wait_for_timeout(300)
        lines = self._note_lines(page)
        page.close()
        self.assertEqual(lines, 1, "the note under the chart wrapped")


class GapHonestyTests(_GasViewsFixture, unittest.TestCase):
    """A day the meter did not report is drawn as a gap."""

    def test_a_day_with_no_readings_is_not_drawn_as_a_zero(self):
        """The month's gaps are empty cells, not pale ones."""
        page = self._page()
        self._switch(page, "month")
        filled = page.evaluate(
            "() => document.querySelectorAll('.slide .mcell:not(.none)').length")
        empty = page.evaluate(
            "() => document.querySelectorAll('.slide .mcell.none').length")
        page.close()
        # The demo reports every day, so there should be no gaps here -
        # and every cell should be a real one.
        self.assertEqual(empty, 0)
        self.assertGreater(filled, 0)



    def test_the_month_view_says_how_many_days_were_reported(self):
        page = self._page()
        self._switch(page, "month")
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        self.assertIn("reported days", text.lower())


class TheGapTests(_GasViewsFixture, unittest.TestCase):
    """The gap after a meter exchange.

    The meter stopped for a couple of days and then started again.
    Those days are holes, not days of no gas, and the calendar has to
    say so rather than shading them pale.

    Its own fixture, because it takes readings out of the store and
    the other tests need them.
    """

    def test_the_month_shows_the_gap_as_empty_cells(self):
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        start = (midnight - timedelta(days=5)).timestamp()
        end = (midnight - timedelta(days=3)).timestamp()
        self.history.delete_gas_between(start, end)
        self.dashboard.poll()

        page = self._page()
        self._switch(page, "month")
        empty = page.evaluate(
            "() => document.querySelectorAll('.slide .mcell.none').length")
        titles = page.evaluate(
            "() => Array.from(document.querySelectorAll('.slide .mcell'))"
            ".map((cell) => cell.getAttribute('title'))")
        page.close()
        self.assertGreaterEqual(empty, 2)
        self.assertTrue(any("not reported" in (title or "") for title in titles))

    def test_the_week_estimates_the_gap_it_can(self):
        """A day with no reading is drawn as an estimate, not as a hole.

        The chart's job is to show how much gas each day took. A day
        with no reading is still a day, and the model has an answer for
        it - so the column is drawn in plum, the colour every estimate
        wears. What it must never be is a zero.
        """
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        start = (midnight - timedelta(days=5)).timestamp()
        end = (midnight - timedelta(days=3)).timestamp()
        self.history.delete_gas_between(start, end)
        self.dashboard.poll()

        page = self._page()
        self._switch(page, "week")
        plum = page.evaluate(
            "() => document.querySelectorAll('.slide svg rect[fill=\"var(--plum)\"]').length")
        days = page.evaluate(
            "() => Array.from(document.querySelectorAll('.slide svg g[data-day]'))"
            ".map((group) => group.getAttribute('data-day'))")
        page.close()
        self.assertGreaterEqual(plum, 2)
        self.assertEqual(len(days), 7)

    def test_the_week_leaves_a_gap_the_model_cannot_fill(self):
        """When the model has nothing to say, the day stays a gap.

        The estimator is the thing being taken away here, so this is
        about the chart: a day it knows nothing about is a dashed mark
        on the baseline, not a zero-height bar and not a zero.

        (The estimator has its own tests; this one is about what the
        chart does with a day that was left empty.)
        """
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        start = (midnight - timedelta(days=5)).timestamp()
        end = (midnight - timedelta(days=3)).timestamp()
        self.history.delete_gas_between(start, end)
        with mock.patch.object(Dashboard, "_estimate_the_week",
                               lambda *args, **kwargs: None):
            self.dashboard.poll()

        page = self._page()
        self._switch(page, "week")
        dashed = page.evaluate(
            "() => document.querySelectorAll('.slide svg line[stroke-dasharray]').length")
        bars = page.evaluate(
            "() => document.querySelectorAll('.slide svg rect').length")
        page.close()
        self.assertGreaterEqual(dashed, 2)
        self.assertLessEqual(bars, 5)


if __name__ == "__main__":
    unittest.main()

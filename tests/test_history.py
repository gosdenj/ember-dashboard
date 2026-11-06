"""Tests for the history store.

This is the thing that makes heating hours and heating history exist at
all, so it gets tested properly - especially around days that are 23 or
25 hours long, because a British Gas day is not always a day.
"""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ember.history import History, day_bounds, day_length, midnight
from ember.octopus import Octopus

LONDON = ZoneInfo("Europe/London")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        self.history = History(self.path)

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    # -- writing ----------------------------------------------------------

    def test_a_poll_is_recorded(self):
        self.history.record_poll(1000.0, True, 19.5, 4.2)
        row = self.history.latest_poll()
        self.assertIsNotNone(row)
        self.assertEqual(row["boiler_on"], 1)
        self.assertAlmostEqual(row["home_temp"], 19.5)
        self.assertAlmostEqual(row["outside_temp"], 4.2)

    def test_gas_readings_are_upserted_so_a_revision_wins(self):
        self.history.record_gas([{"ts": 100.0, "m3": 0.5, "provisional": True}])
        self.history.record_gas([{"ts": 100.0, "m3": 0.52, "provisional": False}])
        readings = self.history.gas_readings_between(0, 1000)
        self.assertEqual(len(readings), 1)
        self.assertAlmostEqual(readings[0]["value"], 0.52)
        self.assertEqual(readings[0]["provisional"], 0)

    def test_trv_readings_are_stored_per_room(self):
        self.history.record_trvs(1000.0, [
            {"room": "Lounge", "target": 19.0, "current": 19.6, "valve_open": False},
            {"room": "Kitchen", "target": 20.0, "current": 20.3, "valve_open": True},
        ])
        latest = self.history.latest_trvs()
        self.assertEqual(set(latest), {"Lounge", "Kitchen"})
        self.assertEqual(latest["Kitchen"]["valve_open"], 1)

    # -- heating minutes ---------------------------------------------------

    def _polls(self, on_until, until, step=120.0, on=True):
        """Polls every `step` seconds, with the boiler on until `on_until`.

        The real poll interval is two minutes; a fixture that polls every
        ten minutes is a fixture for a dashboard that was not running.
        """
        stamp = 0.0
        while stamp <= until:
            self.history.record_poll(stamp, on and stamp < on_until,
                                     19.0, 5.0)
            stamp += step

    def test_heating_minutes_counts_the_on_periods(self):
        # The boiler is on from 0 to 600, off from 600 to 1200.
        self._polls(600.0, 1200.0)
        minutes = self.history.heating_minutes(0.0, 1200.0)
        self.assertAlmostEqual(minutes, 10.0, delta=0.2)

    def test_heating_minutes_never_counts_more_than_the_window(self):
        self._polls(3600.0, 3600.0)
        minutes = self.history.heating_minutes(0.0, 600.0)
        self.assertAlmostEqual(minutes, 10.0, delta=0.2)

    def test_no_polls_means_no_heating(self):
        self.assertEqual(self.history.heating_minutes(0.0, 1000.0), 0.0)

    def test_a_gap_ember_was_not_watching_is_not_counted(self):
        """We only know the state at the moments we polled.

        Two-minute polls for half an hour, then an hour Ember was not
        running, then more polls. The break carries no information, so
        the heating inside it cannot be counted - and assuming the
        boiler stayed on for the whole of it is not an estimate, it is
        an invention.
        """
        stamp = 0.0
        while stamp <= 1800:
            self.history.record_poll(stamp, True, 19.0, 5.0)
            stamp += 120
        stamp = 5400.0
        while stamp <= 7200:
            self.history.record_poll(stamp, True, 19.0, 5.0)
            stamp += 120
        minutes = self.history.heating_minutes(0.0, 7200.0)
        # The half hour before the break and the half hour after it.
        # The hour in the middle is not counted.
        self.assertAlmostEqual(minutes, 60.0, delta=1.0)

    # -- daily totals ------------------------------------------------------

    def test_gas_by_day_sums_the_readings(self):
        """Yesterday's readings add up to yesterday's total.

        Anchored to yesterday rather than to a date written down, so the
        test does not quietly stop testing anything the day after the
        day it was written on.
        """
        yesterday = midnight(timezone.utc) - timedelta(days=1)
        readings = []
        for index in range(4):
            readings.append({"ts": (yesterday + timedelta(hours=index)).timestamp(),
                             "m3": 0.25, "provisional": False})
        self.history.record_gas(readings)
        days = self.history.gas_by_day(2, timezone.utc)
        match = [d for d in days
                 if d["date"] == yesterday.strftime("%Y-%m-%d")]
        self.assertEqual(len(match), 1)
        self.assertAlmostEqual(match[0]["value"], 1.0, delta=0.001)

    def test_a_clock_change_day_is_the_right_length(self):
        """The 25th of October 2026 is 25 hours long in Britain."""
        self.assertEqual(day_length(datetime(2026, 10, 25, tzinfo=LONDON)), 25 * 3600)
        self.assertEqual(day_length(datetime(2026, 3, 29, tzinfo=LONDON)), 23 * 3600)
        self.assertEqual(day_length(datetime(2026, 11, 4, tzinfo=LONDON)), 24 * 3600)

    def test_a_day_of_readings_is_not_lost_to_the_clock_change(self):
        long_day = datetime(2026, 10, 25, tzinfo=LONDON)
        start, end = day_bounds(long_day)
        readings = [{"ts": start.timestamp() + index * 3600, "m3": 0.1,
                     "provisional": False} for index in range(25)]
        self.history.record_gas(readings)
        days = self.history.gas_by_day(20, LONDON)
        match = [d for d in days if d["date"] == long_day.strftime("%Y-%m-%d")]
        self.assertEqual(len(match), 1)
        self.assertAlmostEqual(match[0]["value"], 2.5, delta=0.001)

    def test_the_day_bounds_end_is_the_next_midnight(self):
        long_day = datetime(2026, 10, 25, tzinfo=LONDON)
        start, end = day_bounds(long_day)
        self.assertEqual(end.date().isoformat(), "2026-10-26")
        self.assertEqual(end.hour, 0)
        self.assertEqual(end.utcoffset(), timedelta(0),
                         "the 26th is GMT, not BST")

    def test_partial_day_compares_like_for_like(self):
        """The same time of day, on each day.

        The comparison is made at two in the afternoon, and said so
        outright, rather than depending on what time the test happens to
        run.
        """
        moment = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
        for day_offset in range(3):
            day = datetime(2026, 11, 4, tzinfo=timezone.utc) - timedelta(days=day_offset)
            readings = []
            for index in range(14):
                readings.append({"ts": (day + timedelta(hours=index)).timestamp(),
                                 "m3": 0.1, "provisional": False})
            self.history.record_gas(readings)
        partial = self.history.gas_partial_by_day(3, timezone.utc, now=moment)
        self.assertEqual(len(partial), 3)
        for entry in partial:
            self.assertAlmostEqual(entry["value"], 1.4, delta=0.001)

    def test_a_partial_day_excludes_the_rest_of_the_day(self):
        """The reading after the cut-off is not counted."""
        moment = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
        day = datetime(2026, 11, 4, tzinfo=timezone.utc)
        readings = [{"ts": (day + timedelta(hours=index)).timestamp(),
                     "m3": 0.1, "provisional": False} for index in range(24)]
        self.history.record_gas(readings)
        partial = self.history.gas_partial_by_day(1, timezone.utc, now=moment)
        self.assertAlmostEqual(partial[0]["value"], 1.4, delta=0.001)

    # -- pruning -----------------------------------------------------------

    def test_pruning_drops_old_polls(self):
        import time
        self.history.record_poll(time.time() - 100 * 86400, True, 19.0, 5.0)
        self.history.record_poll(time.time(), True, 19.0, 5.0)
        removed = self.history.prune(days=30)
        self.assertEqual(removed, 1)
        self.assertIsNotNone(self.history.latest_poll())


if __name__ == "__main__":
    unittest.main()


class ReadingShapeTests(unittest.TestCase):
    """The reading that came straight from the API.

    On 2026-11-04 the app crashed on its first live gas poll because the
    API calls the number 'consumption' and the history store was looking
    for 'm3'. These are the tests that stop that happening again.
    """

    def setUp(self):
        self.path = Path(tempfile.gettempdir()) / "ember-test-shape.db"
        if self.path.exists():
            self.path.unlink()
        self.history = History(self.path)

    def tearDown(self):
        self.history.close()
        if self.path.exists():
            self.path.unlink()

    def test_a_reading_from_the_api_is_stored(self):
        row = Octopus._to_reading({
            "interval_start": "2026-11-04T00:00:00Z",
            "interval_end": "2026-11-04T00:30:00Z",
            "consumption": 0.0123,
        })
        self.assertEqual(self.history.record_gas([row]), 1)
        readings = self.history.gas_readings_between(0, time.time())
        self.assertEqual(len(readings), 1)
        self.assertAlmostEqual(readings[0]["value"], 0.0123)

    def test_a_reading_under_any_of_the_usual_names_is_stored(self):
        for index, name in enumerate(("value", "m3", "consumption")):
            self.history.record_gas([{"ts": 1700000000.0 + index * 1800, name: 2.5}])
        total = self.history.gas_total_between(1699000000, 1700100000)
        self.assertAlmostEqual(total, 7.5)

    def test_a_reading_that_makes_no_sense_is_skipped_not_fatal(self):
        self.history.record_gas([{"ts": 1700000000.0, "value": 1.0}])
        written = self.history.record_gas([
            {"ts": 1700003600.0},                 # no number at all
            {"value": 2.0},                        # no timestamp
            {"ts": "not a time", "value": 3.0},    # a timestamp that is not one
            {"ts": 1700007200.0, "value": "x"},    # a number that is not one
        ])
        self.assertEqual(written, 0)
        # The good row is still there.
        self.assertAlmostEqual(
            self.history.gas_total_between(1699000000, 1700100000), 1.0)


class MigrationTests(unittest.TestCase):
    """A database from before the column was renamed."""

    def test_an_old_database_is_brought_up_to_date(self):
        import sqlite3
        path = Path(tempfile.gettempdir()) / "ember-test-old.db"
        if path.exists():
            path.unlink()
        db = sqlite3.connect(str(path))
        db.executescript(
            "CREATE TABLE gas (ts REAL PRIMARY KEY, m3 REAL NOT NULL,"
            " provisional INTEGER NOT NULL DEFAULT 1);"
            "INSERT INTO gas VALUES (1700000000, 2.5, 0);")
        db.commit()
        db.close()

        history = History(path)
        try:
            # The reading that was already there is kept.
            self.assertAlmostEqual(
                history.gas_total_between(1699000000, 1700100000), 2.5)
            # And a new one can be written.
            history.record_gas([{"ts": 1700003600.0, "value": 1.25}])
            self.assertAlmostEqual(
                history.gas_total_between(1699000000, 1700100000), 3.75)
        finally:
            history.close()
            if path.exists():
                path.unlink()


class TodayIsNotTheFutureTests(unittest.TestCase):
    """Today's heating total must stop at now.

    The day summary used to run to midnight, so the last poll was
    assumed to describe the rest of the day. That put heating time
    into today's total that had not happened yet, and made the total
    disagree with the "heating today" figure shown beside it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.db")
        tz = timezone.utc
        self.midnight = datetime.now(tz).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        self.now = self.midnight.timestamp() + 3600 * 10  # ten in the morning
        # A poll every two minutes, all with the boiler on - which is
        # what the dashboard actually does when it is running.
        stamp = self.now - 3600
        while stamp <= self.now:
            self.history.record_poll(stamp, True, 17.0, 4.0)
            stamp += 120

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def test_today_stops_at_now(self):
        days = self.history.heating_minutes_by_day(2, timezone.utc,
                                                   now=self.now)
        today = days[-1]
        # One hour of heating: from the first poll to now. The second
        # poll is at now, so it adds nothing on its own.
        self.assertAlmostEqual(today["minutes"], 60.0, delta=1.0)

    def test_a_past_day_is_not_cut_short(self):
        days = self.history.heating_minutes_by_day(2, timezone.utc,
                                                   now=self.now)
        yesterday = days[0]
        # Yesterday had no polls at all, so no heating.
        self.assertEqual(yesterday["minutes"], 0.0)

    def test_it_matches_the_figure_shown_beside_it(self):
        """The panel's "heating today" and today's tile must agree."""
        today_minutes = self.history.heating_minutes(self.midnight.timestamp(),
                                                     self.now)
        days = self.history.heating_minutes_by_day(2, timezone.utc,
                                                   now=self.now)
        self.assertAlmostEqual(days[-1]["minutes"], today_minutes, delta=1.0)


class TheWatchedCoverageTests(unittest.TestCase):
    """How much of a day Ember was actually watching.

    Hive measures the boiler's run time itself. Ember counts it from
    its own polls, so a period when Ember was not running is missing
    from the total - and the total looks like a whole day's heating.

    A lower bound has to say that it is one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.db")
        tz = timezone.utc
        self.midnight = datetime.now(tz).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        self.start = self.midnight.timestamp()

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _day(self, now=None):
        days = self.history.heating_minutes_by_day(1, timezone.utc,
                                                   now=now or (self.start + 86400))
        return days[-1]

    def test_a_day_with_no_polls_is_not_covered(self):
        day = self._day()
        self.assertFalse(day["recorded"])
        self.assertEqual(day["watched_minutes"], 0.0)
        self.assertFalse(day["complete"])

    def test_a_fully_watched_day_is_complete(self):
        """Every two minutes, all day."""
        for index in range(0, 24 * 60, 2):
            self.history.record_poll(self.start + index * 60, index % 120 == 0,
                                     17.0, 4.0)
        day = self._day()
        self.assertTrue(day["recorded"])
        self.assertTrue(day["complete"], "a fully watched day is not complete")
        self.assertGreater(day["watched_minutes"], 1400)

    def _day_with_a_break(self, on_after=False):
        """Two-minute polls, then an hour Ember was not running."""
        for index in range(0, 31, 2):
            self.history.record_poll(self.start + index * 60, True, 17.0, 4.0)
        for index in range(90, 120, 2):
            self.history.record_poll(self.start + index * 60, on_after,
                                     17.0, 4.0)

    def test_a_break_in_the_middle_is_not_watched(self):
        """The break is Ember not running.

        It carries no information about the boiler, and the heating
        inside it cannot be counted.
        """
        self._day_with_a_break(on_after=False)
        day = self._day()
        # The half hour before the break is counted; the break is not.
        self.assertAlmostEqual(day["minutes"], 30.0, delta=1.0)
        self.assertFalse(day["complete"],
                         "a day with a break was called complete")

    def test_slow_polls_are_still_watched(self):
        """A poll cycle that takes minutes is not a break.

        This is what a broken session does: every poll is slow, and
        the gaps are minutes rather than seconds. If the ceiling were
        a fixed number, every interval would look like a period Ember
        was not running - and the heating hours would be **zero**,
        which is what happened the first time this rule was written.
        """
        for index in range(0, 2 * 60, 5):
            self.history.record_poll(self.start + index * 60, index < 30,
                                     17.0, 4.0)
        day = self._day()
        self.assertGreater(day["minutes"], 25.0,
                           "slow polls were treated as a break")
        self.assertGreater(day["watched_minutes"], 100.0)

    def test_the_heating_total_is_a_lower_bound(self):
        """The boiler was on through the break; Ember did not see it."""
        self._day_with_a_break(on_after=True)
        day = self._day()
        # The half hour before the break and the half hour after it.
        # The hour in the middle is not counted: Ember was not there
        # to see it, so it cannot say the boiler was on.
        self.assertAlmostEqual(day["minutes"], 60.0, delta=1.0)
        self.assertLess(day["watched_minutes"], 70.0)
        self.assertFalse(day["complete"])


class BulkPollTests(unittest.TestCase):
    """Writing many polls at once.

    The demo seeds thirty days of half-hourly polls. A commit per
    poll took seconds; on a Raspberry Pi's SD card it was long enough
    to look like a hang.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.db")

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def test_the_rows_are_stored(self):
        rows = [(1700000000.0 + i * 1800, 1 if i % 2 else 0, 17.5, 4.5)
                for i in range(48)]
        written = self.history.record_polls(rows)
        self.assertEqual(written, 48)
        stored = self.history.polls_between(1700000000.0, 1700000000.0 + 48 * 1800)
        self.assertEqual(len(stored), 48)
        self.assertTrue(stored[0]["boiler_on"] is False or stored[0]["boiler_on"] == 0)

    def test_an_empty_batch_writes_nothing(self):
        self.assertEqual(self.history.record_polls([]), 0)

    def test_the_same_row_twice_keeps_the_last_one(self):
        self.history.record_polls([(1700000000.0, 0, 17.0, 4.0)])
        self.history.record_polls([(1700000000.0, 1, 18.0, 5.0)])
        row = self.history.latest_poll()
        self.assertEqual(row["home_temp"], 18.0)
        self.assertEqual(row["outside_temp"], 5.0)

"""The timestamp shapes Octopus actually writes.

On 2026-11-05 the dashboard died on its first live gas poll:

    ValueError: time data '2026-10-11T17:00:00+01:00' does not match
    format '%Y-%m-%dT%H:%M:%SZ'

Octopus is not consistent about the timezone it writes. Some rows come
back as "...Z" and some as "...+01:00" - the same moment, written the
way that day's settlement was. The parser only knew one of them, and
one odd row took the whole application down with it.

So these check two things: that every shape Octopus uses is read, and
that a row which cannot be read is left out and reported rather than
taking the poll down.
"""

import unittest
from datetime import datetime, timedelta, timezone

from ember.octopus import Octopus, _parse, _parse_moment


class TimestampTests(unittest.TestCase):
    """Every shape Octopus writes, read as the same moment."""

    def test_a_zulu_timestamp_is_read(self):
        self.assertEqual(_parse("2026-11-03T00:00:00Z"),
                         datetime(2026, 11, 3, tzinfo=timezone.utc))

    def test_an_offset_timestamp_is_read(self):
        """The shape that killed it."""
        self.assertEqual(_parse("2026-10-11T17:00:00+01:00"),
                         datetime(2026, 10, 11, 17, 0,
                                  tzinfo=timezone(timedelta(hours=1))))

    def test_an_offset_without_the_colon_is_read(self):
        self.assertEqual(_parse("2026-10-11T17:00:00+0100"),
                         datetime(2026, 10, 11, 17, 0,
                                  tzinfo=timezone(timedelta(hours=1))))

    def test_a_negative_offset_is_read(self):
        self.assertEqual(_parse("2026-10-11T17:00:00-05:00"),
                         datetime(2026, 10, 11, 17, 0,
                                  tzinfo=timezone(timedelta(hours=-5))))

    def test_fractional_seconds_are_read(self):
        self.assertEqual(_parse("2026-11-03T00:00:00.123Z"),
                         datetime(2026, 11, 3, 0, 0, 0, 123000,
                                  tzinfo=timezone.utc))

    def test_a_date_on_its_own_is_read(self):
        self.assertEqual(_parse("2026-11-03"),
                         datetime(2026, 11, 3, tzinfo=timezone.utc))

    def test_an_offset_timestamp_is_the_same_moment_as_its_utc_twin(self):
        """17:00+01:00 is 16:00Z - the same instant, written differently."""
        offset = _parse("2026-10-11T17:00:00+01:00")
        zulu = _parse("2026-10-11T16:00:00Z")
        self.assertEqual(offset.timestamp(), zulu.timestamp())

    def test_an_unreadable_timestamp_is_none_and_not_an_exception(self):
        self.assertIsNone(_parse("not a timestamp"))
        self.assertIsNone(_parse(""))
        self.assertIsNone(_parse(None))

    def test_the_two_parsers_agree(self):
        """One parser, so the two can never drift apart again."""
        for text in ("2026-11-03T00:00:00Z", "2026-10-11T17:00:00+01:00",
                     "2026-11-03T00:00:00.500Z", "2026-11-03"):
            self.assertEqual(_parse(text), _parse_moment(text), text)


class ConsumptionRowTests(unittest.TestCase):
    """A row is a reading, or it is nothing."""

    def test_a_row_with_an_offset_is_a_reading(self):
        reading = Octopus._to_reading({
            "consumption": 0.05,
            "interval_start": "2026-10-11T17:00:00+01:00",
            "interval_end": "2026-10-11T17:30:00+01:00",
        })
        self.assertIsNotNone(reading)
        self.assertAlmostEqual(
            reading["ts"],
            datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc).timestamp(),
            places=3)
        self.assertAlmostEqual(reading["value"], 0.05, places=6)

    def test_a_row_that_cannot_be_placed_in_time_is_nothing(self):
        """Not a reading at the epoch. A reading at 1970 is a lie."""
        reading = Octopus._to_reading({
            "consumption": 0.05,
            "interval_start": "the eleventh of October",
            "interval_end": "later",
        })
        self.assertIsNone(reading)

    def test_a_row_with_no_moment_is_nothing(self):
        self.assertIsNone(Octopus._to_reading({"consumption": 0.05}))

    def test_a_row_that_is_not_a_row_is_nothing(self):
        self.assertIsNone(Octopus._to_reading("a string"))
        self.assertIsNone(Octopus._to_reading(None))

    def test_the_consumption_call_keeps_the_good_rows_and_counts_the_rest(self):
        client = Octopus("sk_live_test", "A-3B178E7A")
        client.get = lambda path, params=None: {
            "results": [
                {"consumption": 0.05,
                 "interval_start": "2026-10-11T17:00:00+01:00",
                 "interval_end": "2026-10-11T17:30:00+01:00"},
                {"consumption": 0.04,
                 "interval_start": "2026-10-11T17:30:00+01:00",
                 "interval_end": "2026-10-11T18:00:00+01:00"},
                {"consumption": 0.99,
                 "interval_start": "whenever",
                 "interval_end": "later"},
            ],
        }
        readings = client.consumption("8920115204", "E6S13783422161",
                                      datetime(2026, 10, 1, tzinfo=timezone.utc),
                                      datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(len(readings), 2)
        self.assertEqual(client.skipped_readings, 1)
        self.assertIn("whenever", client.skipped_sample)

    def test_one_bad_row_does_not_take_the_fetch_down(self):
        client = Octopus("sk_live_test", "A-3B178E7A")

        def boom(path, params=None):
            raise RuntimeError("a shape nobody has seen before")

        client.get = boom
        with self.assertRaises(RuntimeError):
            client.consumption("8920115204", "E6S13783422161",
                               datetime(2026, 10, 1, tzinfo=timezone.utc),
                               datetime(2026, 10, 12, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()


class ThePollSurvivesItTests(unittest.TestCase):
    """The whole path, with the rows the user actually has.

    The crash happened in the live poll, so a test of the parser alone
    is not enough: the poll has to come out the other side with the
    readings it could read, and say so about the one it could not.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from ember.config import Settings
        from ember.dashboard import Dashboard
        from ember.history import History

        self.dir = tempfile.mkdtemp()
        self.settings = Settings(Path(self.dir) / "settings.json")
        self.settings.update({
            "octopus_api_key": "sk_live_test",
            "octopus_account": "A-3B178E7A",
            "gas_mprn": "8920115204",
            "gas_meter_serial": "E6S13783422161",
            "gas_meter_serials": "E6E09927562343,E6S13783422161",
            "gas_product_code": "OE-FIX-18M-26-09-11",
            "gas_tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "gas_meter_type": "SMETS2",
            "gas_unit": "auto",
        })
        self.history = History(Path(self.dir) / "history.db")
        self.dashboard = Dashboard(self.settings, self.history, None)

    def tearDown(self):
        self.history.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _poll_with(self, rows, boom=None, rates_boom=None):
        """Run a poll against a reply built from these rows.

        `boom` is raised from the consumption call and `rates_boom` from
        the rates call - neither is an OctopusError, so they stand for
        the shape surprise nobody has seen before.
        """
        import ember.dashboard as dashboard_module

        class Fake:
            def __init__(self, api_key, account):
                self.skipped_readings = 0
                self.skipped_sample = ""

            def find_tariff(self, mprn):
                return {"product_code": "OE-FIX-18M-26-09-11",
                        "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                        "standing_charge": 28.388535, "unit_rate": 8.4378,
                        "meter_type": "SMETS2", "consumption_units": "M3",
                        "serials": ["E6E09927562343", "E6S13783422161"]}

            def consumption(self, mprn, serial, start, end,
                            group_by="hour", page_size=5000):
                if boom is not None:
                    raise boom
                # Exactly what the real client does with these rows.
                readings = []
                skipped = 0
                for row in rows:
                    reading = Octopus._to_reading(row)
                    if reading is None:
                        skipped += 1
                        continue
                    readings.append(reading)
                self.skipped_readings = skipped
                return readings

            def rates_now(self, product, tariff):
                if rates_boom is not None:
                    raise rates_boom
                return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

            detect_unit = staticmethod(Octopus.detect_unit)

        original = dashboard_module.Octopus
        dashboard_module.Octopus = Fake
        try:
            payload = self.dashboard.poll()
        finally:
            dashboard_module.Octopus = original
        return payload

    def _rows(self, days=10, hours=24):
        """Ten days of hourly rows, written with an offset like the user's.

        Ten days, so the forecast model has enough history to fit - the
        point is not just that the poll survives but that the readings
        it did understand carry all the way through to the estimate.
        """
        offset = timezone(timedelta(hours=1))
        start = datetime.now(timezone.utc) - timedelta(days=days + 1)
        start = start.replace(minute=0, second=0, microsecond=0)
        rows = []
        for day in range(days):
            for hour in range(hours):
                moment = start + timedelta(days=day, hours=hour)
                rows.append({
                    "consumption": 0.05,
                    # The user's rows: written with an offset, not a Z.
                    "interval_start": moment.astimezone(offset).isoformat(),
                    "interval_end": (moment + timedelta(hours=1)).astimezone(
                        offset).isoformat(),
                })
        return rows

    def test_the_poll_survives_offset_timestamps(self):
        payload = self._poll_with(self._rows())
        gas = payload["gas"]
        # The unit was worked out from the readings, which is only
        # possible if they were read.
        self.assertEqual(gas["unit"], "m3")
        # The readings landed: 10 days of 24 hours at 0.05.
        self.assertAlmostEqual(self.history.gas_total_between(0, 2e9),
                               10 * 24 * 0.05, places=6)
        # And nothing was written at the epoch.
        self.assertGreater(self.history.oldest_gas_reading(), 1.0e9)

    def test_the_poll_survives_one_unreadable_row_and_says_so(self):
        rows = self._rows()
        rows.append({"consumption": 0.99, "interval_start": "whenever",
                     "interval_end": "later"})
        payload = self._poll_with(rows)
        octopus_errors = [e for e in payload["errors"] if e["source"] == "octopus"]
        self.assertTrue(octopus_errors, "the skipped row was not reported")
        self.assertIn("skipped", octopus_errors[0]["message"])

    def test_a_shape_surprise_in_the_readings_does_not_take_the_app_down(self):
        """Not an OctopusError - something nobody has seen before.

        The dashboard is a wall panel. One odd reply must not turn it
        into a blank screen with a traceback.
        """
        payload = self._poll_with(
            [], boom=RuntimeError("a shape nobody has seen before"))
        octopus_errors = [e for e in payload["errors"] if e["source"] == "octopus"]
        self.assertTrue(octopus_errors, "the failure was not reported")
        self.assertIn("shape nobody has seen", octopus_errors[0]["message"])

    def test_a_shape_surprise_in_the_rates_does_not_take_the_app_down(self):
        payload = self._poll_with(
            self._rows(), rates_boom=RuntimeError("the rates moved"))
        octopus_errors = [e for e in payload["errors"] if e["source"] == "octopus"]
        self.assertTrue(octopus_errors, "the failure was not reported")
        self.assertIn("the rates moved", octopus_errors[0]["message"])

    def test_an_unreadable_row_is_not_stored_at_the_epoch(self):
        rows = self._rows() + [{"consumption": 0.99, "interval_start": "whenever",
                                "interval_end": "later"}]
        self._poll_with(rows)
        oldest = self.history.oldest_gas_reading()
        self.assertIsNotNone(oldest)
        self.assertGreater(oldest, 1.0e9, "a reading landed in 1970")

"""The live path, end to end.

On 2026-11-04 the dashboard crashed on its first live gas poll:

    KeyError: 'm3'

The API calls the number 'consumption'. The history store was looking
for 'm3'. Nothing about the demo caught it, because the demo generates
its own readings.

So this walks the whole live path - settings, the client, the poll, the
history store and the payload the browser gets - with the API's real
reply shapes, and checks that it comes out the other side.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History
from ember.octopus import Octopus


def api_readings(count=96, value=0.05):
    """The rows the consumption endpoint actually returns.

    Anchored to yesterday, so there are always readings for today. A
    test that only passes on the day it was written on is not a test -
    this one passed on the 4th and failed on the 5th, for no reason
    connected to what it was checking.
    """
    start = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
             - timedelta(days=1))
    return {
        "count": count,
        "next": None,
        "previous": None,
        "results": [{
            "consumption": value,
            "interval_start": (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval_end": (start + timedelta(minutes=30 * (i + 1))).strftime("%Y-%m-%dT%H:%M:%SZ"),
        } for i in range(count)],
    }


class FakeOctopus:
    """Stands in for the real client, with the real reply shapes."""

    def __init__(self, api_key, account):
        self.api_key = api_key
        self.account = account

    def gas_agreements(self):
        return [{
            "mprn": "8920115204",
            "serial": "E6S13783422161",
            "serials": ["E6E09927562343", "E6S13783422161"],
            "meter_type": "SMETS2",
            "products": ["OE-FIX-18M-26-09-11"],
            "product_code": "OE-FIX-18M-26-09-11",
            "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "agreements": [],
            "valid_from": "2026-09-10T23:00:00Z",
            "valid_to": "",
        }]

    def current_gas_product(self):
        return "OE-FIX-18M-26-09-11", "G-1R-OE-FIX-18M-26-09-11-A"

    def find_tariff(self, mprn):
        return {"source": "the GraphQL API", "product_code": "OE-FIX-18M-26-09-01",
                "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                "standing_charge": 28.388535, "unit_rate": 8.4378,
                "meter_type": "SMETS2", "consumption_units": "M3",
                "serials": ["E6E09927562343", "E6S13783422161"], "meters": []}

    def consumption(self, mprn, serial, start, end, group_by="hour", page_size=5000):
        # The real client drops a row it cannot place in time and counts
        # it. The fake does the same, so a test never passes for a
        # reason the real thing would not.
        rows = [Octopus._to_reading(row) for row in api_readings()["results"]]
        return [row for row in rows if row is not None]

    def rates_now(self, product, tariff):
        return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

    detect_unit = staticmethod(Octopus.detect_unit)


class FakeHive:
    """Stands in for the signed-in Hive client.

    The shape comes from the real account: nine zones, twelve valves,
    no valve position anywhere.
    """

    def __init__(self, base=""):
        self.base = base
        # The real client has this, and the dashboard reads it to tell a
        # session that works from one that could not be renewed.
        self.last_error = ""

    def load_session(self, path):
        return True

    def read_state(self):
        rooms = []
        names = ["Dining Room", "Downstairs Hallway", "Kitchen", "Loft Room",
                 "Lounge", "Master Bedroom", "Matteo Bedroom", "Sofia Bedroom",
                 "Upstairs Hallway"]
        for name in names:
            rooms.append({
                "room": name,
                "current": 19.7 if name == "Sofia Bedroom" else 20.4,
                "target": 20.0,
                "calling": name == "Sofia Bedroom",
                "position": None,
                "valve_count": 2 if name in ("Downstairs Hallway", "Kitchen", "Lounge") else 1,
                "battery": 100,
                "mode": "SCHEDULE",
            })
        return {
            # The boiler is firing, so the history has something real
            # to record.
            "boiler_on": True,
            "home_temp": 17.55,
            "target_temp": 7.0,
            "boost_until": None,
            "rooms": rooms,
        }


class LiveHeatingPathTests(unittest.TestCase):
    """The live heating path, end to end.

    This exists because of a real bug: the code that wrote the heating
    history down was unreachable, so the live poll returned nothing and
    the app crashed on startup with a signed-in Hive. The demo hid it,
    because the demo takes a completely different path.

    So this signs in a fake Hive, polls, and checks that the poll comes
    back with the rooms in it and that the history was written.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.settings = Settings(data / "settings.json")
        self.settings.update({
            "hive_email": "someone@example.com",
            "hive_base": "",
            "latitude": 51.5,
            "longitude": -0.1,
            "postcode": "SW1A 1AA",
        })
        self.history = History(data / "history.db")

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _dashboard(self):
        import ember.dashboard as dashboard_module
        original = dashboard_module.Hive
        dashboard_module.Hive = FakeHive
        try:
            dashboard = Dashboard(self.settings, self.history, None)
            dashboard.poll()
            return dashboard.payload()
        finally:
            dashboard_module.Hive = original

    def test_the_live_poll_returns_the_rooms(self):
        """The poll must return a result, not None."""
        payload = self._dashboard()
        rooms = payload["heating"]["rooms"]
        self.assertEqual(len(rooms), 9)
        self.assertEqual(rooms[0]["room"], "Dining Room")
        self.assertTrue(rooms[7]["valve_open"])

    def test_the_heating_history_is_actually_written(self):
        """Without this, "heating hours" is always zero.

        A check that merely says "not None" would pass even with no
        history written at all, so this looks for the boiler's state in
        the store itself.
        """
        self._dashboard()
        rows = self.history.polls_between(time.time() - 3600, time.time())
        self.assertTrue(rows, "the poll was not written down")
        self.assertTrue(any(row["boiler_on"] for row in rows),
                        "the boiler's state was not written down")

    def test_heating_minutes_can_be_worked_out_from_it(self):
        """A poll every two minutes, with the boiler on.

        This is the sum that "2h 20m of heating" comes from, so it has
        to come out of the store the live path fills. The polls are two
        minutes apart because that is what the dashboard does - a gap
        that long is a period Ember was not running, and Ember cannot
        say what the boiler was doing then.
        """
        self._dashboard()
        now = time.time()
        stamp = now - 3600
        while stamp <= now:
            self.history.record_poll(stamp, True, 17.5, 5.0)
            stamp += 120
        minutes = self.history.heating_minutes(now - 3600, now)
        self.assertAlmostEqual(minutes, 60.0, delta=1.0)

    def test_a_gap_is_not_counted_as_heating(self):
        """The live path must not invent heating it never saw.

        A session that breaks for an hour is exactly what happened to
        this dashboard. If the boiler was on when the polls stopped and
        on when they resumed, the honest answer is "at least", not the
        whole hour.
        """
        self._dashboard()
        now = time.time()
        stamp = now - 5400
        while stamp <= now - 3600:
            self.history.record_poll(stamp, True, 17.5, 5.0)
            stamp += 120
        # An hour with no polls at all, then more.
        stamp = now - 1800
        while stamp <= now:
            self.history.record_poll(stamp, True, 18.0, 5.5)
            stamp += 120
        minutes = self.history.heating_minutes(now - 5400, now)
        # The half hour before the break and the half hour after it.
        # The hour in the middle is not counted.
        self.assertAlmostEqual(minutes, 60.0, delta=2.0)

    def test_the_panel_does_not_show_zero_heating(self):
        """The symptom, end to end: the panel said 0h 00m.

        The heating minutes were judged against a fixed five-minute
        gap, so a dashboard whose polls were slow - because its
        session needed renewing, say - judged every interval a period
        it had not been running, and every heating figure came out
        zero.
        """
        now = time.time()
        stamp = now - 3600
        while stamp <= now:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            # Ten minutes apart: a poll cycle that has gone slow
            # because the session needs renewing.
            stamp += 600
        payload = self._dashboard()
        minutes = payload["heating"]["heating_today_minutes"]
        self.assertIsNotNone(minutes, "the panel has no heating figure at all")
        self.assertGreater(minutes, 50.0, "the panel shows zero heating")

    def test_slow_polls_are_not_treated_as_a_break(self):
        """A poll cycle that takes minutes still counts.

        This is the bug that made every heating figure zero: the gap
        between two polls was judged against a fixed five minutes, so a
        dashboard whose polls were slow - because its session needed
        renewing, say - judged every single interval a period it had
        not been running.
        """
        self._dashboard()
        now = time.time()
        stamp = now - 3600
        while stamp <= now:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            stamp += 600
        minutes = self.history.heating_minutes(now - 3600, now)
        self.assertGreater(minutes, 50.0,
                           "slow polls were treated as a break")

    def test_a_break_in_a_slow_poll_cycle_is_still_watched(self):
        """Ten-minute polls, and a half-hour break in the middle.

        The ceiling is generous on purpose. Skipping a watched interval
        puts the heating to zero, which is a disaster; counting a break
        only over-counts a little. So a gap three times the typical one
        is still watched, and only a gap far longer than that is a
        period Ember was not running.
        """
        self._dashboard()
        now = time.time()
        stamp = now - 5400
        while stamp <= now - 3600:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            stamp += 600
        # A half-hour break.
        stamp = now - 1800
        while stamp <= now:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            stamp += 600
        minutes = self.history.heating_minutes(now - 5400, now)
        self.assertAlmostEqual(minutes, 90.0, delta=3.0)

    def test_one_fast_poll_does_not_collapse_the_ceiling(self):
        """A restart makes two polls land a second apart.

        The ceiling is worked out from the **typical** gap, so one fast
        poll cannot make every other interval look like a period Ember
        was not running - which would put the heating back to zero.
        """
        self._dashboard()
        now = time.time()
        stamp = now - 3600
        while stamp <= now - 600:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            stamp += 120
        # Two polls a second apart, as a restart would produce.
        self.history.record_poll(now - 599, True, 18.0, 5.0)
        self.history.record_poll(now - 598, True, 18.0, 5.0)
        stamp = now - 480
        while stamp <= now:
            self.history.record_poll(stamp, True, 18.0, 5.0)
            stamp += 120
        minutes = self.history.heating_minutes(now - 3600, now)
        self.assertGreater(minutes, 40.0,
                           "one fast poll collapsed the gap ceiling")

    def test_the_polls_are_in_the_store(self):
        self._dashboard()
        rows = self.history.polls_between(time.time() - 3600, time.time())
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("outside_temp", rows[0].keys())

    def test_the_outside_temperature_is_recorded_with_the_poll(self):
        """The degree-day model needs the cold as well as the heat."""
        self._dashboard()
        rows = self.history.polls_between(time.time() - 3600, time.time())
        self.assertIsNotNone(rows[0]["outside_temp"])

    def test_the_valves_are_recorded(self):
        self._dashboard()
        readings = self.history.latest_trvs()
        self.assertEqual(len(readings), 9)

    def test_the_poll_does_not_crash_the_app(self):
        """The crash the user actually saw."""
        payload = self._dashboard()
        hive_errors = [e for e in payload["errors"] if e["source"] == "hive"]
        self.assertEqual(hive_errors, [])


class LiveGasPathTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(Path(self.dir) / "settings.json")
        self.settings.update({
            "octopus_api_key": "sk_live_test",
            "octopus_account": "A-3B178E7A",
            "gas_mprn": "8920115204",
            "gas_meter_serials": "E6E09927562343,E6S13783422161",
            "gas_product_code": "OE-FIX-18M-26-09-11",
            "gas_tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "gas_meter_type": "SMETS2",
            "gas_unit": "auto",
        })
        self.history = History(Path(self.dir) / "history.db")

    def tearDown(self):
        self.history.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_poll_survives_the_real_reply_shape(self):
        """The crash, fixed."""
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus
        dashboard_module.Octopus = FakeOctopus
        try:
            dashboard = Dashboard(self.settings, self.history, None)
            errors: list = []
            dashboard.poll()
        finally:
            dashboard_module.Octopus = original

        # The readings landed. The ones stamped after now are not counted,
        # because they have not happened yet.
        now = time_now()
        expected = sum(1 for row in api_readings()["results"]
                       if datetime.fromisoformat(row["interval_start"]
                                                 .replace("Z", "+00:00")).timestamp() < now)
        total = self.history.gas_total_between(0, now)
        self.assertGreater(expected, 40)
        self.assertAlmostEqual(total, expected * 0.05, places=6)

    def test_the_payload_the_browser_gets_is_complete(self):
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus
        dashboard_module.Octopus = FakeOctopus
        try:
            dashboard = Dashboard(self.settings, self.history, None)
            dashboard.poll()
            payload = dashboard.payload()
        finally:
            dashboard_module.Octopus = original

        gas = payload["gas"]
        self.assertEqual(gas["unit"], "m3")
        self.assertEqual(gas["unit_label"], "m³")
        self.assertEqual(gas["unit_rate_p"], 8.4378)
        # The standing charge is shown to a tenth of a penny but added in
        # full, so the cost is right.
        self.assertAlmostEqual(gas["standing_p"], 28.4, places=1)
        energy = gas["today_m3"] * gas["calorific"] * gas["unit_rate_p"]
        self.assertAlmostEqual(gas["cost_today"],
                               round((energy + 28.388535) / 100.0, 2), places=2)
        self.assertIsNotNone(gas["today_m3"])
        self.assertIsNotNone(gas["cost_today"])
        self.assertEqual(len(gas["hourly"]), 24)


def time_now():
    import time
    return time.time()


if __name__ == "__main__":
    unittest.main()

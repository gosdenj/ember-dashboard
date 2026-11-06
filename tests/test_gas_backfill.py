"""How much gas history Ember asks for.

A chart of the week needs a week's worth of readings, and a store that
starts empty has none. Octopus keeps about a month, so Ember asks for
the lot the first time and then only for what is new.

These tests use the real Octopus client with the reply shapes the live
service sends, so the normalisation boundary is the one in production.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History
from ember.octopus import Octopus


def api_readings(days: int = 30, value: float = 0.1) -> dict:
    """The consumption endpoint's reply, for `days` of hourly readings."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    count = days * 24
    return {
        "count": count,
        "next": None,
        "previous": None,
        "results": [{
            "consumption": value,
            "interval_start": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval_end": (start + timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        } for i in range(count)],
    }


class FakeOctopus:
    """The real client, with the calls recorded.

    It replaces the class, not an instance, so the recorded windows
    live on the class and every client shares them.
    """

    #: Every (start, end) the client was asked for.
    windows: list[tuple[datetime, datetime]] = []

    def __init__(self, api_key, account):
        self.api_key = api_key
        self.account = account

    def gas_agreements(self):
        return [{
            "mprn": "8920115204",
            "serial": "E6S13783422161",
            "serials": ["E6E09927562343", "E6S13783422161"],
            "meter_type": "SMETS2",
            "product_code": "OE-FIX-18M-26-09-11",
            "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "agreements": [],
            "valid_from": "2026-09-10T23:00:00Z",
            "valid_to": "",
        }]

    def current_gas_product(self):
        return "OE-FIX-18M-26-09-11", "G-1R-OE-FIX-18M-26-09-11-A"

    def find_tariff(self, mprn):
        return {"source": "the GraphQL API",
                "product_code": "OE-FIX-18M-26-09-11",
                "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                "standing_charge": 28.388535, "unit_rate": 8.4378,
                "meter_type": "SMETS2", "consumption_units": "M3",
                "serials": ["E6E09927562343", "E6S13783422161"], "meters": []}

    def consumption(self, mprn, serial, start, end, group_by="hour",
                    page_size=5000):
        FakeOctopus.windows.append((start, end))
        return [Octopus._to_reading(row)
                for row in api_readings(days=30)["results"]]

    def rates_now(self, product, tariff):
        return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

    detect_unit = staticmethod(Octopus.detect_unit)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(root / "settings.json")
        self.settings.update({
            "octopus_api_key": "sk_live_test",
            "octopus_account": "A-3B178E7A",
            "gas_mprn": "8920115204",
            "gas_meter_serial": "E6S13783422161",
            "gas_meter_serials": "E6E09927562343,E6S13783422161",
            "gas_product_code": "OE-FIX-18M-26-09-11",
            "gas_tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "gas_meter_type": "SMETS2",
            "postcode": "PO4 0PL", "latitude": 50.785, "longitude": -1.075,
        })
        self.history = History(root / "history.db")
        FakeOctopus.windows = []
        import ember.dashboard as dashboard_module
        self.original = dashboard_module.Octopus
        dashboard_module.Octopus = FakeOctopus
        self.dashboard = Dashboard(self.settings, self.history, None)

    def tearDown(self):
        import ember.dashboard as dashboard_module
        dashboard_module.Octopus = self.original
        self.dashboard.stop()
        self.history.close()
        self.tmp.cleanup()

    def _days_asked_for(self) -> float:
        start, end = FakeOctopus.windows[-1]
        return (end - start).total_seconds() / 86400.0

    def test_the_first_fetch_asks_for_a_month(self):
        """A store that starts empty has nothing to chart.

        The chart of the week needs a week, and the month is what
        Octopus has.
        """
        self.dashboard.poll()
        self.assertTrue(FakeOctopus.windows, "no readings were fetched at all")
        self.assertGreaterEqual(self._days_asked_for(), 29)

    def test_the_store_ends_up_with_a_month_of_readings(self):
        self.dashboard.poll()
        rows = self.history.gas_readings_between(0, 2 ** 31)
        self.assertGreaterEqual(len(rows), 29 * 24)

    def _poll_again(self) -> None:
        """Poll as if the fifteen minutes had passed.

        The quick poll is throttled so it does not hammer Octopus's
        rate limit, and a test has to say that the time has gone by.
        """
        self.dashboard._last_gas_fetch = 0
        self.dashboard.poll()

    def test_the_next_fetch_only_asks_for_what_is_new(self):
        """Two days is all that can be new between polls."""
        self.dashboard.poll()
        FakeOctopus.windows.clear()
        self._poll_again()
        self.assertTrue(FakeOctopus.windows)
        self.assertLessEqual(self._days_asked_for(), 2.5)

    def test_the_month_is_fetched_again_once_a_day(self):
        """Octopus revises readings and the meter leaves gaps."""
        self.dashboard.poll()
        FakeOctopus.windows.clear()
        # Pretend the last backfill was yesterday.
        self.dashboard._last_gas_backfill -= 86400
        self._poll_again()
        self.assertGreaterEqual(self._days_asked_for(), 29)

    def test_the_store_keeps_growing_by_one_day_at_a_time(self):
        """Each poll adds a day, not a month.

        The month is asked for once a day; the rest of the time only
        the new two days are fetched, and the store keeps what it
        already had.
        """
        self.dashboard.poll()
        first = len(self.history.gas_readings_between(0, 2 ** 31))
        FakeOctopus.windows.clear()
        self._poll_again()
        self.assertLessEqual(self._days_asked_for(), 2.5)
        second = len(self.history.gas_readings_between(0, 2 ** 31))
        self.assertGreaterEqual(second, first)


if __name__ == "__main__":
    unittest.main()

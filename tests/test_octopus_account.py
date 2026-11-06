"""Tests for finding the gas meter in the Octopus account payload.

The account endpoint has been seen in more than one shape, and guessing
has already cost one crash and one false "you have no gas agreement".
So the client looks for the pieces wherever they are, and these tests
pin that down against every shape seen or plausible.
"""

import os
import unittest

from ember.dashboard import Dashboard
from ember.octopus import Octopus, declared_unit


def client_with(payload):
    """An Octopus whose account endpoint returns the payload given."""
    client = Octopus("sk_live_test", "A-3B178E7A")
    client.account_details = lambda: payload
    return client


SHAPE_PROPERTIES = {
    "number": "A-3B178E7A",
    "properties": [
        {
            "id": 1,
            "name": "Home",
            "region": "South England",
            "gas_meter_points": [
                {
                    "mprn": "1234567890",
                    "meters": [
                        {"serial_number": "ABC123", "meter_type": "SMETS2",
                         "manufacturer": "Sagemcom"},
                    ],
                    "agreed_product_codes": ["AGILE-24-10-01"],
                }
            ],
        }
    ],
    "gas_agreements": {
        "44556677": {
            "meter_point": "1234567890",
            "product_code": "AGILE-24-10-01",
            "tariff_code": "G-1R-AGILE-24-10-01-A",
            "valid_from": "2026-10-01T00:00:00Z",
            "valid_to": None,
        }
    },
}

SHAPE_AGREEMENTS_LIST = {
    "number": "A-3B178E7A",
    "properties": [],
    "gas_agreements": [
        {
            "id": 44556677,
            "meter_points": [
                {
                    "mprn": "1234567890",
                    "meters": [{"serial_number": "ABC123", "meter_type": "SMETS2"}],
                    "agreed_product_codes": ["AGILE-24-10-01"],
                    "valid_to": None,
                }
            ],
            "agreements": [{"start_date": "2026-10-01", "end_date": None}],
        }
    ],
}

SHAPE_POINT_ONLY = {
    "number": "A-3B178E7A",
    "properties": [],
    "gas_agreements": {
        "44556677": {
            "meter_point": "1234567890",
            "product_code": "AGILE-24-10-01",
            "tariff_code": "G-1R-AGILE-24-10-01-A",
            "valid_from": "2026-10-01T00:00:00Z",
            "valid_to": None,
        }
    },
}

SHAPE_NESTED = {
    "number": "A-3B178E7A",
    "properties": [
        {
            "name": "Home",
            "gas_meter_points": [
                {
                    "mprn": "1234567890",
                    "meters": [{"serial_number": "ABC123", "meter_type": "SMETS1"}],
                }
            ],
        }
    ],
    "gas_agreements": {
        "44556677": {
            "meter_point": "1234567890",
            "product_code": "VAR-21-01-01-A",
            "tariff_code": "G-1R-VAR-21-01-01-A",
            "valid_from": "2021-04-01T00:00:00Z",
            "valid_to": None,
        }
    },
}

#: What the real reply looked like: a property with no name and no
#: region, and no gas agreements at all.
SHAPE_EMPTY = {
    "number": "A-3B178E7A",
    "properties": [{"id": 1}],
    "gas_agreements": {},
}


class FindTheGasMeterTests(unittest.TestCase):
    def test_properties_shape(self):
        rows = client_with(SHAPE_PROPERTIES).gas_agreements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mprn"], "1234567890")
        self.assertEqual(rows[0]["serial"], "ABC123")
        self.assertEqual(rows[0]["meter_type"], "SMETS2")
        self.assertEqual(rows[0]["product_code"], "AGILE-24-10-01")
        self.assertEqual(rows[0]["tariff_code"], "G-1R-AGILE-24-10-01-A")

    def test_agreements_as_a_list_shape(self):
        rows = client_with(SHAPE_AGREEMENTS_LIST).gas_agreements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mprn"], "1234567890")
        self.assertEqual(rows[0]["serial"], "ABC123")
        self.assertEqual(rows[0]["product_code"], "AGILE-24-10-01")

    def test_meter_point_only_in_the_agreements_shape(self):
        rows = client_with(SHAPE_POINT_ONLY).gas_agreements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mprn"], "1234567890")
        self.assertEqual(rows[0]["product_code"], "AGILE-24-10-01")

    def test_a_nested_shape(self):
        rows = client_with(SHAPE_NESTED).gas_agreements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meter_type"], "SMETS1")
        self.assertEqual(rows[0]["product_code"], "VAR-21-01-01-A")

    def test_an_account_with_no_gas_returns_nothing_rather_than_raising(self):
        rows = client_with(SHAPE_EMPTY).gas_agreements()
        self.assertEqual(rows, [])

    def test_a_meter_point_is_not_reported_twice(self):
        payload = {
            "properties": [{"gas_meter_points": [
                {"mprn": "1234567890", "meters": [{"serial_number": "ABC123"}]}]}],
            "gas_agreements": {
                "1": {"meter_point": "1234567890", "product_code": "X"},
            },
        }
        rows = client_with(payload).gas_agreements()
        self.assertEqual(len(rows), 1)

    def test_two_meters_are_both_found(self):
        payload = {
            "properties": [{"gas_meter_points": [
                {"mprn": "111", "meters": [{"serial_number": "AAA"}]},
                {"mprn": "222", "meters": [{"serial_number": "BBB"}]},
            ]}],
        }
        rows = client_with(payload).gas_agreements()
        self.assertEqual(sorted(row["mprn"] for row in rows), ["111", "222"])


if __name__ == "__main__":
    unittest.main()


class ChooseUnitTests(unittest.TestCase):
    """The meter's own type beats a guess from the size of the readings."""

    def test_a_smeths2_meter_wins(self):
        unit, calorific, reason, confidence = Dashboard._choose_unit(
            {"unit": "kwh", "calorific": 1.0, "reason": "numbers say kWh"},
            "SMETS2", 11.16)
        self.assertEqual(unit, "m3")
        self.assertAlmostEqual(calorific, 11.16)
        self.assertIn("cubic metres", reason)
        self.assertEqual(confidence, "the meter type")

    def test_a_smeths1_meter_wins(self):
        unit, calorific, reason, _ = Dashboard._choose_unit(
            {"unit": "m3", "calorific": None, "reason": "numbers say m3"},
            "SMETS1", 11.16)
        self.assertEqual(unit, "kwh")
        self.assertAlmostEqual(calorific, 1.0)
        self.assertIn("kWh", reason)

    def test_without_a_meter_type_the_guess_stands(self):
        unit, calorific, reason, confidence = Dashboard._choose_unit(
            {"unit": "m3", "calorific": None, "reason": "numbers say m3"},
            "", 11.16)
        self.assertEqual(unit, "m3")
        self.assertEqual(reason, "numbers say m3")
        self.assertNotEqual(confidence, "the meter type")

    def test_an_unknown_meter_type_is_ignored(self):
        unit, _, reason, _ = Dashboard._choose_unit(
            {"unit": "m3", "calorific": None, "reason": "numbers say m3"},
            "GAS-METER", 11.16)
        self.assertEqual(unit, "m3")
        self.assertEqual(reason, "numbers say m3")

    def test_the_meter_type_and_the_numbers_agreeing(self):
        unit, _, reason, _ = Dashboard._choose_unit(
            {"unit": "m3", "calorific": None, "reason": "numbers say m3"},
            "SMETS2", 11.16)
        self.assertEqual(unit, "m3")
        self.assertIn("agrees", reason)


class MeterExchangeTests(unittest.TestCase):
    """A meter point can carry two meters: the old one and its
    replacement. The history lives under the old serial and the present
    under the new one, and Ember needs both."""

    PAYLOAD = {
        "number": "A-3B178E7A",
        "properties": [
            {
                "gas_meter_points": [
                    {
                        "mprn": "8920115204",
                        "meters": [
                            {"serial_number": "E6E09927562343", "meter_type": "SMETS1"},
                            {"serial_number": "E6S13783422161", "meter_type": "SMETS2"},
                        ],
                    }
                ]
            }
        ],
    }

    def test_both_meters_are_found(self):
        rows = client_with(self.PAYLOAD).gas_agreements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]["serials"]),
                         ["E6E09927562343", "E6S13783422161"])

    def test_the_meter_in_service_is_the_one_shown(self):
        """The name used to say the opposite of what it asserted.

        The retired meter is the one that was there first; the new one
        was added after it. So the meter in service is the second one,
        and this payload carries no dates to say so - the order does.
        """
        rows = client_with(self.PAYLOAD).gas_agreements()
        self.assertEqual(rows[0]["serial"], "E6S13783422161")
        self.assertEqual(rows[0]["live_serial"], "E6S13783422161")

    def test_the_active_window_wins_over_the_order(self):
        """When the meters say when they were in service, that is used."""
        payload = {
            "number": "A-3B178E7A",
            "properties": [{
                "gas_meter_points": [{
                    "mprn": "8920115204",
                    "meters": [
                        {"serial_number": "E6S13783422161",
                         "meter_type": "SMETS2",
                         "active_to": "2026-11-03T00:00:00Z"},
                        {"serial_number": "E6E09927562343",
                         "meter_type": "SMETS1",
                         "active_from": "2026-11-03T00:00:00Z"},
                    ],
                }],
            }],
        }
        rows = client_with(payload).gas_agreements()
        self.assertEqual(rows[0]["serial"], "E6E09927562343")
        self.assertEqual(rows[0]["live_serial"], "E6E09927562343")
        self.assertEqual(rows[0]["meter_type"], "SMETS1")

    def test_readings_from_both_meters_combine(self):
        """The old meter's last day plus the new meter's first day is
        one continuous history, not two overlapping ones."""
        import tempfile
        from datetime import datetime, timedelta, timezone
        from ember.history import History

        path = os.path.join(tempfile.gettempdir(), "ember-test-exchange.db")
        if os.path.exists(path):
            os.remove(path)
        history = History(path)
        try:
            start = datetime(2026, 11, 2, tzinfo=timezone.utc)
            old = [{"ts": (start + timedelta(hours=1)).timestamp(), "m3": 0.2,
                    "provisional": False}]
            new = [{"ts": (start + timedelta(hours=2)).timestamp(), "m3": 0.3,
                    "provisional": False}]
            history.record_gas(old)
            history.record_gas(new)
            total = history.gas_total_between(start.timestamp(),
                                               (start + timedelta(days=1)).timestamp())
            self.assertAlmostEqual(total, 0.5)
        finally:
            history.close()
            if os.path.exists(path):
                os.remove(path)

    def test_the_same_interval_from_two_meters_counts_once(self):
        """If both meters report the same interval, it is one reading."""
        import tempfile
        from datetime import datetime, timezone
        from ember.history import History

        path = os.path.join(tempfile.gettempdir(), "ember-test-overlap.db")
        if os.path.exists(path):
            os.remove(path)
        history = History(path)
        try:
            stamp = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
            history.record_gas([{"ts": stamp, "m3": 0.2, "provisional": False}])
            history.record_gas([{"ts": stamp, "m3": 0.2, "provisional": False}])
            total = history.gas_total_between(stamp - 3600, stamp + 3600)
            self.assertAlmostEqual(total, 0.2)
        finally:
            history.close()
            if os.path.exists(path):
                os.remove(path)


class DeclaredUnitTests(unittest.TestCase):
    """What the meter says it reads, which beats a guess."""

    def test_a_smeths2_meter_reads_cubic_metres(self):
        self.assertEqual(declared_unit("SMETS2"), "m3")

    def test_a_smeths1_meter_reads_kwh(self):
        self.assertEqual(declared_unit("SMETS1"), "kwh")

    def test_the_units_field_says_it_too(self):
        self.assertEqual(declared_unit("", "M3"), "m3")
        self.assertEqual(declared_unit("", "kWh"), "kwh")
        self.assertEqual(declared_unit("", "M^3"), "m3")

    def test_an_unknown_meter_says_nothing(self):
        self.assertEqual(declared_unit("GAS-METER"), "")
        self.assertEqual(declared_unit("", ""), "")

    def test_the_meter_type_is_checked_first(self):
        self.assertEqual(declared_unit("SMETS2", "kWh"), "m3")


class GraphQLShapeTests(unittest.TestCase):
    """The reply from the query that was verified against Kraken's
    live schema on 2026-11-04."""

    REPLY = {
        "data": {
            "account": {
                "number": "A-3B178E7A",
                "gasAgreements": [
                    {
                        "id": 8920115204,
                        "validFrom": "2026-10-01T00:00:00+00:00",
                        "validTo": None,
                        "meterPoint": {
                            "mprn": "8920115204",
                            "status": "ACTIVE",
                            "meters": [
                                {
                                    "serialNumber": "E6E09927562343",
                                    "meterType": "SMETS2",
                                    "consumptionUnits": "M3",
                                    "manufacturerCode": "LANDISGYR",
                                    "modelName": "E6S",
                                    "manufacturedYear": 2024,
                                    "mechanism": "MT",
                                    "status": "CU",
                                    "activeFrom": "2026-10-20",
                                    "activeTo": None,
                                    "installationDate": "2026-10-20",
                                },
                                {
                                    "serialNumber": "E6S13783422161",
                                    "meterType": "SMETS2",
                                    "consumptionUnits": "M3",
                                    "manufacturerCode": "LANDISGYR",
                                    "modelName": "E6S",
                                    "manufacturedYear": 2026,
                                    "status": "CA",
                                    "activeFrom": "2026-11-03",
                                    "activeTo": None,
                                    "installationDate": "2026-11-03",
                                },
                            ],
                        },
                        "tariff": {
                            "productCode": "AGILE-24-10-01",
                            "tariffCode": "G-1R-AGILE-24-10-01-A",
                            "standingCharge": 28.8,
                            "preVatStandingCharge": 26.9,
                            "displayName": "Agile Octopus",
                            "fullName": "Agile Octopus November 2024",
                            "unitRate": 5.63,
                        },
                    }
                ],
            }
        }
    }

    def test_the_agreement_carries_the_meter_and_the_tariff(self):
        agreements = self.REPLY["data"]["account"]["gasAgreements"]
        agreement = agreements[0]
        self.assertEqual(agreement["meterPoint"]["mprn"], "8920115204")
        self.assertEqual(agreement["tariff"]["productCode"], "AGILE-24-10-01")
        self.assertEqual(agreement["tariff"]["tariffCode"], "G-1R-AGILE-24-10-01-A")

    def test_both_meters_are_on_the_point(self):
        meters = self.REPLY["data"]["account"]["gasAgreements"][0]["meterPoint"]["meters"]
        self.assertEqual(len(meters), 2)
        self.assertEqual([m["serialNumber"] for m in meters],
                         ["E6E09927562343", "E6S13783422161"])

    def test_the_meter_type_and_units_are_there(self):
        meters = self.REPLY["data"]["account"]["gasAgreements"][0]["meterPoint"]["meters"]
        self.assertEqual(meters[0]["meterType"], "SMETS2")
        self.assertEqual(meters[0]["consumptionUnits"], "M3")

    def test_the_query_in_the_code_matches_the_verified_schema(self):
        """The query must not drift away from what the server accepts."""
        import ember.octopus as octopus
        query = octopus._GAS_AGREEMENTS_QUERY
        for field in ("gasAgreements", "meterPoint", "meters", "tariff",
                      "productCode", "tariffCode", "standingCharge",
                      "serialNumber", "meterType", "consumptionUnits"):
            self.assertIn(field, query)
        # Gas tariffs are a plain object, not a union, so no inline
        # fragments. Getting that wrong is what the old query did.
        self.assertNotIn("... on ", query)

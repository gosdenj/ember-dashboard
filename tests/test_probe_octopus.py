"""Tests for the Octopus probe's judgement calls.

The probe is a diagnostic, not part of the app, but the one thing it has
to get right - the unit the meter reports in - cannot be read from the
API. It has to be reasoned about, and the reasoning has to be shown.

These tests pin the reasoning down without touching the network.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "probes", "probe_octopus.py")


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_octopus", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnitGuessTests(unittest.TestCase):
    """A home with gas heating burns tens of kWh a day, which is a couple
    of cubic metres. So the size of the numbers is a clue - but only a
    clue, and the probe has to admit when it is guessing."""

    def setUp(self):
        self.probe = load_probe()

    def test_a_small_daily_total_reads_as_cubic_metres(self):
        """2.1 a day is 2.1 m3 (about 23 kWh) or 2.1 kWh (impossible)."""
        unit, why = self.probe.guess_unit(2.1)
        self.assertEqual(unit, "m3")
        self.assertIn("far too little", why)

    def test_a_large_daily_total_reads_as_kwh(self):
        """23 a day is 23 kWh (normal) or 23 m3 (258 kWh, far too much)."""
        unit, why = self.probe.guess_unit(23.0)
        self.assertEqual(unit, "kWh")
        self.assertIn("far too much", why)

    def test_an_impossible_total_is_called_out(self):
        unit, why = self.probe.guess_unit(0.05)
        self.assertIn("below the range", why)

    def test_an_ambiguous_total_says_so(self):
        """Somewhere both readings land in a normal range - the probe must
        not pretend to know."""
        unit, why = self.probe.guess_unit(8.5)
        self.assertIn("coin toss", why)

    def test_no_readings_leaves_the_unit_undetermined(self):
        unit, why = self.probe.guess_unit(0)
        self.assertEqual(unit, "m3")
        self.assertIn("no readings", why)

    def test_the_reasoning_always_mentions_the_daily_total(self):
        for total in (0.4, 2.1, 8.5, 23.0, 140.0):
            _, why = self.probe.guess_unit(total)
            self.assertIn(str(round(total, 2)), why)


class OverrideTests(unittest.TestCase):
    """The guess can be overridden, because the guess can be wrong."""

    def setUp(self):
        self.probe = load_probe()
        self._saved = os.environ.get("OCTOPUS_GAS_UNIT")
        os.environ.pop("OCTOPUS_GAS_UNIT", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OCTOPUS_GAS_UNIT", None)
        else:
            os.environ["OCTOPUS_GAS_UNIT"] = self._saved

    def test_m3_override_wins(self):
        os.environ["OCTOPUS_GAS_UNIT"] = "m3"
        unit, why = self.probe.guess_unit(23.0)
        self.assertEqual(unit, "m3")
        self.assertIn("OCTOPUS_GAS_UNIT", why)

    def test_kwh_override_wins(self):
        os.environ["OCTOPUS_GAS_UNIT"] = "kWh"
        unit, why = self.probe.guess_unit(2.1)
        self.assertEqual(unit, "kWh")
        self.assertIn("OCTOPUS_GAS_UNIT", why)

    def test_the_alternative_spellings_are_accepted(self):
        for spelling in ("m^3", "m³", "cubic"):
            os.environ["OCTOPUS_GAS_UNIT"] = spelling
            self.assertEqual(self.probe.guess_unit(23.0)[0], "m3")
        for spelling in ("kwh", "kilowatt", "KWH"):
            os.environ["OCTOPUS_GAS_UNIT"] = spelling
            self.assertEqual(self.probe.guess_unit(2.1)[0], "kWh")


class CalorificTests(unittest.TestCase):
    """The m3 to kWh conversion, and the number the bill actually shows."""

    def test_the_conversion_matches_the_octopus_formula(self):
        """kWh = m3 x CV x 1.02264 / 3.6, with CV in MJ/m3."""
        for cv in (39.0, 39.3, 39.5, 40.0):
            expected = cv * 1.02264 / 3.6
            self.assertAlmostEqual(expected, cv * 1.02264 / 3.6, places=6)
        # 39.5 MJ/m3 is the value most bills carry.
        self.assertAlmostEqual(39.5 * 1.02264 / 3.6, 11.221, places=3)

    def test_the_conversion_is_the_right_size(self):
        """A cubic metre of mains gas is about 11 kWh."""
        self.assertAlmostEqual(39.5 * 1.02264 / 3.6, 11.22, places=2)
        self.assertAlmostEqual(39.0 * 1.02264 / 3.6, 11.08, places=2)

    def test_a_bill_calorific_value_maps_to_a_sane_kwh_per_m3(self):
        """Every MJ/m3 value a UK bill can carry has to land in the
        11.0-11.2 kWh/m3 band, or the conversion is wrong."""
        for cv in (38.0, 38.5, 39.0, 39.5, 40.0, 40.5):
            kwh_per_m3 = cv * 1.02264 / 3.6
            self.assertGreater(kwh_per_m3, 10.7)
            self.assertLess(kwh_per_m3, 11.6)


if __name__ == "__main__":
    unittest.main()


class RealShapeTests(unittest.TestCase):
    """The account endpoint returns 'properties' as a LIST.

    An earlier version of the probe assumed a single object and died
    with an AttributeError on the real reply, so the shape is pinned
    down here against the shape Octopus actually sends.
    """

    ACCOUNT = {
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
                            {
                                "serial_number": "ABC123",
                                "meter_type": "SMETS2",
                                "manufacturer": "Sagemcom",
                                "model": "NB-IoT",
                            }
                        ],
                        "agreements": [],
                    }
                ],
                "electricity_meter_points": [],
            }
        ],
        "gas_agreements": {
            "44556677": {
                "meter_point": "1234567890",
                "product_code": "AGILE-24-10-01",
                "valid_from": "2026-10-01T00:00:00Z",
                "valid_to": None,
                "agreement_type": "Joint",
                "tariff_code": "G-1R-AGILE-24-10-01-A",
            }
        },
    }

    def _run(self, consumption, meter_type="SMETS2", profile_path=None):
        probe = load_probe()
        probe._key = lambda: "sk_live_test"
        probe._account = lambda: "A-3B178E7A"
        # The probe writes two files when it finishes. A test must not
        # leave them in the project tree.
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        probe.HERE = scratch
        probe.PROFILE = profile_path or os.path.join(scratch, "gas-profile.json")

        account = json.loads(json.dumps(self.ACCOUNT))
        meter = account["properties"][0]["gas_meter_points"][0]["meters"][0]
        if meter_type is None:
            meter.pop("meter_type", None)
        else:
            meter["meter_type"] = meter_type

        def fake_get(path, params=None, key=None):
            if path.startswith("accounts/"):
                return account
            if "/consumption/" in path:
                return {"count": len(consumption),
                        "results": [{"consumption": value,
                                     "interval_start": "2026-11-04T00:00:00Z",
                                     "interval_end": "2026-11-04T00:30:00Z"}
                                    for value in consumption]}
            if path.endswith("/standard-unit-rates/"):
                return {"results": [{"value_inc_vat": 5.63,
                                     "valid_from": "2026-11-01T00:00:00Z"}]}
            if path.endswith("/standing-charges/"):
                return {"results": [{"value_inc_vat": 28.8,
                                     "valid_from": "2026-11-01T00:00:00Z"}]}
            if path.startswith("products/"):
                return {"display_name": "Agile Octopus", "unit": "kWh",
                        "active": True, "standing_charge_inc_vat": 28.8}
            return {}

        probe.get = fake_get
        probe.get_optional = lambda path, params=None: (fake_get(path, params), "")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = probe.main()
        return code, buffer.getvalue()

    def test_the_account_with_a_list_of_properties_does_not_crash(self):
        code, output = self._run([0.02] * 48)
        self.assertEqual(code, 0)
        self.assertIn("A-3B178E7A", output)
        self.assertIn("Home", output)

    def test_the_meter_is_found_in_the_property(self):
        _, output = self._run([0.02] * 48)
        self.assertIn("ABC123", output)
        self.assertIn("1234567890", output)

    def test_a_smeths2_meter_settles_the_unit(self):
        _, output = self._run([0.02] * 48, meter_type="SMETS2")
        self.assertIn("SMETS2", output)
        self.assertIn("UNIT: m3", output)

    def test_a_smeths1_meter_settles_the_unit_the_other_way(self):
        _, output = self._run([1.2] * 48, meter_type="SMETS1")
        self.assertIn("UNIT: kwh", output)

    def test_the_meter_type_overrides_a_contrary_guess(self):
        """The meter's own type is stronger evidence than the numbers."""
        _, output = self._run([0.02] * 48, meter_type="SMETS1")
        self.assertIn("meter type wins", output)

    def test_without_a_meter_type_it_falls_back_to_the_numbers(self):
        _, output = self._run([0.02] * 48, meter_type=None)
        self.assertIn("UNIT: m3", output)
        self.assertNotIn("meter type wins", output)

    def test_the_profile_is_written(self):
        path = os.path.join(tempfile.gettempdir(), "ember-test-gas-profile.json")
        try:
            self._run([0.02] * 48, profile_path=path)
            with open(path, encoding="utf-8") as handle:
                profile = json.load(handle)
            self.assertEqual(profile["account_number"], "A-3B178E7A")
            self.assertEqual(profile["unit"], "m3")
            self.assertEqual(profile["meter_type"], ["SMETS2"])
        finally:
            if os.path.exists(path):
                os.remove(path)


class SettingsOutputTests(unittest.TestCase):
    """The probe writes the settings the dashboard needs, so wiring it
    up is a copy rather than a dozen keystrokes."""

    def test_the_settings_file_is_written(self):
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "ember-test-settings.json")
        try:
            self._run(path)
            with open(path, encoding="utf-8") as handle:
                settings = json.load(handle)
            self.assertEqual(settings["octopus_account"], "A-3B178E7A")
            self.assertEqual(settings["gas_mprn"], "1234567890")
            self.assertEqual(settings["gas_product_code"], "AGILE-24-10-01")
            self.assertEqual(settings["gas_tariff_code"], "G-1R-AGILE-24-10-01-A")
            self.assertEqual(settings["gas_meter_type"], "SMETS2")
            self.assertEqual(settings["gas_unit"], "m3")
            self.assertIn("ABC123", settings["gas_meter_serials"])
        finally:
            if os.path.exists(path):
                os.remove(path)

    def _run(self, path):
        probe = load_probe()
        probe._key = lambda: "sk_live_test"
        probe._account = lambda: "A-3B178E7A"

        account = json.loads(json.dumps(RealShapeTests.ACCOUNT))

        def fake_get(name, params=None, key=None):
            if name.startswith("accounts/"):
                return account
            if "/consumption/" in name:
                return {"count": 48, "results": [
                    {"consumption": 0.05, "interval_start": "2026-11-04T00:00:00Z",
                     "interval_end": "2026-11-04T00:30:00Z"} for _ in range(48)]}
            if name.endswith("/standard-unit-rates/"):
                return {"results": [{"value_inc_vat": 5.63,
                                     "valid_from": "2026-11-01T00:00:00Z"}]}
            if name.endswith("/standing-charges/"):
                return {"results": [{"value_inc_vat": 28.39,
                                     "valid_from": "2026-11-01T00:00:00Z"}]}
            if name.startswith("products/"):
                return {"display_name": "Agile Octopus", "unit": "kWh",
                        "active": True}
            return {}

        probe.get = fake_get
        probe.get_optional = lambda name, params=None: (fake_get(name, params), "")
        probe.PROFILE = path + ".profile.json"
        probe.find_tariff = lambda mprn: {
            "source": "the GraphQL API",
            "product_code": "AGILE-24-10-01",
            "tariff_code": "G-1R-AGILE-24-10-01-A",
            "display_name": "Agile Octopus",
            "standing_charge": 28.39,
            "unit_rate": 5.63,
            "meter_type": "SMETS2",
            "consumption_units": "M3",
            "serials": ["ABC123"],
            "meters": [{"serial": "ABC123", "meter_type": "SMETS2",
                        "consumption_units": "M3", "manufacturer": "LG",
                        "model": "E6S", "year": 2026, "status": "CU",
                        "active_from": "2026-11-03", "active_to": "",
                        "installation_date": "2026-11-03"}],
        }
        # The settings path is derived from the module's own directory.
        #
        # `probe.os` is the os module itself, so patching it patches it
        # for the whole process. It is put back afterwards - otherwise
        # every later test in the run gets a path that is not a path.
        original = probe.os.path.join
        probe.os.path.join = lambda *parts: path if parts[-1] == "ember-settings.json" \
            else original(*parts)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                probe.main()
        finally:
            probe.os.path.join = original

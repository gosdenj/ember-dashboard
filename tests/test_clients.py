"""Tests for the settings store and the two API clients."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from ember.config import ROOMS, SECRET_KEYS, Settings
from ember.hive import Hive, pad_hex, sha256_hex
from ember.octopus import Octopus, OctopusError


def _day_of(total, half_hours=48):
    """A day of half-hourly readings adding up to the total given."""
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 11, 4, tzinfo=timezone.utc)
    per = total / half_hours
    return [{
        "consumption": per,
        "ts": (start + timedelta(minutes=30 * index)).timestamp(),
        "interval_start": (start + timedelta(minutes=30 * index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval_end": (start + timedelta(minutes=30 * (index + 1))).strftime("%Y-%m-%dT%H:%M:%SZ"),
    } for index in range(half_hours)]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.addCleanup(self.tmp.cleanup)

    def test_defaults_are_used_when_there_is_no_file(self):
        settings = Settings(self.path)
        self.assertEqual(settings.get("panel_name"), "Home")
        self.assertEqual(settings.get_float("calorific_value"), 11.16)

    def test_values_survive_a_save_and_load(self):
        settings = Settings(self.path)
        settings.set("gas_mprn", "1234567890")
        settings.set("monthly_budget", 75.5)
        settings.save()
        again = Settings(self.path)
        self.assertEqual(again.get("gas_mprn"), "1234567890")
        self.assertEqual(again.get_float("monthly_budget"), 75.5)

    def test_the_file_is_not_world_readable(self):
        settings = Settings(self.path)
        settings.set("octopus_api_key", "sk_live_secret")
        settings.save()
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "the settings file holds secrets")

    def test_secrets_never_reach_the_browser(self):
        settings = Settings(self.path)
        settings.set("octopus_api_key", "sk_live_secret")
        settings.set("hive_password", "hunter2")
        public = settings.public()
        for key in SECRET_KEYS:
            self.assertNotIn(key, public)
        self.assertNotIn("hunter2", json.dumps(public))
        self.assertTrue(public["has_octopus_key"])
        self.assertTrue(public["has_hive_password"])

    def test_the_environment_overrides_the_file(self):
        settings = Settings(self.path)
        settings.set("gas_mprn", "from-file")
        os.environ["EMBER_GAS_MPRN"] = "from-environment"
        try:
            again = Settings(self.path)
            self.assertEqual(again.get("gas_mprn"), "from-environment")
        finally:
            del os.environ["EMBER_GAS_MPRN"]

    def test_unknown_settings_are_refused(self):
        settings = Settings(self.path)
        with self.assertRaises(KeyError):
            settings.set("not_a_setting", 1)

    def test_types_are_coerced(self):
        settings = Settings(self.path)
        settings.set("poll_seconds", "45")
        settings.set("demo", "true")
        settings.set("calorific_value", "11.2")
        self.assertEqual(settings.get_int("poll_seconds"), 45)
        self.assertIs(settings.get_bool("demo"), True)
        self.assertAlmostEqual(settings.get_float("calorific_value"), 11.2)

    def test_there_are_nine_rooms_and_lounge_is_one_of_them(self):
        self.assertEqual(len(ROOMS), 9)
        self.assertIn("Lounge", ROOMS)
        self.assertIn("Upstairs Hallway", ROOMS)
        self.assertIn("Dining Room", ROOMS)
        self.assertEqual(len(set(ROOMS)), 9, "no room should be listed twice")


class HiveHelperTests(unittest.TestCase):
    def test_pad_hex_always_even_length(self):
        self.assertEqual(len(pad_hex(1)), 2)
        self.assertEqual(len(pad_hex(255)), 4)
        self.assertEqual(pad_hex("abc"), "0abc")

    def test_pad_hex_leading_nibble(self):
        # 0x8.. and 0xf.. need a leading zero so they read as positive.
        self.assertEqual(pad_hex("ff"), "00ff")

    def test_sha256_hex_is_padded_to_64(self):
        self.assertEqual(len(sha256_hex(b"ember")), 64)

    def test_the_reading_of_a_trv(self):
        attributes = {"setpoint": 20.0, "temperature": 19.2, "position": 255}
        reading = Hive._read_trv(attributes)
        self.assertAlmostEqual(reading["target"], 20.0)
        self.assertAlmostEqual(reading["current"], 19.2)
        self.assertIs(reading["valve_open"], True)

    def test_a_closed_valve_is_read_as_shut(self):
        reading = Hive._read_trv({"setpoint": 20.0, "temperature": 19.2, "position": 0})
        self.assertIs(reading["valve_open"], False)

    def test_a_node_with_no_temperatures_is_not_a_trv(self):
        self.assertIsNone(Hive._read_trv({"something": "else"}))


class OctopusTests(unittest.TestCase):
    def test_it_refuses_to_work_without_a_key(self):
        with self.assertRaises(OctopusError):
            Octopus("", "A-123")

    def test_it_refuses_to_work_without_an_account(self):
        with self.assertRaises(OctopusError):
            Octopus("sk_live_abc", "")

    def test_a_kwh_meter_is_detected(self):
        """A SMETS1 meter reads kWh, so a day of readings adds up to
        about 25 - a normal amount of kWh for a home."""
        detected = Octopus.detect_unit(_day_of(25.0))
        self.assertEqual(detected["unit"], "kwh")
        self.assertAlmostEqual(detected["calorific"], 1.0)

    def test_an_m3_meter_is_detected(self):
        """A SMETS2 meter reads cubic metres, so a day adds up to about
        2.2 - which is about 25 kWh."""
        detected = Octopus.detect_unit(_day_of(2.2))
        self.assertEqual(detected["unit"], "m3")

    def test_a_detection_is_never_certain(self):
        """There is no second number in the reply to check against, so
        this is always a reasoned guess."""
        for total in (2.2, 25.0):
            detected = Octopus.detect_unit(_day_of(total))
            self.assertNotEqual(detected["confidence"], "high")
            self.assertTrue(detected.get("reason"))

    def test_no_data_means_no_guess(self):
        detected = Octopus.detect_unit([{"consumption": None}])
        self.assertEqual(detected["unit"], "unknown")
        self.assertIsNone(detected["calorific"])

    def test_a_consumption_row_is_normalised(self):
        row = Octopus._to_reading({
            "interval_start": "2026-11-04T00:00:00Z",
            "interval_end": "2026-11-04T00:30:00Z",
            "consumption": 0.0123,
            "is_estimated": True,
        })
        self.assertAlmostEqual(row["consumption"], 0.0123)
        self.assertTrue(row["provisional"])
        self.assertGreater(row["ts"], 0)

    def test_a_consumption_row_has_no_kwh_field(self):
        """The gas consumption endpoint returns three fields and no
        more. A 'kwh' on the row would be something we made up."""
        row = Octopus._to_reading({
            "interval_start": "2026-11-04T00:00:00Z",
            "interval_end": "2026-11-04T00:30:00Z",
            "consumption": 0.0123,
        })
        self.assertNotIn("kwh", row)


if __name__ == "__main__":
    unittest.main()

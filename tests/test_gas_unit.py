"""Tests for how Ember handles the gas unit.

The rule that makes this tricky: **Octopus never says which unit the
meter reports in.**

From Octopus's own API documentation:

    "customers on SMETS1 meters get their consumption data pre-converted
     to kwh, while customers on SMETS2 meters get their consumption data
     in cubic meters"

And the consumption endpoint returns three fields - consumption,
interval_start, interval_end - and nothing else. There is no second
number to compare against, so the unit cannot be detected from the data.

Everything here has to work from the size of the readings instead, and
has to be honest that it is a guess.
"""

import unittest

from ember.octopus import Octopus, kwh_per_m3, m3_to_kwh


def readings(daily_total, half_hours=48):
    """A day of readings that add up to the daily total given."""
    per = daily_total / half_hours
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 11, 4, tzinfo=timezone.utc)
    out = []
    for index in range(half_hours):
        stamp = start + timedelta(minutes=30 * index)
        out.append({
            "consumption": per,
            "ts": stamp.timestamp(),
            "interval_start": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval_end": (stamp + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return out


def history_rows(daily_total, half_hours=48):
    """The same day, as the history store hands it back.

    The column is called 'value', not 'm3', because the reading is in
    the meter's own unit - calling it 'm3' would assume the very thing
    this module is trying to work out.
    """
    return [{"value": reading["consumption"], "ts": reading["ts"]}
            for reading in readings(daily_total, half_hours)]


class DetectUnitTests(unittest.TestCase):
    def test_a_small_daily_total_is_cubic_metres(self):
        """2.1 a day is 2.1 m3 (about 23 kWh) or 2.1 kWh (impossible)."""
        detected = Octopus.detect_unit(readings(2.1))
        self.assertEqual(detected["unit"], "m3")
        self.assertIn("far too little", detected["reason"])

    def test_a_large_daily_total_is_kwh(self):
        """23 a day is 23 kWh (normal) or 23 m3 (258 kWh, far too much)."""
        detected = Octopus.detect_unit(readings(23.0))
        self.assertEqual(detected["unit"], "kwh")

    def test_it_never_claims_high_confidence(self):
        """There is no second number to check against, so nothing about
        this is ever certain."""
        for total in (0.4, 2.1, 8.5, 23.0, 140.0):
            detected = Octopus.detect_unit(readings(total))
            self.assertNotEqual(detected["confidence"], "high")

    def test_no_readings_says_so(self):
        detected = Octopus.detect_unit([])
        self.assertEqual(detected["unit"], "unknown")
        self.assertEqual(detected["confidence"], "no data")

    def test_junk_readings_do_not_crash_it(self):
        detected = Octopus.detect_unit([
            {"consumption": None}, {"consumption": "abc"},
        ])
        self.assertEqual(detected["unit"], "unknown")

    def test_too_few_readings_to_tell(self):
        """One reading is not a day, so it cannot say anything."""
        detected = Octopus.detect_unit(readings(2.0, half_hours=1))
        self.assertEqual(detected["confidence"], "not enough data")

    def test_it_works_on_the_history_rows_the_dashboard_actually_has(self):
        """The history store keeps 'value' and 'ts', not 'consumption'.

        Detecting against the wrong key means it never fires - and the
        dashboard's cross-check against the readings is then silently
        doing nothing, which is how a wrong unit survives a meter
        exchange.
        """
        detected = Octopus.detect_unit(history_rows(2.1))
        self.assertEqual(detected["unit"], "m3")
        self.assertAlmostEqual(detected["daily_total"], 2.1, places=3)

    def test_the_dashboard_cross_check_actually_fires(self):
        """The whole point: the meter and the readings must agree.

        With the wrong key the detection returns "unknown", the reason
        says the readings would have suggested "unknown", and the
        cross-check has quietly stopped happening.
        """
        from ember.dashboard import Dashboard
        detected = Octopus.detect_unit(history_rows(2.433))
        self.assertEqual(detected["unit"], "m3")
        unit, calorific, reason, confidence = Dashboard._choose_unit(
            detected, "SMETS2", 11.16, "m3")
        self.assertEqual(unit, "m3")
        self.assertAlmostEqual(calorific, 11.16, places=2)
        self.assertIn("agrees", reason)

    def test_a_wrong_meter_type_is_said_out_loud(self):
        """The meter's record wins, but the disagreement is reported."""
        from ember.dashboard import Dashboard
        detected = Octopus.detect_unit(history_rows(2.433))
        unit, calorific, reason, _ = Dashboard._choose_unit(
            detected, "SMETS1", 11.16, "kwh")
        self.assertEqual(unit, "kwh")
        self.assertIn("would have suggested", reason)

    def test_nothing_known_means_no_unit_at_all(self):
        """No meter type and no readings: say so, do not guess."""
        from ember.dashboard import Dashboard
        unit, calorific, reason, confidence = Dashboard._choose_unit(
            {"unit": "unknown", "reason": "no readings", "confidence": "no data"},
            "", 11.16, "")
        self.assertEqual(unit, "unknown")
        self.assertAlmostEqual(calorific, 1.0, places=6)
        self.assertIn("does not say", reason)

    def test_the_daily_total_is_scaled_by_the_span(self):
        """Two days of readings must give the same daily total as one."""
        two_days = readings(2.1 * 2, half_hours=96)
        detected = Octopus.detect_unit(two_days)
        self.assertAlmostEqual(detected["daily_total"], 2.1, places=3)

    def test_the_old_code_looked_for_a_field_that_never_exists(self):
        """The API has no 'kwh' field on a reading. If someone adds one
        back, the unit detection must not depend on it - the old code
        compared the two and concluded 'unknown' every time."""
        rows = readings(2.1)
        for row in rows:
            row["kwh"] = row["consumption"] * 11.2
        detected = Octopus.detect_unit(rows)
        self.assertEqual(detected["unit"], "m3")


class CalorificTests(unittest.TestCase):
    """The conversion, and the number a gas bill actually carries."""

    def test_the_formula_is_octopus_s(self):
        """kWh = m3 x CV x 1.02264 / 3.6, with CV in MJ/m3."""
        self.assertAlmostEqual(m3_to_kwh(1.0, 39.5), 39.5 * 1.02264 / 3.6, places=9)

    def test_a_bill_value_maps_to_the_right_kwh_per_m3(self):
        self.assertAlmostEqual(kwh_per_m3(39.5), 11.221, places=3)
        self.assertAlmostEqual(kwh_per_m3(39.0), 11.079, places=3)
        self.assertAlmostEqual(kwh_per_m3(40.0), 11.363, places=3)

    def test_every_bill_value_lands_in_the_expected_band(self):
        for cv in (38.0, 38.5, 39.0, 39.5, 40.0, 40.5):
            self.assertGreater(kwh_per_m3(cv), 10.7)
            self.assertLess(kwh_per_m3(cv), 11.7)

    def test_a_cubic_metre_is_about_eleven_kilowatt_hours(self):
        self.assertAlmostEqual(m3_to_kwh(1.0, 39.5), 11.2, places=1)


if __name__ == "__main__":
    unittest.main()

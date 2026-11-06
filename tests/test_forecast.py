"""Tests for the forecast model.

The model has one rule that is easy to get wrong and expensive to get
wrong: it must not be forced through the origin. With a combi boiler,
hot water and cooking are used whatever the weather, so the line has to
have an intercept.
"""

import unittest

from ember import forecast


class DegreeDays(unittest.TestCase):
    def test_mild_day_has_no_degree_days(self):
        self.assertEqual(forecast.degree_days(18.0, 16.0), 0.0)

    def test_cold_day(self):
        self.assertEqual(forecast.degree_days(4.0, 16.0), 12.0)

    def test_exactly_the_base_temperature(self):
        self.assertEqual(forecast.degree_days(16.0, 16.0), 0.0)

    def test_missing_temperature(self):
        self.assertEqual(forecast.degree_days(None, 16.0), 0.0)


class Fit(unittest.TestCase):
    def build(self, baseline, slope, days=21, noise=0.0, seed=1):
        """Make a set of days that a model with these numbers would produce."""
        import random
        rng = random.Random(seed)
        pairs = []
        for index in range(days):
            degree_days = float(index)
            gas = baseline + slope * degree_days + rng.uniform(-noise, noise)
            pairs.append((degree_days, gas))
        return pairs

    def test_recovers_the_baseline_and_slope(self):
        pairs = self.build(0.8, 0.13, noise=0.02)
        model = forecast.fit(pairs, 16.0)
        self.assertAlmostEqual(model.baseline, 0.8, delta=0.05)
        self.assertAlmostEqual(model.slope, 0.13, delta=0.01)

    def test_the_line_does_not_go_through_the_origin(self):
        """The whole point: a warm day still costs gas."""
        pairs = self.build(0.8, 0.13, noise=0.01)
        model = forecast.fit(pairs, 16.0)
        self.assertGreater(model.baseline, 0.5,
                           "the intercept should hold the weather-independent gas")
        self.assertGreater(model.predict(0.0), 0.5)

    def test_a_through_origin_fit_would_over_predict(self):
        """The reason the intercept exists, demonstrated."""
        pairs = self.build(0.8, 0.13, noise=0.0)
        model = forecast.fit(pairs, 16.0)
        self.assertAlmostEqual(model.predict(0.0), 0.8, delta=0.05)

    def test_not_enough_days(self):
        pairs = self.build(0.8, 0.13, days=4)
        with self.assertRaises(forecast.ForecastUnavailable):
            forecast.fit(pairs, 16.0)

    def test_flat_data_has_no_slope(self):
        pairs = [(5.0, 1.5) for _ in range(20)]
        with self.assertRaises(forecast.ForecastUnavailable):
            forecast.fit(pairs, 16.0)

    def test_one_outlier_does_not_throw_the_fit_out(self):
        pairs = self.build(0.8, 0.13, noise=0.01, seed=7)
        # A day with the freezer defrosting, or guests over.
        pairs.append((10.0, 9.0))
        model = forecast.fit(pairs, 16.0)
        self.assertLess(model.dropped, 3, "the outlier should have been dropped")
        self.assertAlmostEqual(model.slope, 0.13, delta=0.05)

    def test_band_grows_with_noise(self):
        quiet = forecast.fit(self.build(0.8, 0.13, noise=0.01), 16.0)
        noisy = forecast.fit(self.build(0.8, 0.13, noise=0.4, seed=3), 16.0)
        self.assertGreater(noisy.band(10.0), quiet.band(10.0))

    def test_never_predicts_negative_gas(self):
        model = forecast.fit(self.build(0.8, 0.13, noise=0.02), 16.0)
        self.assertGreaterEqual(model.predict(-100), 0.0)

    def test_baseline_is_never_negative(self):
        pairs = [(float(i), 0.001 * i) for i in range(20)]
        model = forecast.fit(pairs, 16.0)
        self.assertGreaterEqual(model.baseline, 0.0)

    def test_the_model_reports_its_own_provenance(self):
        model = forecast.fit(self.build(0.8, 0.13, noise=0.02), 16.0)
        described = model.to_dict()
        self.assertEqual(described["days_used"], 21)
        self.assertIn("sigma_m3", described)
        self.assertIn("r_squared", described)


if __name__ == "__main__":
    unittest.main()

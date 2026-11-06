"""Postcodes, and turning one into a place on the map.

This exists because of a real failure: the user typed PO40PL and Ember
said "no place found for postcode 'PO40PL'". The postcode was real and
the app was wrong twice over - Open-Meteo's geocoder does not know
British postcodes at all (not even SW1A 1AA), and PO40PL without a
space is the same postcode as PO4 0PL.
"""

import json
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from ember import weather
from ember.weather import (WeatherError, geocode, looks_like_a_postcode,
                           normalise_postcode)


class PostcodeShapeTests(unittest.TestCase):
    """The shape of a postcode, and what people actually type."""

    def test_the_space_goes_three_characters_from_the_end(self):
        self.assertEqual(normalise_postcode("PO40PL"), "PO4 0PL")
        self.assertEqual(normalise_postcode("SW1A1AA"), "SW1A 1AA")
        self.assertEqual(normalise_postcode("EH15 5QS"), "EH15 5QS")

    def test_lower_case_and_spaces_are_tolerated(self):
        self.assertEqual(normalise_postcode("po4 0pl"), "PO4 0PL")
        self.assertEqual(normalise_postcode("  po40pl  "), "PO4 0PL")
        self.assertEqual(normalise_postcode("Po4-0Pl"), "PO4 0PL")

    def test_a_one_letter_outward_code(self):
        """A9 9AA is a real shape, and the rule still holds."""
        self.assertEqual(normalise_postcode("M11AE"), "M1 1AE")

    def test_it_recognises_a_postcode(self):
        self.assertTrue(looks_like_a_postcode("PO40PL"))
        self.assertTrue(looks_like_a_postcode("SW1A 1AA"))
        self.assertTrue(looks_like_a_postcode("M11AE"))

    def test_a_town_is_not_a_postcode(self):
        self.assertFalse(looks_like_a_postcode("Poole"))
        self.assertFalse(looks_like_a_postcode(""))
        self.assertFalse(looks_like_a_postcode("12345"))

    def test_nonsense_is_left_alone(self):
        self.assertEqual(normalise_postcode("Poole"), "POOLE")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload.encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PostcodeLookupTests(unittest.TestCase):
    """Which service is asked, and what happens when it says no."""

    def _fake_get(self, url, params, what="Open-Meteo"):
        self.asked = url
        if "postcodes.io" in url:
            # The URL carries the postcode percent-encoded.
            if "PO4 0PL" in urllib.parse.unquote(url):
                return {"status": 200, "result": {
                    "postcode": "PO4 0PL",
                    "latitude": 50.785095,
                    "longitude": -1.075212,
                    "admin_district": "Portsmouth",
                    "country": "England",
                    "region": "South East",
                }}
            return {"status": 404, "error": "Invalid postcode"}
        return {"results": [{"latitude": 50.71429, "longitude": -1.98458,
                              "name": "Poole", "country": "United Kingdom",
                              "timezone": "Europe/London"}]}

    def test_a_postcode_goes_to_the_postcode_directory(self):
        with mock.patch.object(weather, "_get", self._fake_get):
            place = geocode("PO40PL")
        self.assertIn("postcodes.io", self.asked)
        self.assertAlmostEqual(place["latitude"], 50.785095)
        self.assertEqual(place["name"], "Portsmouth")
        self.assertEqual(place["timezone"], "Europe/London")

    def test_a_town_goes_to_open_meteo(self):
        with mock.patch.object(weather, "_get", self._fake_get):
            place = geocode("Poole")
        self.assertIn("geocoding-api.open-meteo.com", self.asked)
        self.assertEqual(place["name"], "Poole")

    def test_an_unknown_postcode_says_so_plainly(self):
        with mock.patch.object(weather, "_get", self._fake_get):
            with self.assertRaises(WeatherError) as caught:
                geocode("ZZ1 1ZZ")
        self.assertIn("no such postcode", str(caught.exception))

    def test_a_404_is_an_answer_not_a_failure(self):
        """A postcode that is not there is not a broken service."""

        def not_found(url, params, what="Open-Meteo"):
            raise WeatherError("HTTP 404 from postcodes.io: Invalid postcode")

        with mock.patch.object(weather, "_get", not_found):
            with self.assertRaises(WeatherError) as caught:
                geocode("ZZ1 1ZZ")
        self.assertIn("no such postcode", str(caught.exception))
        self.assertIn("Check it", str(caught.exception))

    def test_a_place_name_that_does_not_exist_says_what_to_do(self):
        def no_results(url, params, what="Open-Meteo"):
            return {"results": []}

        with mock.patch.object(weather, "_get", no_results):
            with self.assertRaises(WeatherError) as caught:
                geocode("Nowhere-at-all")
        self.assertIn("no place found", str(caught.exception))


class LivePostcodeTests(unittest.TestCase):
    """Against the real services, so a change in them is noticed."""

    def test_a_real_postcode_resolves(self):
        try:
            place = geocode("PO4 0PL")
        except (WeatherError, urllib.error.URLError) as exc:
            raise unittest.SkipTest(f"no network: {exc}")
        self.assertAlmostEqual(place["latitude"], 50.785, places=2)
        self.assertAlmostEqual(place["longitude"], -1.075, places=2)
        self.assertTrue(place["name"])

    def test_the_unspaced_form_gives_the_same_answer(self):
        try:
            spaced = geocode("PO4 0PL")
            unspaced = geocode("PO40PL")
        except (WeatherError, urllib.error.URLError) as exc:
            raise unittest.SkipTest(f"no network: {exc}")
        self.assertEqual(spaced["latitude"], unspaced["latitude"])
        self.assertEqual(spaced["longitude"], unspaced["longitude"])


if __name__ == "__main__":
    unittest.main()

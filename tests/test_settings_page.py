"""The settings page and the discovery behind it.

The point of this feature is that nobody has to remember to run a probe
and copy a file. You type the two things you have - a Hive login and an
Octopus API key - and Ember works out the rest.

So these tests are mostly about one question: **given only the key, does
Ember find the account, the meter point, the meters, the tariff and the
unit?** Everything else on the page is furniture.
"""

import json
import tempfile
import time
import threading
from datetime import datetime, timedelta, timezone
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ember import discover
from ember.config import Settings
from ember.dashboard import Dashboard
from ember.discover import DiscoveryError, HiveFlows, discover_octopus
from ember.history import History
from ember.hive import Hive as HiveApi
from ember.octopus import Octopus, OctopusError
from ember.server import EmberServer


# --------------------------------------------------------------------------
# Stands-ins for the services, with the reply shapes they really send
# --------------------------------------------------------------------------

CHROMIUM = "/root/.cache/ms-playwright/chromium-1243/chrome-linux/chrome"


def _chromium_path():
    import os
    if os.path.isfile(CHROMIUM):
        return CHROMIUM
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if os.path.isfile(candidate):
            return candidate
    return None


class FakeOctopus:
    """The Octopus client, with the replies the live service sends.

    The numbers are the ones from the real account: account A-3B178E7A,
    meter point 8920115204, two meters because of an exchange, the
    "Octopus 18M Fixed" tariff at 8.4378p/kWh and 28.388535p/day.
    """

    def __init__(self, api_key, account):
        self.api_key = api_key
        self.account = account

    def accounts(self):
        return ["A-3B178E7A"]

    def gas_agreements(self):
        """The account's meters, retired one first.

        This is the shape the real client returns after a meter
        exchange: the old meter is still on the record, and it reads in
        the *old* unit. A fake that put the live meter first would
        never catch a discovery that picks the wrong one.
        """
        return [{
            "mprn": "8920115204",
            "serial": "E6S13783422161",
            "serials": ["E6E09927562343", "E6S13783422161"],
            "meter_type": "SMETS2",
            "consumption_units": "M3",
            "live_serial": "E6S13783422161",
            "products": ["OE-FIX-18M-26-09-11"],
            "product_code": "OE-FIX-18M-26-09-11",
            "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "agreements": [],
            "valid_from": "2026-09-10T23:00:00Z",
            "valid_to": "",
        }]

    def find_tariff(self, mprn):
        """The REST reply, which is the one that answers first.

        This is the shape the real client returns when the REST meter
        point endpoint answers, and it is the one that matters: the
        GraphQL reply is only reached as a fallback. An earlier version
        of this fake returned the GraphQL shape, which meant the test
        passed without the REST path ever being exercised.
        """
        return {"source": f"gas-meter-points/{mprn}/",
                "product_code": "OE-FIX-18M-26-09-11",
                "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                "standing_charge": None,
                "display_name": "",
                "meter_type": "SMETS2",
                "consumption_units": "M3",
                "serials": ["E6E09927562343", "E6S13783422161"]}

    def rates_now(self, product, tariff):
        return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

    def get(self, path, params=None):
        return {}


class FakeHive:
    """The Hive client, with the two-step sign-in the live service does."""

    #: The last instance, so a test can see what it was asked for.
    last = None

    def __init__(self, needs_code=True):
        self.needs_code = needs_code
        self.username = ""
        self.password = ""
        self.saved = None
        FakeHive.last = self

    def save_session(self, path):
        """Write a real file, so the page can see that it is there."""
        self.saved = Path(path)
        self.saved.parent.mkdir(parents=True, exist_ok=True)
        # The real session file records the username, because the
        # device registration needs it later.
        self.saved.write_text(json.dumps({"tokens": {"token": "t"},
                                          "username": self.username}),
                              encoding="utf-8")

    def login(self, email, password):
        self.username = email
        self.password = password
        if self.needs_code:
            return {"status": "needs_code", "session": "session-abc",
                    "username": "USER_ID_FOR_SRP",
                    "message": "Hive has sent a two-step code to your phone."}
        return {"status": "signed_in", "tokens": {"token": "t"},
                "rooms": ["Dining Room", "Kitchen", "Lounge"],
                "device": "device remembered"}

    def complete_code(self, session, username, code, email=""):
        self.username = email or username
        if code != "123456":
            raise discover.HiveError("Invalid code or auth state for user")
        return {"status": "signed_in", "tokens": {"token": "t"},
                "rooms": ["Dining Room", "Kitchen", "Lounge"],
                "device": "device remembered"}

    def remember_device(self):
        """The device registration needs a username, or it cannot work."""
        if not self.username:
            return "could not register the device (Username is required)"
        return "device remembered"

    def read_state(self):
        return {"rooms": [{"name": "Dining Room"}, {"name": "Kitchen"},
                          {"name": "Lounge"}]}


# --------------------------------------------------------------------------
# The discovery itself
# --------------------------------------------------------------------------

class OctopusDiscoveryTests(unittest.TestCase):
    """Given the key alone, everything else is worked out."""

    def setUp(self):
        self.original = discover.Octopus
        discover.Octopus = FakeOctopus

    def tearDown(self):
        discover.Octopus = self.original

    def test_the_account_is_found_rather_than_asked_for(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["octopus_account"], "A-3B178E7A")

    def test_the_meter_point_is_found(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["gas_mprn"], "8920115204")

    def test_both_meters_are_kept(self):
        """A meter exchange leaves the old meter's history behind."""
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["gas_meter_serials"],
                         "E6E09927562343,E6S13783422161")

    def test_the_tariff_is_found(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["gas_product_code"],
                         "OE-FIX-18M-26-09-11")
        self.assertEqual(found["settings"]["gas_tariff_code"],
                         "G-1R-OE-FIX-18M-26-09-11-A")

    def test_the_unit_comes_from_the_meter_record(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["summary"]["unit"], "m3")
        self.assertEqual(found["settings"]["gas_meter_type"], "SMETS2")
        self.assertIn("meter record", found["summary"]["unit_reason"])

    def test_the_unit_setting_is_left_to_the_dashboard(self):
        """The dashboard cross-checks the meter against the readings.

        Pinning the unit would skip that check, so the discovery records
        the meter's *type* and puts the unit back to "work it out".
        """
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["gas_unit"], "auto")

    def test_the_discovery_reports_that_it_reset_the_unit(self):
        """It has to say so, rather than quietly overriding whatever
        was there. The clearing itself is checked by the test below."""
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            found = discover_octopus("sk_live_test")
        finally:
            discover.Octopus = original
        # The discovery always puts the unit back to "work it out", so
        # the stale pin cannot survive it.
        self.assertEqual(found["settings"]["gas_unit"], "auto")
        self.assertIn("unit_reset", found["summary"])

    def test_a_stale_pin_from_an_older_discovery_is_cleared(self):
        """A settings file written by the old discovery may carry a unit
        that came from the wrong meter. Leaving it would keep the wrong
        number on the dashboard for exactly the people who ran it."""
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            settings.set("gas_unit", "kwh")
            settings.save()
            original = discover.Octopus
            discover.Octopus = FakeOctopus
            try:
                found = discover_octopus("sk_live_test")
                settings.update(found["settings"])
                settings.save()
            finally:
                discover.Octopus = original
            self.assertEqual(settings.get("gas_unit"), "auto")
            self.assertEqual(settings.get("gas_meter_type"), "SMETS2")
        finally:
            tmp.cleanup()

    def test_the_rates_are_reported(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["summary"]["unit_rate_p"], 8.4378)
        self.assertEqual(found["summary"]["standing_charge_p"], 28.388535)

    def test_the_key_is_never_echoed_back(self):
        """The key goes in; it does not come out to be shown around."""
        found = discover_octopus("sk_live_test")
        self.assertNotIn("sk_live_test", json.dumps(found["summary"]))

    def test_a_named_account_is_used(self):
        """For the rare key that can see more than one account."""
        found = discover_octopus("sk_live_test", "A-3B178E7A")
        self.assertEqual(found["settings"]["octopus_account"], "A-3B178E7A")

    def test_a_named_account_the_key_cannot_see_is_refused(self):
        with self.assertRaises(DiscoveryError) as caught:
            discover_octopus("sk_live_test", "A-0000000X")
        self.assertIn("cannot see", str(caught.exception))

    def test_the_summary_lists_the_accounts_the_key_can_see(self):
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["summary"]["accounts"], ["A-3B178E7A"])

    def test_no_key_is_an_error_not_a_crash(self):
        with self.assertRaises(DiscoveryError):
            discover_octopus("")

    def test_a_key_that_sees_no_account_says_so(self):
        class Blind(FakeOctopus):
            def accounts(self):
                return []
        discover.Octopus = Blind
        with self.assertRaises(DiscoveryError) as caught:
            discover_octopus("sk_live_wrong")
        self.assertIn("did not recognise", str(caught.exception))


class SessionPathTests(unittest.TestCase):
    """Where a new session goes."""

    def test_it_follows_the_data_directory(self):
        import os
        original = os.environ.get("EMBER_DATA_DIR")
        os.environ["EMBER_DATA_DIR"] = "/tmp/ember-test-data"
        try:
            self.assertEqual(discover.default_session_path(),
                             "/tmp/ember-test-data/hive-session.json")
        finally:
            if original is None:
                os.environ.pop("EMBER_DATA_DIR", None)
            else:
                os.environ["EMBER_DATA_DIR"] = original

    def test_the_server_writes_where_it_looks(self):
        """The path the app reads from is the path the app writes to."""
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            settings.set("hive_session_file",
                         str(Path(tmp.name) / "session.json"))
            history = History(Path(tmp.name) / "history.db")
            dashboard = Dashboard(settings, history, None)
            server = EmberServer(("127.0.0.1", 0), settings, dashboard)
            try:
                self.assertEqual(server._session_path(),
                                 str(Path(tmp.name) / "session.json"))
            finally:
                server.server_close()
                history.close()
        finally:
            tmp.cleanup()


class HiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.original = discover.Hive
        discover.Hive = FakeHive
        self.tmp = tempfile.TemporaryDirectory()
        # The session goes to a temporary place. Without this the tests
        # write a session file into the real data directory, which is
        # not a thing a test should be doing to somebody's machine.
        self.session = str(Path(self.tmp.name) / "hive-session.json")

    def tearDown(self):
        discover.Hive = self.original
        self.tmp.cleanup()

    def _flows(self):
        return HiveFlows(session_path=self.session)

    def test_a_sign_in_that_needs_a_code_says_so(self):
        flows = self._flows()
        result = flows.start("you@example.com", "secret")
        self.assertEqual(result["status"], "needs_code")
        self.assertTrue(result.get("flow"))

    def test_the_code_finishes_the_sign_in(self):
        flows = self._flows()
        started = flows.start("you@example.com", "secret")
        finished = flows.complete_code(started["flow"], "123456")
        self.assertEqual(finished["status"], "signed_in")
        self.assertEqual(len(finished["rooms"]), 3)

    def test_a_wrong_code_raises_a_readable_error(self):
        """A wrong code is a HiveError, not a crash and not a silence."""
        flows = self._flows()
        started = flows.start("you@example.com", "secret")
        with self.assertRaises(discover.HiveError) as caught:
            flows.complete_code(started["flow"], "000000")
        self.assertIn("Invalid code", str(caught.exception))

    def test_a_flow_can_only_be_used_once(self):
        flows = self._flows()
        started = flows.start("you@example.com", "secret")
        flows.complete_code(started["flow"], "123456")
        with self.assertRaises(DiscoveryError):
            flows.complete_code(started["flow"], "123456")

    def test_an_unknown_flow_is_refused(self):
        flows = self._flows()
        with self.assertRaises(DiscoveryError):
            flows.complete_code("made-up", "123456")

    def test_an_expired_flow_is_refused(self):
        flows = HiveFlows(ttl=0.0, session_path=self.session)
        started = flows.start("you@example.com", "secret")
        with self.assertRaises(DiscoveryError):
            flows.complete_code(started["flow"], "123456")


# --------------------------------------------------------------------------
# The page and its routes
# --------------------------------------------------------------------------

class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        # The Hive session goes to a temporary place, not to the real
        # data directory.
        cls.settings.set("hive_session_file", str(data / "hive-session.json"))
        cls.history = History(data / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)

        # The dashboard's own Hive client must not touch the network in
        # a test. The sign-in fake writes a session file, and the poll
        # would then try to use it against the live API.
        cls.original_api = HiveApi.api
        cls.original_refresh = HiveApi.refresh

        def fake_api(self, path):
            return {"products": [], "devices": []}

        def fake_refresh(self):
            return True

        HiveApi.api = fake_api
        HiveApi.refresh = fake_refresh

        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path, body):
        request = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_the_settings_page_is_served(self):
        status, body = self.get("/settings")
        self.assertEqual(status, 200)
        self.assertIn("Ember", body)
        self.assertIn('id="hive_email"', body)
        self.assertIn('id="octopus_api_key"', body)

    def test_the_stylesheet_is_served(self):
        status, body = self.get("/static/settings.css")
        self.assertEqual(status, 200)
        self.assertIn(".card", body)

    def test_the_script_is_served(self):
        status, body = self.get("/static/settings.js")
        self.assertEqual(status, 200)
        self.assertIn("localStorage", body)

    def test_the_theme_is_shared_with_the_dashboard(self):
        """The palette lives in one file, so the two pages cannot drift."""
        _, theme = self.get("/static/theme.css")
        _, app = self.get("/static/app.css")
        self.assertIn("--ember: #ef8a4a", theme)
        self.assertIn("/static/theme.css", app)

    def test_the_status_says_what_is_missing(self):
        status, body = self.get("/api/discover/status")
        payload = json.loads(body)
        self.assertIn("Octopus API key", payload["missing"])
        self.assertIn("postcode", payload["missing"])

    def test_the_status_does_not_ask_for_the_meter_point(self):
        """The meter point is worked out from the key - it is not typed."""
        self.settings.set("octopus_api_key", "sk_live_test")
        self.settings.set("gas_mprn", "8920115204")
        try:
            _, body = self.get("/api/discover/status")
            payload = json.loads(body)
            self.assertNotIn("Octopus API key", payload["missing"])
            self.assertFalse([m for m in payload["missing"] if "meter point" in m])
        finally:
            self.settings.set("octopus_api_key", "")
            self.settings.set("gas_mprn", "")

    def test_the_status_never_carries_a_secret(self):
        self.settings.set("octopus_api_key", "sk_live_secret")
        try:
            _, body = self.get("/api/discover/status")
            self.assertNotIn("sk_live_secret", body)
        finally:
            self.settings.set("octopus_api_key", "")

    def test_the_settings_still_save(self):
        status, payload = self.post("/api/settings", {"monthly_budget": 42.0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["monthly_budget"], 42.0)
        self.settings.set("monthly_budget", 60.0)

    def test_a_malformed_content_length_is_refused_not_a_crash(self):
        request = urllib.request.Request(
            self.url("/api/settings"), data=b"{}",
            headers={"Content-Type": "application/json",
                     "Content-Length": "banana"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body)

    def test_a_negative_content_length_is_refused_not_a_hang(self):
        request = urllib.request.Request(
            self.url("/api/settings"), data=b"{}",
            headers={"Content-Type": "application/json",
                     "Content-Length": "-1"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body)

    def test_an_unknown_setting_is_refused(self):
        status, payload = self.post("/api/settings", {"nonsense": 1})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_a_bad_discovery_answer_is_reported_not_raised(self):
        status, payload = self.post("/api/discover/octopus", {"api_key": ""})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

    def test_the_octopus_discovery_stores_what_it_finds(self):
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            status, payload = self.post("/api/discover/octopus",
                                        {"api_key": "sk_live_test"})
        finally:
            discover.Octopus = original
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["mprn"], "8920115204")
        self.assertEqual(self.settings.get("gas_mprn"), "8920115204")
        # The meter's type is recorded; the unit is left for the
        # dashboard, which cross-checks it against the readings.
        self.assertEqual(self.settings.get("gas_meter_type"), "SMETS2")
        self.assertEqual(self.settings.get("gas_unit"), "auto")

    def test_the_octopus_discovery_stores_the_key(self):
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            self.post("/api/discover/octopus", {"api_key": "sk_live_test"})
        finally:
            discover.Octopus = original
        self.assertEqual(self.settings.get("octopus_api_key"), "sk_live_test")
        self.settings.set("octopus_api_key", "")
        self.settings.set("gas_mprn", "")
        self.settings.set("gas_meter_serials", "")
        self.settings.set("gas_product_code", "")
        self.settings.set("gas_tariff_code", "")
        self.settings.set("gas_unit", "auto")

    def test_the_hive_sign_in_says_it_needs_a_code(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            status, payload = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
        finally:
            discover.Hive = original
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "needs_code")
        self.assertTrue(payload["result"].get("flow"))

    def test_the_saved_password_is_used_when_the_box_is_empty(self):
        """Signing in again must not mean typing the password again.

        The password is in the settings file and is never sent to the
        browser, so the box on the page is empty every time. An empty
        box therefore means "use the one you have" - which is what
        makes recovering from a dead session a one-click job.
        """
        self.settings.set("hive_email", "you@example.com")
        self.settings.set("hive_password", "already-saved")
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            status, payload = self.post("/api/discover/hive",
                                        {"email": "", "password": ""})
        finally:
            discover.Hive = original
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "needs_code")

    def test_a_typed_password_is_used_in_preference_to_the_saved_one(self):
        self.settings.set("hive_email", "you@example.com")
        self.settings.set("hive_password", "already-saved")
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            self.post("/api/discover/hive",
                      {"email": "you@example.com", "password": "typed-now"})
        finally:
            discover.Hive = original
        self.assertEqual(discover.Hive, FakeHive)

    def test_the_saved_password_is_used_when_the_box_is_empty(self):
        """Signing in again must not mean typing the password again.

        The password is in the settings file and is never sent to the
        browser, so the box on the page is empty every time. An empty
        box therefore means "use the one you have" - which is what
        makes recovering from a dead session a one-click job.
        """
        self.settings.set("hive_email", "you@example.com")
        self.settings.set("hive_password", "already-saved")
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            status, payload = self.post("/api/discover/hive",
                                        {"email": "", "password": ""})
        finally:
            discover.Hive = original
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "needs_code")

    def test_a_typed_password_is_used_in_preference_to_the_saved_one(self):
        self.settings.set("hive_email", "you@example.com")
        self.settings.set("hive_password", "already-saved")
        original = discover.Hive
        FakeHive.last = None
        discover.Hive = FakeHive
        try:
            self.post("/api/discover/hive",
                      {"email": "you@example.com", "password": "typed-now"})
        finally:
            discover.Hive = original
        self.assertIsNotNone(FakeHive.last)
        self.assertEqual(FakeHive.last.password, "typed-now")

    def test_the_code_finishes_the_sign_in(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            _, started = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
            status, payload = self.post("/api/discover/hive/code", {
                "flow": started["result"]["flow"], "code": "123456"})
        finally:
            discover.Hive = original
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "signed_in")
        self.assertEqual(len(payload["result"]["rooms"]), 3)

    def test_a_wrong_code_is_a_400_not_a_crash(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            _, started = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
            status, payload = self.post("/api/discover/hive/code", {
                "flow": started["result"]["flow"], "code": "000000"})
        finally:
            discover.Hive = original
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_a_direct_sign_in_keeps_the_login_for_next_time(self):
        """A sign-in that needs no code still saves the login."""
        class NoCode(FakeHive):
            def login(self, email, password):
                result = FakeHive.login(self, email, password)
                result["status"] = "signed_in"
                result.pop("session", None)
                return result

        original = discover.Hive
        discover.Hive = NoCode
        try:
            status, payload = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
        finally:
            discover.Hive = original
            self.settings.set("hive_email", "")
            self.settings.set("hive_password", "")
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "signed_in")

    def test_the_sign_in_keeps_the_login_for_next_time(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
        finally:
            discover.Hive = original
        self.assertEqual(self.settings.get("hive_email"), "you@example.com")
        self.assertEqual(self.settings.get("hive_password"), "secret")
        self.settings.set("hive_email", "")
        self.settings.set("hive_password", "")

    def test_the_status_lists_the_files(self):
        _, body = self.get("/api/discover/status")
        payload = json.loads(body)
        names = [f["name"] for f in payload["files"]]
        self.assertIn("settings", names)
        self.assertIn("Hive session", names)
        self.assertIn("history", names)

    def test_a_refresh_returns_the_state(self):
        status, payload = self.post("/api/discover/refresh", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("errors", payload["state"])


class SettingsPageBrowserTests(unittest.TestCase):
    """Drive the page in a browser.

    The unit tests above check the pieces. These check the page: that
    the button works, that the answer comes back, and that the browser
    ends up knowing what it needs to know.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        chromium = _chromium_path()
        if not chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.set("hive_session_file", str(data / "hive-session.json"))
        cls.history = History(data / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)

        # The dashboard's own Hive client must not touch the network in
        # a test. The sign-in fake writes a session file, and the poll
        # would then try to use it against the live API.
        cls.original_api = HiveApi.api
        cls.original_refresh = HiveApi.refresh

        def fake_api(self, path):
            return {"products": [], "devices": []}

        def fake_refresh(self):
            return True

        HiveApi.api = fake_api
        HiveApi.refresh = fake_refresh

        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()
        HiveApi.api = cls.original_api
        HiveApi.refresh = cls.original_refresh

    def _page(self):
        page = self.browser.new_page(viewport={"width": 900, "height": 1000})
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.wait_for_timeout(700)
        return page

    def test_the_octopus_button_finds_the_account_and_shows_it(self):
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            page = self._page()
            page.fill("#octopus_api_key", "sk_live_test")
            page.click("#octopus-look")
            page.wait_for_timeout(1200)
            text = page.inner_text("#octopus-result")
            state = page.inner_text("#octopus-state")
            page.close()
        finally:
            discover.Octopus = original
        self.assertIn("8920115204", text)
        self.assertIn("A-3B178E7A", text)
        self.assertIn("OE-FIX-18M-26-09-11", text)
        self.assertIn("E6S13783422161", text)
        self.assertIn("8920115204", state)

    def test_the_key_is_stored_on_the_server_after_a_lookup(self):
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            page = self._page()
            page.fill("#octopus_api_key", "sk_live_test")
            page.click("#octopus-look")
            page.wait_for_timeout(1200)
            page.close()
        finally:
            discover.Octopus = original
        self.assertEqual(self.settings.get("octopus_api_key"), "sk_live_test")
        self.assertEqual(self.settings.get("gas_mprn"), "8920115204")
        self.settings.set("octopus_api_key", "")
        self.settings.set("gas_mprn", "")
        self.settings.set("gas_meter_serials", "")
        self.settings.set("gas_product_code", "")
        self.settings.set("gas_tariff_code", "")
        self.settings.set("gas_unit", "auto")

    def test_a_failed_lookup_shows_the_reason_and_keeps_the_key(self):
        """A failure must say why, and must not throw the key away."""
        class Blind(FakeOctopus):
            def accounts(self):
                return []
        original = discover.Octopus
        discover.Octopus = Blind
        try:
            page = self._page()
            page.fill("#octopus_api_key", "sk_live_wrong")
            page.click("#octopus-look")
            page.wait_for_timeout(1200)
            text = page.inner_text("#octopus-result")
            kept = page.input_value("#octopus_api_key")
            page.close()
        finally:
            discover.Octopus = original
        self.assertIn("did not recognise", text)
        self.assertEqual(kept, "sk_live_wrong")

    def test_the_hive_sign_in_asks_for_a_code_and_takes_it(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            page = self._page()
            page.fill("#hive_email", "you@example.com")
            page.fill("#hive_password", "secret")
            page.click("#hive-signin")
            page.wait_for_timeout(1000)
            asking = page.is_visible("#hive-code")
            page.fill("#hive_code_input", "123456")
            page.click("#hive-code-send")
            page.wait_for_timeout(1200)
            text = page.inner_text("#hive-result")
            state = page.inner_text("#hive-state")
            page.close()
        finally:
            discover.Hive = original
        self.assertTrue(asking, "the page did not ask for the code")
        self.assertIn("Signed in", text)
        self.assertIn("signed in", state)

    def test_a_wrong_code_is_shown_not_swallowed(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            page = self._page()
            page.fill("#hive_email", "you@example.com")
            page.fill("#hive_password", "secret")
            page.click("#hive-signin")
            page.wait_for_timeout(900)
            page.fill("#hive_code_input", "000000")
            page.click("#hive-code-send")
            page.wait_for_timeout(1000)
            text = page.inner_text("#hive-result")
            page.close()
        finally:
            discover.Hive = original
        self.assertIn("Invalid code", text)

    def test_the_banner_updates_after_a_lookup(self):
        """The banner is a snapshot, so it has to be re-read."""
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            page = self._page()
            before = page.inner_text("#banner")
            page.fill("#octopus_api_key", "sk_live_test")
            page.click("#octopus-look")
            page.wait_for_timeout(1500)
            after = page.inner_text("#banner")
            page.close()
        finally:
            discover.Octopus = original
        self.assertIn("Octopus API key", before)
        self.assertNotIn("Octopus API key", after)

    def test_the_file_list_updates_after_a_lookup(self):
        """The settings file is written by the lookup, so it appears."""
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            page = self._page()
            page.fill("#octopus_api_key", "sk_live_test")
            page.click("#octopus-look")
            page.wait_for_timeout(1500)
            rows = page.evaluate(
                """() => Array.from(document.querySelectorAll('#files div'))
                         .map(el => [el.textContent, el.className])""")
            page.close()
        finally:
            discover.Octopus = original
        settings_row = [r for r in rows if r[0].startswith("settings")]
        self.assertEqual(len(settings_row), 1)
        self.assertNotIn("missing", settings_row[0][1])

    def test_the_numbers_save(self):
        page = self._page()
        page.fill("#monthly_budget", "77")
        page.click("#numbers-save")
        page.wait_for_timeout(700)
        text = page.inner_text("#numbers-result")
        page.close()
        self.assertIn("Saved", text)
        self.assertEqual(self.settings.get("monthly_budget"), 77.0)
        self.settings.set("monthly_budget", 60.0)

    def test_a_secret_is_not_in_the_page_after_a_successful_sign_in(self):
        """The browser forgets it once the server has it."""
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            page = self._page()
            page.fill("#hive_email", "you@example.com")
            page.fill("#hive_password", "hunter2-secret")
            page.click("#hive-signin")
            page.wait_for_timeout(900)
            page.fill("#hive_code_input", "123456")
            page.click("#hive-code-send")
            page.wait_for_timeout(1200)
            stored = page.evaluate(
                """() => window.localStorage.getItem('ember.settings.v1') || ''""")
            page.close()
        finally:
            discover.Hive = original
        self.assertNotIn("hunter2-secret", stored)

    def test_a_secret_does_not_come_back_on_the_next_keystroke(self):
        """Clearing the stored copy is not enough on its own.

        The form is remembered on every keystroke, so a secret that is
        only removed from the local storage comes straight back the
        moment you type anything at all.
        """
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            page = self._page()
            page.fill("#hive_email", "you@example.com")
            page.fill("#hive_password", "hunter2-secret")
            page.click("#hive-signin")
            page.wait_for_timeout(900)
            page.fill("#hive_code_input", "123456")
            page.click("#hive-code-send")
            page.wait_for_timeout(1200)
            # Type something else entirely.
            page.fill("#panel_name", "Home")
            page.wait_for_timeout(400)
            stored = page.evaluate(
                """() => window.localStorage.getItem('ember.settings.v1') || ''""")
            in_field = page.input_value("#hive_password")
            page.close()
        finally:
            discover.Hive = original
        self.assertNotIn("hunter2-secret", stored)
        self.assertEqual(in_field, "")




class NoSecretLeavesTheProcessTests(unittest.TestCase):
    """Nothing secret may come back out of an endpoint.

    The Hive refresh token is a long-lived credential for the account,
    so a sign-in result that carries it into an HTTP response is a
    serious leak - and it is the kind of leak that looks harmless in
    the code because the dict is called "result".
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.set("hive_session_file", str(data / "hive-session.json"))
        cls.history = History(data / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)

        # The dashboard's own Hive client must not touch the network in
        # a test. The sign-in fake writes a session file, and the poll
        # would then try to use it against the live API.
        cls.original_api = HiveApi.api
        cls.original_refresh = HiveApi.refresh

        def fake_api(self, path):
            return {"products": [], "devices": []}

        def fake_refresh(self):
            return True

        HiveApi.api = fake_api
        HiveApi.refresh = fake_refresh

        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def post(self, path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_a_sign_in_does_not_hand_back_the_tokens(self):
        class TokenHive(FakeHive):
            def login(self, email, password):
                result = FakeHive.login(self, email, password)
                if result.get("status") == "signed_in":
                    result["tokens"] = {"token": "ID-TOKEN-SECRET",
                                        "accessToken": "ACCESS-SECRET",
                                        "refreshToken": "REFRESH-SECRET"}
                return result

            def complete_code(self, session, username, code, email=""):
                result = FakeHive.complete_code(self, session, username, code,
                                                email)
                result["tokens"] = {"token": "ID-TOKEN-SECRET",
                                    "refreshToken": "REFRESH-SECRET"}
                return result

        original = discover.Hive
        discover.Hive = TokenHive
        try:
            status, body = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
            self.assertEqual(status, 200)
            self.assertNotIn("ID-TOKEN-SECRET", body)
            self.assertNotIn("REFRESH-SECRET", body)
            self.assertNotIn("tokens", body)

            _, started = json.loads(body), None
            flow = json.loads(body)["result"]["flow"]
            status, body = self.post("/api/discover/hive/code", {
                "flow": flow, "code": "123456"})
            self.assertEqual(status, 200)
            self.assertNotIn("ID-TOKEN-SECRET", body)
            self.assertNotIn("REFRESH-SECRET", body)
            self.assertNotIn("tokens", body)
        finally:
            discover.Hive = original
            self.settings.set("hive_email", "")
            self.settings.set("hive_password", "")

    def test_the_cognito_session_is_not_handed_back(self):
        """The session can answer the MFA challenge. It stays here."""
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            status, body = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "secret"})
        finally:
            discover.Hive = original
            self.settings.set("hive_email", "")
            self.settings.set("hive_password", "")
        self.assertEqual(status, 200)
        self.assertNotIn("session-abc", body)
        self.assertNotIn("USER_ID_FOR_SRP", body)

    def test_the_password_is_not_handed_back(self):
        original = discover.Hive
        discover.Hive = FakeHive
        try:
            status, body = self.post("/api/discover/hive", {
                "email": "you@example.com", "password": "my-secret-password"})
        finally:
            discover.Hive = original
            self.settings.set("hive_email", "")
            self.settings.set("hive_password", "")
        self.assertEqual(status, 200)
        self.assertNotIn("my-secret-password", body)

    def test_the_api_key_is_not_handed_back(self):
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            status, body = self.post("/api/discover/octopus",
                                      {"api_key": "sk_live_never_show_me"})
        finally:
            discover.Octopus = original
            self.settings.set("octopus_api_key", "")
            self.settings.set("gas_mprn", "")
            self.settings.set("gas_meter_serials", "")
            self.settings.set("gas_product_code", "")
            self.settings.set("gas_tariff_code", "")
            self.settings.set("gas_unit", "auto")
        self.assertEqual(status, 200)
        self.assertNotIn("sk_live_never_show_me", body)

    def test_the_saved_settings_are_not_handed_back(self):
        self.settings.set("octopus_api_key", "sk_live_saved_secret")
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/discover/status")
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
        finally:
            self.settings.set("octopus_api_key", "")
        self.assertNotIn("sk_live_saved_secret", body)


class TwoStepSignInTests(unittest.TestCase):
    """The two-step path has to work the way the one-step path does."""

    def setUp(self):
        self.original = discover.Hive
        discover.Hive = FakeHive
        self.tmp = tempfile.TemporaryDirectory()
        self.session = str(Path(self.tmp.name) / "hive-session.json")

    def tearDown(self):
        discover.Hive = self.original
        self.tmp.cleanup()

    def test_a_wrong_code_does_not_end_the_sign_in(self):
        """One slip should not mean waiting for another SMS."""
        flows = HiveFlows(session_path=self.session)
        started = flows.start("you@example.com", "secret")
        with self.assertRaises(discover.HiveError):
            flows.complete_code(started["flow"], "000000")
        # The same flow, with the code typed properly this time.
        finished = flows.complete_code(started["flow"], "123456")
        self.assertEqual(finished["status"], "signed_in")

    def test_the_device_is_remembered_after_a_code(self):
        """An unremembered device means an SMS code every time."""
        flows = HiveFlows(session_path=self.session)
        started = flows.start("you@example.com", "secret")
        finished = flows.complete_code(started["flow"], "123456")
        self.assertIn("device remembered", finished.get("device", ""))

    def test_the_session_records_the_username(self):
        flows = HiveFlows(session_path=self.session)
        started = flows.start("you@example.com", "secret")
        flows.complete_code(started["flow"], "123456")
        saved = json.loads(Path(self.session).read_text(encoding="utf-8"))
        self.assertEqual(saved.get("username"), "you@example.com")


class TariffNotFoundTests(unittest.TestCase):
    """A missing tariff must not throw away everything else."""

    def setUp(self):
        self.original = discover.Octopus
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        discover.Octopus = self.original
        self.tmp.cleanup()

    def test_what_was_found_is_still_returned(self):
        class NoTariff(FakeOctopus):
            def find_tariff(self, mprn):
                found = FakeOctopus.find_tariff(self, mprn)
                found["product_code"] = ""
                found["tariff_code"] = ""
                return found

        discover.Octopus = NoTariff
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["settings"]["octopus_account"], "A-3B178E7A")
        self.assertEqual(found["settings"]["gas_mprn"], "8920115204")
        self.assertEqual(found["settings"]["gas_meter_serials"],
                         "E6E09927562343,E6S13783422161")
        self.assertTrue(found["summary"]["notes"])
        self.assertIn("tariff", found["summary"]["missing"])

    def test_it_does_not_raise(self):
        class NoTariff(FakeOctopus):
            def find_tariff(self, mprn):
                found = FakeOctopus.find_tariff(self, mprn)
                found["product_code"] = ""
                found["tariff_code"] = ""
                return found

        discover.Octopus = NoTariff
        found = discover_octopus("sk_live_test")  # must not raise
        self.assertIn("tariff", found["summary"]["missing"])


class UnitFromTheRestPathTests(unittest.TestCase):
    """The unit has to come from the meter's record, on the REST path.

    The REST meter point endpoint is the one that answers first, so a
    discovery that only worked on the GraphQL fallback would never run
    with a live key.
    """

    def test_the_rest_reply_carries_the_meter_type_and_units(self):
        from ember.octopus import _tariff_from, declared_unit
        reply = {
            "mprn": "8920115204",
            "meter_type": "SMETS2",
            "consumption_units": "M3",
            "agreed_product_codes": ["OE-FIX-18M-26-09-11"],
            "meters": [{"serial_number": "E6E09927562343"},
                       {"serial_number": "E6S13783422161"}],
            "single_register_tariff": {
                "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A"},
        }
        found = _tariff_from(reply)
        self.assertEqual(found["meter_type"], "SMETS2")
        self.assertEqual(found["consumption_units"], "M3")
        self.assertEqual(found["serials"],
                         ["E6E09927562343", "E6S13783422161"])
        self.assertEqual(declared_unit(found["meter_type"],
                                        found["consumption_units"]), "m3")

    def test_the_discovery_uses_it(self):
        """The fake returns the REST shape, so this is the REST path."""
        original = discover.Octopus
        discover.Octopus = FakeOctopus
        try:
            found = discover_octopus("sk_live_test")
        finally:
            discover.Octopus = original
        self.assertEqual(found["summary"]["unit"], "m3")
        self.assertEqual(found["settings"]["gas_meter_type"], "SMETS2")
        self.assertIn("meter record", found["summary"]["unit_reason"])


class MeterExchangeTests(unittest.TestCase):
    """After a meter exchange, the meter's own record still lists both.

    The retired meter keeps its own type, and its own unit. So the type
    has to come from the meter that is in service - not from the first
    one in the list, and not from the oldest.
    """

    METERS = [
        {"serial": "E6E09927562343", "meter_type": "SMETS1",
         "consumption_units": "KWH",
         "active_from": "2019-04-01T00:00:00Z",
         "active_to": "2026-11-03T00:00:00Z",
         "installation_date": "2019-03-01T00:00:00Z"},
        {"serial": "E6S13783422161", "meter_type": "SMETS2",
         "consumption_units": "M3",
         "active_from": "2026-11-03T00:00:00Z",
         "active_to": "",
         "installation_date": "2026-10-20T00:00:00Z"},
    ]

    def test_the_timestamp_shapes_are_all_read(self):
        """Octopus uses several, and a wrong date picks the wrong meter."""
        from ember.octopus import _parse_moment
        for text in ("2026-11-03T00:00:00Z", "2026-11-03T00:00:00+00:00",
                     "2026-11-03T00:00:00", "2026-11-03",
                     "2026-11-03T00:00:00.000Z", "2026-11-03T00:00:00.500Z"):
            self.assertIsNotNone(_parse_moment(text),
                                 f"could not read {text!r}")
        self.assertEqual(_parse_moment("2026-11-03T00:00:00Z").hour, 0)
        self.assertEqual(_parse_moment("2026-11-03").day, 3)
        self.assertIsNone(_parse_moment(""))
        self.assertIsNone(_parse_moment("rubbish"))

    def test_the_meter_in_service_is_picked(self):
        from ember.octopus import pick_live_meter
        live = pick_live_meter(self.METERS)
        self.assertEqual(live["serial"], "E6S13783422161")
        self.assertEqual(live["meter_type"], "SMETS2")

    def test_the_unit_follows_the_meter_in_service(self):
        from ember.octopus import declared_unit, pick_live_meter
        live = pick_live_meter(self.METERS)
        self.assertEqual(declared_unit(live["meter_type"],
                                        live["consumption_units"]), "m3")

    def test_it_picks_the_live_meter_even_when_it_is_listed_first(self):
        """List position is not evidence."""
        from ember.octopus import pick_live_meter
        meters = list(reversed(self.METERS))
        live = pick_live_meter(meters)
        self.assertEqual(live["serial"], "E6S13783422161")

    def test_the_live_serial_is_used_whatever_the_list_order(self):
        """The recorded payload with the live meter listed first."""
        from ember.octopus import _find_gas_meter_points
        payload = {
            "properties": [{
                "gas_meter_points": [{
                    "mprn": "8920115204",
                    "meters": [
                        {"serial_number": "E6S13783422161",
                         "meter_type": "SMETS2",
                         "active_from": "2026-11-03T00:00:00Z"},
                        {"serial_number": "E6E09927562343",
                         "meter_type": "SMETS1",
                         "active_to": "2026-11-03T00:00:00Z"},
                    ],
                }],
            }],
        }
        points = _find_gas_meter_points(payload)
        self.assertEqual(points[0]["live_serial"], "E6S13783422161")
        self.assertEqual(points[0]["meter_type"], "SMETS2")

    def test_it_falls_back_to_the_newest_when_no_window_is_given(self):
        from ember.octopus import pick_live_meter
        meters = [
            {"serial": "OLD", "meter_type": "SMETS1",
             "installation_date": "2019-03-01T00:00:00Z"},
            {"serial": "NEW", "meter_type": "SMETS2",
             "installation_date": "2026-10-20T00:00:00Z"},
        ]
        self.assertEqual(pick_live_meter(meters)["serial"], "NEW")

    def test_with_no_dates_at_all_the_last_one_wins(self):
        """No dates means no evidence - so it falls to the later one.

        The retired meter is the one that was there first, and the new
        meter is the one that was added, so the later one is the one in
        service.
        """
        from ember.octopus import pick_live_meter
        meters = [
            {"serial": "RETIRED", "meter_type": "SMETS1"},
            {"serial": "LIVE", "meter_type": "SMETS2"},
        ]
        self.assertEqual(pick_live_meter(meters)["serial"], "LIVE")

    def test_the_account_payload_picks_the_live_meter(self):
        """The real client's account payload, after an exchange."""
        from ember.octopus import _find_gas_meter_points
        payload = {
            "properties": [{
                "gas_meter_points": [{
                    "mprn": "8920115204",
                    "meters": [
                        {"serial_number": "E6E09927562343",
                         "meter_type": "SMETS1",
                         "active_to": "2026-11-03T00:00:00Z"},
                        {"serial_number": "E6S13783422161",
                         "meter_type": "SMETS2",
                         "active_from": "2026-11-03T00:00:00Z"},
                    ],
                }],
            }],
        }
        points = _find_gas_meter_points(payload)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["meter_type"], "SMETS2")
        self.assertEqual(points[0]["live_serial"], "E6S13783422161")
        self.assertEqual(points[0]["serials"],
                         ["E6E09927562343", "E6S13783422161"])


class RealClientOnAnExchangeTests(unittest.TestCase):
    """The real client, on a recorded reply from a meter exchange.

    This is the case that matters: the account payload lists the
    retired meter first, and the retired meter reads in the old unit.
    A discovery that took the first meter's type would read cubic
    metres as kilowatt hours - and the cost would come out about five
    times too small.
    """

    def setUp(self):
        self.original = discover.Octopus
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        discover.Octopus = self.original
        self.tmp.cleanup()

    def test_the_real_account_payload_gives_the_live_meter(self):
        payload = {
            "properties": [{
                "gas_meter_points": [{
                    "mprn": "8920115204",
                    "agreed_product_codes": ["OE-FIX-18M-26-09-11"],
                    "meters": [
                        {"serial_number": "E6E09927562343",
                         "meter_type": "SMETS1",
                         "consumption_units": "KWH",
                         "active_from": "2019-04-01T00:00:00Z",
                         "active_to": "2026-11-03T00:00:00Z"},
                        {"serial_number": "E6S13783422161",
                         "meter_type": "SMETS2",
                         "consumption_units": "M3",
                         "active_from": "2026-11-03T00:00:00Z",
                         "active_to": ""},
                    ],
                }],
            }],
        }

        class Recorded(Octopus):
            def account_details(self):
                return payload

            def find_tariff(self, mprn):
                return {"source": "the GraphQL API",
                        "product_code": "OE-FIX-18M-26-09-11",
                        "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                        "standing_charge": 28.388535,
                        "unit_rate": 8.4378,
                        "meter_type": "SMETS2",
                        "consumption_units": "M3",
                        "serials": ["E6E09927562343", "E6S13783422161"],
                        "meters": []}

            def rates_now(self, product, tariff):
                return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

            def accounts(self):
                return ["A-3B178E7A"]

        discover.Octopus = Recorded
        found = discover_octopus("sk_live_test")
        self.assertEqual(found["summary"]["unit"], "m3")
        self.assertEqual(found["settings"]["gas_meter_type"], "SMETS2")
        self.assertEqual(found["settings"]["gas_meter_serial"],
                         "E6S13783422161")
        # Both serials are still read, so the history stays continuous.
        self.assertEqual(found["settings"]["gas_meter_serials"],
                         "E6E09927562343,E6S13783422161")

    def test_the_cost_is_not_five_times_too_small(self):
        """The number that actually matters: the money."""
        payload = {
            "properties": [{
                "gas_meter_points": [{
                    "mprn": "8920115204",
                    "meters": [
                        {"serial_number": "E6E09927562343",
                         "meter_type": "SMETS1", "consumption_units": "KWH",
                         "active_to": "2026-11-03T00:00:00Z"},
                        {"serial_number": "E6S13783422161",
                         "meter_type": "SMETS2", "consumption_units": "M3",
                         "active_from": "2026-11-03T00:00:00Z"},
                    ],
                }],
            }],
        }

        class Recorded(Octopus):
            def account_details(self):
                return payload

            def find_tariff(self, mprn):
                return {"source": "the GraphQL API",
                        "product_code": "OE-FIX-18M-26-09-11",
                        "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                        "standing_charge": 28.388535, "unit_rate": 8.4378,
                        "meter_type": "SMETS2", "consumption_units": "M3",
                        "serials": ["E6E09927562343", "E6S13783422161"],
                        "meters": []}

            def rates_now(self, product, tariff):
                return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

            def accounts(self):
                return ["A-3B178E7A"]

        discover.Octopus = Recorded
        found = discover_octopus("sk_live_test")
        unit = found["summary"]["unit"]
        calorific = 11.16
        rate = 8.4378
        cubic_metres = 2.433
        energy_kwh = (cubic_metres * calorific if unit == "m3"
                      else cubic_metres)
        cost = energy_kwh * rate / 100.0
        # Reading m3 as kWh would give a fifth of this.
        self.assertAlmostEqual(cost, 2.29, delta=0.05)
        self.assertGreater(cost, 2.0)


def _seed_two_weeks(history):
    """Fill the store with readings and outside temperatures.

    Without both halves the degree-day model has nothing to fit and
    the forecast is simply unavailable, which would make any assertion
    about it vacuous.
    """
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    for day in range(14):
        date = midnight - timedelta(days=day)
        readings = []
        for half in range(48):
            start = date + timedelta(minutes=30 * half)
            readings.append({
                "ts": start.timestamp(),
                "value": 2.433 / 48.0,
                "consumption": 2.433 / 48.0,
            })
        history.record_gas(readings)
        history.record_poll(date.timestamp(), False, 17.5,
                            6.0 - (day % 7) * 0.5)


def _FakeNoNetwork(meter_type):
    """A Octopus client that answers without touching the network."""

    class NoNetwork:
        def __init__(self, api_key, account):
            self.api_key = api_key
            self.account = account

        def gas_agreements(self):
            return [{
                "mprn": "8920115204", "serial": "E6S13783422161",
                "serials": ["E6E09927562343", "E6S13783422161"],
                "meter_type": meter_type, "consumption_units": "",
                "live_serial": "E6S13783422161",
                "product_code": "OE-FIX-18M-26-09-11",
                "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                "agreements": [], "valid_from": "", "valid_to": "",
            }]

        def find_tariff(self, mprn):
            return {"source": "a recorded reply",
                    "product_code": "OE-FIX-18M-26-09-11",
                    "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                    "standing_charge": 28.388535, "unit_rate": 8.4378,
                    "meter_type": meter_type, "consumption_units": "",
                    "serials": ["E6E09927562343", "E6S13783422161"],
                    "meters": []}

        def current_gas_product(self):
            return "OE-FIX-18M-26-09-11", "G-1R-OE-FIX-18M-26-09-11-A"

        def consumption(self, mprn, serial, start, end, **kwargs):
            return []

        def rates_now(self, product, tariff):
            return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

        detect_unit = staticmethod(Octopus.detect_unit)

    return NoNetwork


def _a_day_of_readings(daily_total, half_hours=48):
    """A day of readings in the shape the history store keeps."""
    now = time.time()
    per = daily_total / half_hours
    return [{"ts": now - 86400 + index * (86400 / half_hours),
             "value": per, "provisional": False}
            for index in range(half_hours)]


class UnknownUnitTests(unittest.TestCase):
    """When nothing is known about the unit, nothing is claimed.

    The dashboard refusing to name a unit is only honest if it also
    refuses to publish money computed on an assumption it will not
    make. A budget, a forecast and a cost are all numbers that need
    the unit.
    """

    def _payload(self, meter_type=""):
        """A dashboard with no network, so the poll does not fail.

        The point of these tests is what the dashboard does with a unit
        it does not know, and a failed poll would tell us nothing.
        """
        import ember.dashboard as dashboard_module

        class NoNetwork:
            def __init__(self, api_key, account):
                self.api_key = api_key
                self.account = account

            def gas_agreements(self):
                return [{
                    "mprn": "8920115204",
                    "serial": "E6S13783422161",
                    "serials": ["E6E09927562343", "E6S13783422161"],
                    "meter_type": meter_type,
                    "consumption_units": "",
                    "live_serial": "E6S13783422161",
                    "product_code": "OE-FIX-18M-26-09-11",
                    "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                    "agreements": [], "valid_from": "", "valid_to": "",
                }]

            def find_tariff(self, mprn):
                return {"source": "a recorded reply",
                        "product_code": "OE-FIX-18M-26-09-11",
                        "tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
                        "standing_charge": 28.388535, "unit_rate": 8.4378,
                        "meter_type": meter_type, "consumption_units": "",
                        "serials": ["E6E09927562343", "E6S13783422161"],
                        "meters": []}

            def current_gas_product(self):
                return "OE-FIX-18M-26-09-11", "G-1R-OE-FIX-18M-26-09-11-A"

            def consumption(self, mprn, serial, start, end, **kwargs):
                return []

            def rates_now(self, product, tariff):
                return {"unit_rate_p": 8.4378, "standing_charge_p": 28.388535}

            detect_unit = staticmethod(Octopus.detect_unit)

        original = dashboard_module.Octopus
        dashboard_module.Octopus = NoNetwork
        self.addCleanup(lambda: setattr(dashboard_module, "Octopus", original))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        settings = Settings(data / "settings.json")
        settings.update({
            "octopus_api_key": "sk_live_test",
            "octopus_account": "A-3B178E7A",
            "gas_mprn": "8920115204",
            "gas_meter_serials": "E6E09927562343,E6S13783422161",
            "gas_product_code": "OE-FIX-18M-26-09-11",
            "gas_tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            # No meter type, and the unit left to the readings.
            "gas_meter_type": meter_type,
            "gas_unit": "auto",
        })
        history = History(data / "history.db")
        self.addCleanup(history.close)
        return settings, history

    def test_nothing_is_published_when_the_unit_is_unknown(self):
        settings, history = self._payload()
        dashboard = Dashboard(settings, history, None)
        dashboard.poll()
        payload = dashboard.payload()
        gas = payload["gas"]
        self.assertTrue(gas.get("unit_unknown"))
        self.assertEqual(gas["unit"], "unknown")
        self.assertEqual(gas["unit_label"], "")
        self.assertIsNone(gas["cost_today"])
        self.assertIsNone(gas["energy_p"])
        self.assertIsNone(gas["today_kwh"])
        self.assertIsNotNone(gas.get("cost_note"))
        # And nothing derived from it either.
        self.assertIsNone(payload["budget"].get("spend"))
        self.assertIsNone(payload["forecast"].get("tomorrow_cost"))
        self.assertIsNone(payload["forecast"].get("tomorrow_kwh"))

    def test_the_readings_are_consulted_when_they_can_be(self):
        """The cross-check has to actually fire, not just exist."""
        settings, history = self._payload()
        history.record_gas(_a_day_of_readings(2.433))
        dashboard = Dashboard(settings, history, None)
        dashboard.poll()
        payload = dashboard.payload()
        gas = payload["gas"]
        # No meter type, but the readings are enough to tell.
        self.assertEqual(gas["unit"], "m3")
        self.assertFalse(gas.get("unit_unknown"))
        self.assertIsNotNone(gas["cost_today"])

    def test_no_money_at_all_when_there_is_no_unit_rate(self):
        """A zero would read as "nothing spent", which is a different
        claim from "not known"."""
        settings, history = self._payload()
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus

        # A client that finds the meter but cannot read the rates.
        class NoRateClient(_FakeNoNetwork("")):
            def rates_now(self, product, tariff):
                raise OctopusError("no rates for this tariff")

        dashboard_module.Octopus = NoRateClient
        try:
            dashboard = Dashboard(settings, history, None)
            dashboard.poll()
            payload = dashboard.payload()
        finally:
            dashboard_module.Octopus = original
        gas = payload["gas"]
        self.assertIsNone(gas["cost_today"])
        self.assertIsNone(gas["energy_p"])

    def test_nothing_reported_today_is_not_zero_energy(self):
        settings, history = self._payload()
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus
        dashboard_module.Octopus = _FakeNoNetwork("")
        try:
            dashboard = Dashboard(settings, history, None)
            dashboard.poll()
            payload = dashboard.payload()
        finally:
            dashboard_module.Octopus = original
        gas = payload["gas"]
        self.assertFalse(gas["today_reported"])
        self.assertIsNone(gas["energy_p"])
        self.assertIsNone(gas["cost_today"])

    def test_the_forecast_publishes_no_money_either(self):
        """The forecast half of the same fix.

        Without a unit there is no kWh and no cost tomorrow either. The
        forecast is made available on purpose here, so the assertions
        cannot be skipped by the forecast simply not being there.
        """
        import ember.dashboard as dashboard_module
        original_octopus = dashboard_module.Octopus
        original_choose = dashboard_module.Dashboard._choose_unit
        settings, history = self._payload()

        # A fortnight of readings and outside temperatures, so the
        # degree-day model has something to fit.
        _seed_two_weeks(history)

        def unknown_unit(detected, meter_type, self_calorific, declared=""):
            return ("unknown", 1.0,
                    "the meter record does not say what it measures", "none")

        dashboard_module.Octopus = _FakeNoNetwork("")
        dashboard_module.Dashboard._choose_unit = staticmethod(unknown_unit)
        try:
            dashboard = Dashboard(settings, history, None)
            dashboard.poll()
            payload = dashboard.payload()
        finally:
            dashboard_module.Octopus = original_octopus
            # Re-wrap it: reading a staticmethod off the class gives the
            # plain function, and putting that back would make it an
            # ordinary method and pass "self" in as an argument.
            dashboard_module.Dashboard._choose_unit = staticmethod(original_choose)
        forecast = payload["forecast"]
        self.assertTrue(forecast.get("available"),
                        "the forecast must be available for this test to mean "
                        "anything: " + repr(forecast)[:200])
        self.assertIsNone(forecast["tomorrow_kwh"])
        self.assertIsNone(forecast["tomorrow_cost"])

    def test_a_known_meter_type_says_so_and_agrees(self):
        settings, history = self._payload("SMETS2")
        history.record_gas(_a_day_of_readings(2.433))
        dashboard = Dashboard(settings, history, None)
        dashboard.poll()
        payload = dashboard.payload()
        gas = payload["gas"]
        self.assertEqual(gas["unit"], "m3")
        self.assertIn("agrees", gas.get("unit_reason", ""))


class UnitPinTests(unittest.TestCase):
    """What the discovery does with a unit that is already pinned."""

    def setUp(self):
        self.original = discover.Octopus
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        discover.Octopus = self.original
        self.tmp.cleanup()

    def test_a_pin_is_left_alone_when_the_meter_cannot_be_read(self):
        """No evidence either way, so the user's choice stands."""
        class NoMeterType(FakeOctopus):
            def gas_agreements(self):
                rows = FakeOctopus.gas_agreements(self)
                for row in rows:
                    row["meter_type"] = ""
                    row["consumption_units"] = ""
                return rows

            def find_tariff(self, mprn):
                found = FakeOctopus.find_tariff(self, mprn)
                found["meter_type"] = ""
                found["consumption_units"] = ""
                return found

        discover.Octopus = NoMeterType
        found = discover_octopus("sk_live_test", "", "m3")
        self.assertNotIn("gas_unit", found["settings"])
        self.assertEqual(found["summary"].get("unit_kept"), "m3")
        self.assertIn("left as it is", found["summary"]["unit_reason"])

    def test_a_pin_is_reset_when_the_meter_can_be_read(self):
        discover.Octopus = FakeOctopus
        found = discover_octopus("sk_live_test", "", "kwh")
        self.assertEqual(found["settings"]["gas_unit"], "auto")
        self.assertIn("unit_reset", found["summary"])


class _SlideFixture:
    """Serves the dashboard in a browser for the slide tests.

    A mixin rather than a test case of its own, so each scenario gets
    its own server and its own browser.
    """

    METER_TYPE = ""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        chromium = _chromium_path()
        if not chromium:
            raise unittest.SkipTest("no chromium available")

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.update({
            "octopus_api_key": "sk_live_test",
            "octopus_account": "A-3B178E7A",
            "gas_mprn": "8920115204",
            "gas_meter_serials": "E6E09927562343,E6S13783422161",
            "gas_product_code": "OE-FIX-18M-26-09-11",
            "gas_tariff_code": "G-1R-OE-FIX-18M-26-09-11-A",
            "gas_meter_type": "",
            "gas_unit": "auto",
        })
        cls.history = History(data / "history.db")
        # No readings and no meter type: nothing knows what the meter
        # reads in, and the panel has to say so rather than guess.
        cls.dashboard = Dashboard(cls.settings, cls.history, None)

        import ember.dashboard as dashboard_module
        cls.original_octopus = dashboard_module.Octopus
        dashboard_module.Octopus = _FakeNoNetwork(cls.METER_TYPE)
        try:
            cls.dashboard.poll()
        finally:
            dashboard_module.Octopus = cls.original_octopus

        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def _text(self, slide):
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1500)
        text = page.evaluate("() => document.querySelector('.slide').innerText")
        page.close()
        return text

    def _kpi(self, slide):
        """The big figure on a slide, exactly as it is written."""
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"http://127.0.0.1:{self.port}/?slide={slide}")
        page.wait_for_timeout(1500)
        text = page.evaluate(
            "() => document.querySelector('.slide .big .kpi').innerText")
        page.close()
        return text.strip()

    def test_the_gas_slide_does_not_claim_the_meter_reads_kwh(self):
        text = self._text("gas")
        self.assertNotIn("meter reports kWh", text)

    def test_the_today_slide_does_not_claim_the_meter_reads_kwh(self):
        text = self._text("today")
        self.assertNotIn("meter reports kWh", text)

    def test_the_today_slide_figure_carries_no_unit_with_no_number(self):
        """A dash with a unit beside it reads as "nothing, in m3"."""
        self.assertEqual(self._kpi("today"), "\u2014")

    def test_the_gas_slide_figure_carries_no_unit_with_no_number(self):
        self.assertEqual(self._kpi("gas"), "\u2014")


class UnknownUnitOnThePageTests(_SlideFixture, unittest.TestCase):
    """What the panel says when it does not know the unit.

    The whole point of refusing to name a unit is that the panel stops
    claiming things. A line underneath saying "meter reports kWh" would
    undo that, so it has to be checked on the screen.
    """

    METER_TYPE = ""

    def test_the_gas_slide_shows_no_money(self):
        text = self._text("gas")
        self.assertNotIn("£0.00", text)
        self.assertNotIn("0.0p", text)

    def test_the_gas_slide_names_no_unit(self):
        """The volume is shown, but not the unit it is in.

        "p/kWh" in the unit rate is not a claim about the meter - the
        tariff is priced per kWh whatever the meter reads - so only
        the volume's own label is checked.
        """
        text = self._text("gas")
        self.assertNotIn("m³", text)
        self.assertNotIn("meter reports kWh", text)
        self.assertIn("the unit is unknown", text)

    def test_the_today_slide_names_no_unit(self):
        text = self._text("today")
        self.assertNotIn("m³", text)


class KnownUnitNoReadingTests(_SlideFixture, unittest.TestCase):
    """The unit is known, but there is no reading for today yet.

    A dash for today's gas must not come with a unit beside it: that
    would read as "no gas, in m3", which is a claim rather than an
    absence.
    """

    METER_TYPE = "SMETS2"

    def test_the_today_slide_figure_is_a_bare_dash(self):
        self.assertEqual(self._kpi("today"), "\u2014")

    def test_the_gas_slide_figure_is_a_bare_dash(self):
        self.assertEqual(self._kpi("gas"), "\u2014")

    def test_it_does_not_say_the_day_had_no_gas(self):
        text = self._text("today")
        self.assertIn("No reading yet", text)

    def test_the_gas_tile_has_no_unit_beside_its_dash(self):
        """The tile says "- m3" if the unit is left on."""
        text = self._text("gas")
        self.assertNotIn("\u2014m³", text)


class UnitWordingOnThePageTests(unittest.TestCase):
    """What the settings page says about the unit.

    The wording matters: if the page says Ember "will work it out from
    the readings" when the truth is that a unit the user chose is being
    kept as it is, the page is telling them something false.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        chromium = _chromium_path()
        if not chromium:
            raise unittest.SkipTest("no chromium available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(Path(cls.tmp.name) / "settings.json")
        cls.history = History(Path(cls.tmp.name) / "history.db")
        cls.dashboard = Dashboard(cls.settings, cls.history, None)
        cls.server = EmberServer(("127.0.0.1", 0), cls.settings, cls.dashboard)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=chromium,
            args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.history.close()
        cls.tmp.cleanup()

    def _result_text(self, summary):
        page = self.browser.new_page(viewport={"width": 1000, "height": 900})
        page.route("**/api/discover/octopus", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "summary": summary,
                              "settings": {"gas_unit": "m3"}})))
        page.goto(f"http://127.0.0.1:{self.port}/settings")
        page.fill("#octopus_api_key", "sk_live_test")
        page.click("#octopus-look")
        page.wait_for_timeout(800)
        text = page.evaluate("() => document.getElementById('octopus-result').innerText")
        page.close()
        return text

    def test_a_kept_unit_says_it_is_being_kept(self):
        text = self._result_text({
            "account": "A-3B178E7A", "mprn": "8920115204",
            "serials": ["E6S13783422161"],
            "unit_kept": "m3",
            "unit_reason": ("the meter record does not say what it measures, "
                             "so the unit you have set (m3) is left as it is"),
        })
        self.assertIn("left as it is", text)
        self.assertNotIn("work it out from the readings", text)

    def test_an_unknown_unit_says_the_meter_does_not_say(self):
        text = self._result_text({
            "account": "A-3B178E7A", "mprn": "8920115204",
            "serials": ["E6S13783422161"],
            "unit_reason": "the meter record does not say what it measures",
        })
        self.assertIn("does not say what it measures", text)
        self.assertNotIn("work it out from the readings", text)

    def test_a_reset_unit_says_so(self):
        text = self._result_text({
            "account": "A-3B178E7A", "mprn": "8920115204",
            "serials": ["E6S13783422161"], "meter_type": "SMETS2",
            "unit": "m3",
            "unit_reason": "the meter record says it reads in cubic metres",
            "unit_reset": ("the unit you had set (kwh) has been cleared, so "
                           "Ember can check it against the readings"),
        })
        self.assertIn("cubic metres", text)
        self.assertIn("has been cleared", text)


class NothingKnownShowsNothingTests(unittest.TestCase):
    """With nothing to go on, the money fields must be empty.

    They used to start at zero, which renders as "£0.00" and "0.0p" -
    a claim that nothing was spent, rather than an admission that
    nothing is known.
    """

    def test_no_octopus_means_no_money(self):
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus

        class NoRates(_FakeNoNetwork("")):
            def rates_now(self, product, tariff):
                raise OctopusError("no rates for this tariff")

        dashboard_module.Octopus = NoRates
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            settings.update({"octopus_api_key": "sk_live_test",
                             "octopus_account": "A-3B178E7A",
                             "gas_mprn": "8920115204",
                             "gas_meter_serial": "E6S13783422161"})
            history = History(Path(tmp.name) / "history.db")
            try:
                dashboard = Dashboard(settings, history, None)
                dashboard.poll()
                gas = dashboard.payload()["gas"]
            finally:
                dashboard_module.Octopus = original
        finally:
            tmp.cleanup()
        # No rate came back, so there is no money.
        self.assertIsNone(gas["unit_rate_p"])
        self.assertIsNone(gas["cost_today"])
        self.assertIsNone(gas["energy_p"])

    def test_the_defaults_are_not_zero(self):
        """The starting values themselves, with nothing polled."""
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus
        dashboard_module.Octopus = _FakeNoNetwork("")
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            history = History(Path(tmp.name) / "history.db")
            try:
                dashboard = Dashboard(settings, history, None)
                # Before any poll at all.
                gas = dashboard._poll_gas([], timezone.utc)
            finally:
                dashboard_module.Octopus = original
        finally:
            tmp.cleanup()
        self.assertIsNone(gas["cost_today"])
        self.assertIsNone(gas["energy_p"])
        self.assertIsNone(gas["standing_p"])
        self.assertIsNone(gas["unit_rate_p"])


class BudgetZeroTests(unittest.TestCase):
    """A genuine £0.00 spend is not a missing spend."""

    def test_zero_spend_shows_zero_percent(self):
        """£0.00 of £20.00 is 0%, not a missing number.

        The old check was "if spend and budget", and a zero spend is
        falsy, so a genuine £0.00 read as "not known".
        """
        import ember.dashboard as dashboard_module
        original = dashboard_module.Octopus

        class RateNoStandingCharge(_FakeNoNetwork("SMETS2")):
            def rates_now(self, product, tariff):
                return {"unit_rate_p": 8.4378, "standing_charge_p": None}

        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            settings.update({"monthly_budget": "20.00",
                             "octopus_api_key": "sk_live_test",
                             "octopus_account": "A-3B178E7A",
                             "gas_mprn": "8920115204",
                             "gas_meter_serial": "E6S13783422161"})
            history = History(Path(tmp.name) / "history.db")
            dashboard_module.Octopus = RateNoStandingCharge
            try:
                dashboard = Dashboard(settings, history, None)
                dashboard.poll()
                budget = dashboard.payload().get("budget") or {}
            finally:
                dashboard_module.Octopus = original
            self.assertEqual(budget.get("spend"), 0.0)
            self.assertEqual(budget.get("percent"), 0.0)
        finally:
            tmp.cleanup()


class TempFilePermissionTests(unittest.TestCase):
    """A file with the password in it must never be world-readable."""

    def test_the_settings_file_is_0600(self):
        import os
        import stat
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings(Path(tmp.name) / "settings.json")
            settings.set("octopus_api_key", "sk_live_abc")
            settings.save()
            mode = os.stat(Path(tmp.name) / "settings.json").st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)
        finally:
            tmp.cleanup()

    def test_the_session_file_is_0600(self):
        import os
        import stat
        from ember.hive import Hive
        tmp = tempfile.TemporaryDirectory()
        try:
            hive = Hive()
            hive.tokens = {"token": "t"}
            hive.save_session(Path(tmp.name) / "hive-session.json")
            mode = os.stat(Path(tmp.name) / "hive-session.json").st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)
        finally:
            tmp.cleanup()




if __name__ == "__main__":
    unittest.main()

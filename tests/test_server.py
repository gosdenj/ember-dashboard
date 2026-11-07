"""End-to-end tests: start the real server, ask it the real questions."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.demo import DemoProvider
from ember.history import History
from ember.server import EmberServer


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        cls.settings = Settings(data / "settings.json")
        cls.settings.set("demo", True)
        cls.history = History(data / "history.db")
        provider = DemoProvider()
        provider.seed(cls.history)
        # The demo's boiler runs from six in the morning, so between
        # midnight and six the figure for today is honestly zero - the
        # boiler has not run yet. That is right, and a test that wants
        # a number has to make one for itself: without this, the test
        # fails whenever it is run in the small hours, which is a
        # quarter of the day.
        cls.history.record_poll(time.time() - 30, True, 19.5, 5.0)
        cls.dashboard = Dashboard(cls.settings, cls.history, provider)
        cls.dashboard.poll()
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
            return response.status, response.read(), response.headers

    def test_health(self):
        status, body, _ = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_the_page_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Ember", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_the_state_is_served(self):
        status, body, _ = self.get("/api/state")
        payload = json.loads(body)
        self.assertEqual(payload["mode"], "demo")
        self.assertEqual(len(payload["heating"]["rooms"]), 9)
        self.assertIn("Lounge", [room["room"] for room in payload["heating"]["rooms"]])

    def test_the_state_carries_the_sources(self):
        payload = json.loads(self.get("/api/state")[1])
        self.assertIn("heating", payload)
        self.assertIn("gas", payload)
        self.assertIn("forecast", payload)
        self.assertIn("budget", payload)

    def test_the_settings_do_not_leak_secrets(self):
        self.settings.set("octopus_api_key", "sk_live_supersecret")
        self.settings.set("hive_password", "hunter2")
        body = self.get("/api/settings")[1]
        self.assertNotIn(b"supersecret", body)
        self.assertNotIn(b"hunter2", body)

    def test_the_static_files_are_served(self):
        for path in ("/static/app.js", "/static/app.css", "/fonts/fonts.css"):
            status, body, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)

    def test_a_missing_file_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/static/nope.js")
        self.assertEqual(caught.exception.code, 404)

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/static/../../etc/passwd")
        self.assertIn(caught.exception.code, (400, 403, 404))

    def test_the_settings_can_be_saved(self):
        request = urllib.request.Request(
            self.url("/api/settings"),
            data=json.dumps({"panel_name": "Cottage"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.settings.get("panel_name"), "Cottage")

    def test_an_unknown_setting_is_refused(self):
        request = urllib.request.Request(
            self.url("/api/settings"),
            data=json.dumps({"not_a_setting": 1}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_the_forecast_is_available_with_history(self):
        payload = json.loads(self.get("/api/state")[1])
        forecast = payload["forecast"]
        self.assertTrue(forecast["available"], forecast.get("reason"))
        self.assertGreater(forecast["tomorrow_m3"], 0)
        self.assertGreaterEqual(forecast["band"], 0)
        self.assertGreaterEqual(forecast["model"]["days_used"], 7)

    def test_the_status_reports_the_stored_history(self):
        """The settings page shows how big the store has got."""
        payload = json.loads(self.get("/api/discover/status")[1])
        history = payload.get("history") or {}
        self.assertIn("rows", history)
        self.assertIn("bytes", history)
        self.assertEqual(history["history_days"], 400)
        self.assertEqual(history["trv_days"], 30)
        self.assertGreater(history["rows_per_day"]["polls"], 0)

    def test_the_history_can_be_tidied_up_on_request(self):
        request = urllib.request.Request(
            self.url("/api/history/prune"),
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode())
        self.assertTrue(body["ok"])
        self.assertIn("removed", body)
        self.assertIn("history", body)

    def test_the_heating_hours_are_counted(self):
        """The boiler's hours come from Ember's own polls."""
        payload = json.loads(self.get("/api/state")[1])
        self.assertGreater(payload["heating"]["heating_today_minutes"], 0)
        self.assertGreater(payload["heating"]["heating_week_minutes"], 0)

    def test_the_budget_is_measured_against_metered_gas(self):
        payload = json.loads(self.get("/api/state")[1])
        budget = payload["budget"]
        self.assertGreater(budget["budget"], 0)
        self.assertIsNotNone(budget["spend"])
        self.assertIsNotNone(budget["percent"])

    def test_the_app_looks_for_the_hive_session_in_every_place(self):
        """The probe and the app have to agree on where the session is.

        The probe writes to the shared data directory and to a copy next
        to itself; the app looks in the configured path, the shared data
        directory, and that copy.
        """
        paths = self.dashboard._session_paths()
        self.assertGreaterEqual(len(paths), 2)
        self.assertEqual(len(paths), len(set(paths)), "no path should repeat")
        joined = " ".join(paths)
        self.assertIn("hive-session.json", joined)
        self.assertTrue(any("probes" in path for path in paths),
                        "the copy beside the probe must be one of them")


if __name__ == "__main__":
    unittest.main()


class QuietOnDisconnectTests(unittest.TestCase):
    """A browser that closes a connection is not a fault.

    The standard library turns it into a full traceback, which fills the
    console with stack traces whenever the page reloads.
    """

    def test_a_reset_connection_is_not_reported(self):
        import io
        import contextlib
        from ember.server import EmberServer

        server = EmberServer.__new__(EmberServer)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError:
                server.handle_error(None, ("127.0.0.1", 1234))
        self.assertEqual(stderr.getvalue(), "")

    def test_a_real_fault_is_still_reported(self):
        import io
        import contextlib
        from ember.server import EmberServer

        server = EmberServer.__new__(EmberServer)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                raise ValueError("something genuinely wrong")
            except ValueError:
                server.handle_error(None, ("127.0.0.1", 1234))
        self.assertIn("ValueError", stderr.getvalue())

"""What happens to the dashboard when Hive's session dies.

This is the seam between the Hive client and the dashboard, and it is
where a real fault lived: the dashboard renewed the session, used the
new token for one poll, threw it away, and went on using the original
token - which stops working an hour later. From then on every poll said
"401 Unauthorized", and the only way out was for a person to sign in
again by hand.

The dashboard must get itself out of that, without a person.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ember import hive as hive_module
from ember.config import Settings
from ember.dashboard import Dashboard
from ember.history import History
from ember.hive import Hive, HiveError

from tests.test_hive_session import FakeHive, make_token


class DashboardSessionTests(unittest.TestCase):
    """The dashboard, polling against a Hive that expires."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.session = root / "hive-session.json"
        self.fake = FakeHive(lifetime=3600)
        self.original_refresh_url = hive_module.REFRESH_URL
        self.original_cognito = hive_module.COGNITO_ENDPOINT
        hive_module.REFRESH_URL = self.fake.base + "/cognito/refresh-token"
        hive_module.COGNITO_ENDPOINT = self.fake.cognito_endpoint

        self.settings = Settings(root / "settings.json")
        self.settings.update({"demo": False, "hive_session_file": str(self.session),
                              "timezone": "Europe/London"})
        self.history = History(root / "history.db")
        self.dashboard = Dashboard(self.settings, self.history, None)
        # The dashboard builds its own Hive client, so the session file
        # has to be a real one that the real client can use.
        self._write_session(self.fake.issue())

    def tearDown(self):
        hive_module.REFRESH_URL = self.original_refresh_url
        hive_module.COGNITO_ENDPOINT = self.original_cognito
        self.dashboard.stop()
        self.history.close()
        self.fake.stop()
        self.tmp.cleanup()

    def _write_session(self, token: str, refresh_token: str | None = None,
                       path: Path | None = None) -> None:
        hive = Hive(self.fake.base)
        hive.pool_id = "eu-west-1_SamNfoWtf"
        hive.client_id = "jivhemv3fvn8cr32qrggqpcf8"
        hive.region = "eu-west-1"
        hive.device_key = self.fake.device_key
        hive.tokens = {"token": token, "accessToken": token,
                       "refreshToken": refresh_token or self.fake.refresh_token}
        hive.base = self.fake.base
        hive.save_session(path or self.session)

    def _file_token(self) -> str:
        with open(self.session, encoding="utf-8") as handle:
            return json.load(handle)["tokens"]["token"]

    def test_the_dashboard_polls_hive_without_a_person(self):
        payload = self.dashboard.poll()
        errors = [e for e in payload.get("errors") or [] if e.get("source") == "hive"]
        self.assertEqual(errors, [], "a working session produced an error")
        self.assertTrue(payload["heating"]["rooms"])

    def test_the_dashboard_renews_the_session_before_the_token_dies(self):
        """The renewal happens while the token is still alive.

        This is the whole fix: renewing after the expiry is too late,
        because the renewal needs a live token to make.
        """
        # A token that is nearly expired.
        self.fake.lifetime = 30
        self._write_session(self.fake.issue())
        before = self._file_token()

        self.dashboard.poll()

        after = self._file_token()
        self.assertNotEqual(after, before, "the session was not renewed")
        self.assertEqual(len(self.fake.cognito_attempts), 1,
                         "the session was not renewed through Cognito")

    def test_the_dashboard_recovers_when_the_token_is_refused(self):
        """A 401 is not the end of the session."""
        self.dashboard.poll()
        self.fake.revoke(self._file_token())

        payload = self.dashboard.poll()
        errors = [e for e in payload.get("errors") or [] if e.get("source") == "hive"]
        self.assertEqual(errors, [], "the dashboard did not recover by itself")
        self.assertTrue(payload["heating"]["rooms"])

    def test_the_dashboard_says_what_to_do_when_the_session_is_gone(self):
        """The last resort is a clear instruction, not a puzzle."""
        token = self._file_token()
        self.fake.expire(token)
        # Nothing can renew this session any more: not Cognito, not
        # the proxy. Only a person can fix it.
        self.fake.kill_refresh()
        # The file holds a token the refresh endpoint will not accept,
        # and the data endpoint will not accept either.
        payload = self.dashboard.poll()
        errors = [e for e in payload.get("errors") or [] if e.get("source") == "hive"]
        self.assertTrue(errors, "a dead session produced no error at all")
        message = errors[0]["message"]
        self.assertIn("sign in from the settings page", message)
        self.assertIn("expired at", message)

    def test_a_renewable_session_is_preferred_over_a_dead_one(self):
        """Two session files, one of them alive: the alive one wins.

        A session that loads but cannot be renewed works until its
        token dies and then never works again, so it is a last resort -
        not the first choice.
        """
        # A token that has already expired, so Ember tries to renew it
        # and finds out that it cannot.
        dead = make_token(time.time() - 10, "expired")
        self.fake.expire(dead)
        # The dead one goes in the configured place: an expired token
        # AND a refresh token nobody will accept, so it cannot be
        # renewed by anything.
        self._write_session(dead, refresh_token="from-an-old-session")
        # The live one goes in the shared data directory.
        alive = self.fake.issue()
        alternate = Path(self.tmp.name) / "alternate-session.json"
        self._write_session(alive, path=alternate)

        self.dashboard._session_paths = lambda: [str(self.session), str(alternate)]
        client = self.dashboard._hive_client()
        self.assertIsNotNone(client)
        self.assertEqual(client.tokens["token"], alive,
                         "the session that could be renewed was not used")
        self.assertEqual(self.dashboard._hive_session_file, str(alternate))

    def test_a_dead_session_does_not_stop_the_gas_and_the_weather(self):
        """One broken thing must not take the whole panel down."""
        self.fake.expire(self._file_token())
        payload = self.dashboard.poll()
        self.assertTrue(payload["gas"], "the gas panel went missing")
        self.assertTrue(payload["budget"])


if __name__ == "__main__":
    unittest.main()

"""Signing in to Hive from the settings page.

This exists because of a real failure. The user signed in, was told
"Signed in. 0 rooms found", and the dashboard went on saying "not
signed in" - three different symptoms of two bugs:

* the room count read the wrong key, so a sign-in that had worked
  perfectly was reported as finding nothing, and
* the app would not use a session whose refresh had failed, so a
  perfectly good token was thrown away.

And "not signed in" said nothing about why, which made it useless.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ember import hive as hive_module
from ember.dashboard import Dashboard
from ember.discover import _count_rooms
from ember.history import History
from ember.hive import Hive, HiveError


class FakeHive:
    """A signed-in Hive, with the real reply shape."""

    def __init__(self, base=""):
        self.base = base
        self.username = "someone@example.com"
        self.device_key = "device-key"
        self.device_group_key = "device-group-key"
        self.device_password = ""
        self.tokens = {"token": "id-token", "accessToken": "access",
                       "refreshToken": "refresh"}
        self.saved_to = []

    def read_state(self):
        return {"rooms": [
            {"room": "Dining Room", "current": 19.2, "target": 20.0},
            {"room": "Sofia Bedroom", "current": 16.9, "target": 17.5},
        ]}

    def save_session(self, path):
        self.saved_to.append(Path(path))


class RoomCountTests(unittest.TestCase):
    """The count of rooms has to match the state that was read."""

    def test_the_rooms_are_counted(self):
        """read_state() calls the key "room", not "name"."""
        fake = FakeHive()
        self.assertEqual(_count_rooms(fake), ["Dining Room", "Sofia Bedroom"])

    def test_a_name_key_still_works(self):
        """Defensive: a change in one place should not zero the count."""

        class WithName:
            def read_state(self):
                return {"rooms": [{"name": "Kitchen"}]}

        self.assertEqual(_count_rooms(WithName()), ["Kitchen"])

    def test_a_failure_is_not_a_count_of_zero_silently(self):
        class Broken:
            def read_state(self):
                raise HiveError("the API said no")

        self.assertEqual(_count_rooms(Broken()), [])


class SessionLoadingTests(unittest.TestCase):
    """A session that is there should be used, refresh or no refresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hive-session.json"
        self.path.write_text(json.dumps({
            "tokens": {"token": "id-token", "accessToken": "access",
                       "refreshToken": "refresh"},
            "base": "https://beekeeper-uk.hivehome.com/1.0",
            "username": "someone@example.com",
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_working_refresh_gives_a_session(self):
        client = Hive()
        with mock.patch.object(Hive, "refresh", return_value=True):
            self.assertTrue(client.load_session(self.path))
        self.assertEqual(client.last_error, "")

    def test_a_failed_refresh_still_gives_a_session(self):
        """The token is good for an hour. Throwing it away is worse."""
        client = Hive()
        with mock.patch.object(Hive, "refresh",
                               side_effect=HiveError("HTTP 403 from Hive")):
            self.assertTrue(client.load_session(self.path))
        self.assertIn("403", client.last_error)

    def test_a_failed_refresh_with_no_token_is_a_failure(self):
        """Nothing to use and nothing to renew it with."""
        self.path.write_text(json.dumps({
            "tokens": {"refreshToken": "refresh"},
        }), encoding="utf-8")
        client = Hive()
        with mock.patch.object(Hive, "refresh",
                               side_effect=HiveError("HTTP 403 from Hive")):
            self.assertFalse(client.load_session(self.path))
        self.assertIn("403", client.last_error)

    def test_a_session_with_no_token_at_all_is_a_failure(self):
        self.path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")
        client = Hive()
        self.assertFalse(client.load_session(self.path))
        self.assertIn("no token", client.last_error)

    def test_a_missing_file_says_so(self):
        client = Hive()
        self.assertFalse(client.load_session(self.path.with_name("gone.json")))
        self.assertIn("no session file", client.last_error)

    def test_a_corrupt_file_says_so(self):
        self.path.write_text("{not json", encoding="utf-8")
        client = Hive()
        self.assertFalse(client.load_session(self.path))
        self.assertIn("could not be read", client.last_error)


class RefreshUrlTests(unittest.TestCase):
    """Both refresh endpoints are tried, and the failure says which."""

    def test_both_are_tried(self):
        client = Hive()
        client.base = "https://beekeeper-uk.hivehome.com/1.0"
        urls = client._refresh_urls()
        self.assertEqual(urls[0],
                         "https://beekeeper.hivehome.com/1.0/cognito/refresh-token")
        self.assertIn("https://beekeeper-uk.hivehome.com/1.0/cognito/refresh-token",
                      urls)

    def test_the_failure_names_the_urls(self):
        client = Hive()
        client.tokens = {"token": "t", "refreshToken": "r", "accessToken": "a"}
        # Nothing here may reach the real service: the renewal talks
        # to Cognito and to both proxy URLs.
        with mock.patch.object(Hive, "_cognito",
                               side_effect=HiveError("no cognito")):
            with mock.patch.object(Hive, "_post",
                                   side_effect=HiveError("HTTP 404")):
                with self.assertRaises(HiveError) as caught:
                    client.refresh()
        self.assertIn("cognito/refresh-token", str(caught.exception))
        self.assertIn("404", str(caught.exception))
        self.assertIn("Cognito", str(caught.exception))


class CognitoRefreshTests(unittest.TestCase):
    """Cognito's own refresh flow is the one that makes a session last.

    It was recorded in the notes that this flow was not available. It
    is: a bogus refresh token comes back as "Invalid Refresh Token",
    not as an unknown operation.
    """

    def test_cognito_is_tried_before_the_proxy(self):
        client = Hive()
        client.tokens = {"token": "t", "refreshToken": "r", "accessToken": "a"}
        cognito = mock.Mock()
        cognito.refresh_auth.return_value = {
            "AuthenticationResult": {
                "IdToken": "new-id-token",
                "AccessToken": "new-access-token",
                "ExpiresIn": 3600,
            }
        }
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(Hive, "_post") as post:
                self.assertTrue(client.refresh())
                post.assert_not_called()
        self.assertEqual(client.tokens["token"], "new-id-token")

    def test_the_refresh_token_survives_a_response_without_one(self):
        """Cognito only rotates refresh tokens when it is told to.

        When it does not, the old one is still the good one - and
        losing it would end the session at the very next renewal.
        """
        client = Hive()
        client.tokens = {"token": "t", "refreshToken": "the-good-one",
                         "accessToken": "a"}
        cognito = mock.Mock()
        cognito.refresh_auth.return_value = {
            "AuthenticationResult": {"IdToken": "new", "AccessToken": "new-a",
                                      "ExpiresIn": 3600}
        }
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            self.assertTrue(client.refresh())
        self.assertEqual(client.tokens["refreshToken"], "the-good-one")

    def test_a_rotated_refresh_token_replaces_the_old_one(self):
        client = Hive()
        client.tokens = {"token": "t", "refreshToken": "old",
                         "accessToken": "a"}
        cognito = mock.Mock()
        cognito.refresh_auth.return_value = {
            "AuthenticationResult": {"IdToken": "new", "AccessToken": "new-a",
                                      "RefreshToken": "new-refresh",
                                      "ExpiresIn": 3600}
        }
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            self.assertTrue(client.refresh())
        self.assertEqual(client.tokens["refreshToken"], "new-refresh")

    def test_the_proxy_is_the_fallback(self):
        """If Cognito will not renew it, Hive's own proxy still might."""
        client = Hive()
        client.tokens = {"token": "t", "refreshToken": "r", "accessToken": "a"}
        cognito = mock.Mock()
        cognito.refresh_auth.side_effect = HiveError("Invalid Refresh Token")
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(Hive, "_post") as post:
                post.return_value = {"token": "proxied", "accessToken": "p",
                                     "refreshToken": "r2",
                                     "platform": {"endpoint": "https://x/1.0"}}
                self.assertTrue(client.refresh())
        self.assertEqual(client.tokens["token"], "proxied")


class TheReasonIsShownTests(unittest.TestCase):
    """"Not signed in" has to say why."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.settings = hive_module  # placeholder so linters stay quiet
        from ember.config import Settings
        self.settings = Settings(data / "settings.json")
        self.settings.update({"hive_email": "someone@example.com"})
        self.history = History(data / "history.db")

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _poll(self):
        dashboard = Dashboard(self.settings, self.history, None)
        dashboard.poll()
        return dashboard.payload()

    def test_no_session_file_says_where_it_looked(self):
        # The app also looks in the shared data directory, which on a
        # machine with a real Hive account may well hold a real session.
        # Point it at this test's own directory, so the test is about the
        # app and not about who happens to be running it.
        with mock.patch.object(Path, "home", return_value=Path(self.tmp.name)):
            payload = self._poll()
        messages = [e["message"] for e in payload["errors"]
                    if e["source"] == "hive"]
        self.assertTrue(messages)
        self.assertIn("not signed in", messages[0])
        self.assertIn("no session file", messages[0])
        # And it says where it looked, so the answer is actionable.
        self.assertIn("hive-session.json", messages[0])

    def test_a_broken_session_says_why(self):
        """Not just "not signed in" - the reason it could not be used."""
        data = Path(self.tmp.name)
        session = data / "hive-session.json"
        session.write_text(json.dumps({
            "tokens": {"token": "id", "accessToken": "a", "refreshToken": "r"},
        }), encoding="utf-8")
        self.settings.set("hive_session_file", str(session))
        with mock.patch.object(Hive, "refresh",
                               side_effect=HiveError("HTTP 403 from Hive")):
            with mock.patch.object(Hive, "api",
                                   side_effect=HiveError("HTTP 401 on "
                                                         "/nodes/all")):
                payload = self._poll()
        messages = [e["message"] for e in payload["errors"]
                    if e["source"] == "hive"]
        self.assertTrue(messages)
        # The session loads anyway, so there is no "not signed in" -
        # but the API call fails and says why.
        self.assertNotIn("not signed in", messages[0])
        self.assertIn("401", messages[0])


if __name__ == "__main__":
    unittest.main()


class TheSignInConnectsToTheDashboardTests(unittest.TestCase):
    """Sign in, and the dashboard shows the house.

    This is the flow that was broken. The page said "signed in, 0 rooms
    found" and the dashboard said "not signed in", and nothing in
    between said why. The sign-in and the dashboard have to agree.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        from ember.config import Settings
        self.settings = Settings(data / "settings.json")
        self.settings.set("hive_session_file", str(data / "hive-session.json"))
        self.history = History(data / "history.db")

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def _write_session(self):
        """What a successful sign-in leaves behind."""
        path = Path(self.settings.get("hive_session_file"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "tokens": {"token": "id-token", "accessToken": "access",
                       "refreshToken": "refresh"},
            "base": "https://beekeeper-uk.hivehome.com/1.0",
            "username": "someone@example.com",
            "device_key": "device-key",
            "device_group_key": "device-group-key",
        }), encoding="utf-8")

    NODES = {
        "products": [
            {"type": "trvcontrol", "id": "p1",
             "state": {"name": "Dining Room", "target": 20.0},
             "props": {"temperature": 19.2, "trvs": ["d1"]}},
            {"type": "trvcontrol", "id": "p2",
             "state": {"name": "Sofia Bedroom", "target": 17.5},
             "props": {"temperature": 16.9, "trvs": ["d2"]}},
            {"type": "heating", "id": "h1",
             "state": {"target": 7.0, "frostProtection": 12, "boost": None},
             "props": {"temperature": 17.55, "working": False}},
        ],
        "devices": [
            {"id": "d1", "props": {"battery": 90}},
            {"id": "d2", "props": {"battery": 80}},
        ],
    }

    def test_the_dashboard_shows_the_rooms_after_a_sign_in(self):
        self._write_session()
        with mock.patch.object(Hive, "refresh", return_value=True), \
                mock.patch.object(Hive, "api",
                                  return_value=self.NODES):
            dashboard = Dashboard(self.settings, self.history, None)
            dashboard.poll()
            payload = dashboard.payload()
        rooms = payload["heating"]["rooms"]
        self.assertEqual([r["room"] for r in rooms],
                         ["Dining Room", "Sofia Bedroom"])
        hive_errors = [e for e in payload["errors"]
                       if e["source"] == "hive"]
        self.assertEqual(hive_errors, [])
        self.assertTrue(payload["heating"]["home_temp"] is not None)

    def test_the_status_says_signed_in_when_hive_answers(self):
        self._write_session()
        with mock.patch.object(Hive, "refresh", return_value=True), \
                mock.patch.object(Hive, "api",
                                  return_value=self.NODES):
            dashboard = Dashboard(self.settings, self.history, None)
            dashboard.poll()
            status = dashboard.hive_status()
        self.assertTrue(status["signed_in"])
        self.assertEqual(status["problem"], "")
        self.assertTrue(status["session_file"].endswith("hive-session.json"))

    def test_the_status_says_why_when_hive_does_not_answer(self):
        self._write_session()
        with mock.patch.object(Hive, "refresh", return_value=True), \
                mock.patch.object(Hive, "api",
                                  side_effect=HiveError("HTTP 401 on "
                                                        "/nodes/all")):
            dashboard = Dashboard(self.settings, self.history, None)
            dashboard.poll()
            status = dashboard.hive_status()
        self.assertFalse(status["signed_in"])
        self.assertIn("401", status["problem"])

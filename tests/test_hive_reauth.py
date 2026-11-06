"""When the refresh token is dead, the dashboard signs in again by itself.

A refresh token is a **cache**; the password is the truth. The cache can
go stale for reasons that have nothing to do with the user - Cognito
revokes it, the account's device tracking changes, Hive changes
something on their side - and then the only way back is to sign in
again.

Ember can do that by itself, because the settings page already holds
the password on the server, in a file only the owner can read. The
browser never sees it.

The tests here cover the policy, not the cryptography: that the
dashboard tries, that it says what is needed when it cannot, that it
does not hammer the account, and that a new session is written to disk
so the next poll starts from a live token.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ember import hive as hive_module
from ember.config import Settings
from ember.dashboard import REAUTH_BACKOFF, Dashboard
from ember.history import History
from ember.hive import Hive, HiveError

from tests.test_hive_session import FakeHive, make_token


class ReauthFixture(unittest.TestCase):
    """A dashboard, a session file, and a Hive that will not renew."""

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
        self.settings.update({
            "demo": False,
            "hive_session_file": str(self.session),
            "hive_email": "someone@example.com",
            "hive_password": "the-password",
            "timezone": "Europe/London",
        })
        self.history = History(root / "history.db")
        self.dashboard = Dashboard(self.settings, self.history, None)
        self._write_session()

    def tearDown(self):
        hive_module.REFRESH_URL = self.original_refresh_url
        hive_module.COGNITO_ENDPOINT = self.original_cognito
        self.dashboard.stop()
        self.history.close()
        self.fake.stop()
        self.tmp.cleanup()

    def _write_session(self, token: str | None = None,
                       refresh_token: str | None = None) -> str:
        token = token or self.fake.issue()
        hive = Hive(self.fake.base)
        hive.pool_id = "eu-west-1_SamNfoWtf"
        hive.client_id = "jivhemv3fvn8cr32qrggqpcf8"
        hive.region = "eu-west-1"
        hive.device_key = self.fake.device_key
        hive.tokens = {"token": token, "accessToken": token,
                       "refreshToken": refresh_token or self.fake.refresh_token}
        hive.base = self.fake.base
        hive.save_session(self.session)
        return token

    def _file(self) -> dict:
        with open(self.session, encoding="utf-8") as handle:
            return json.load(handle)

    def _errors(self, payload) -> list:
        return [e for e in payload.get("errors") or [] if e.get("source") == "hive"]

    def _fake_login(self, result: dict | None = None, calls: list | None = None):
        """A stand-in for the sign-in, which is tested on its own.

        It does what the real one does at the seam that matters: it
        leaves a token the data endpoint accepts on the client, and it
        points the client at the endpoint that accepts it.

        Both halves matter. The real sign-in takes the base from the
        platform the sign-in returned, so a stand-in that sets the token
        but not the base leaves the client pointing at the real Hive -
        and the test then depends on what the real Hive says, which is
        not what the test is about.
        """
        fake = self.fake

        def login(client, username, password):
            if calls is not None:
                calls.append((username, password))
            if result is not None:
                return result
            client.tokens = {"token": fake.issue(),
                             "accessToken": fake.issue(),
                             "refreshToken": "a-new-refresh-token"}
            client.base = fake.base
            return {"status": "signed_in"}
        return login


class TheDashboardSignsInAgainTests(ReauthFixture):
    """The cache is stale: use the truth."""

    def test_a_dead_refresh_token_leads_to_a_new_sign_in(self):
        """Nothing can renew this session - so sign in again."""
        self.fake.kill_refresh()
        calls: list = []
        with mock.patch.object(Hive, "login", self._fake_login(calls=calls)):
            payload = self.dashboard.poll()
        self.assertEqual(len(calls), 1, "the dashboard did not sign in again")
        self.assertEqual(calls[0], ("someone@example.com", "the-password"))

    def test_the_dashboard_recovers_by_itself(self):
        """After signing in again, the panel works and says nothing
        about it. A session that renews itself is invisible."""
        self.fake.kill_refresh()
        with mock.patch.object(Hive, "login", self._fake_login()):
            payload = self.dashboard.poll()
        self.assertEqual(self._errors(payload), [],
                         "the dashboard did not recover by itself")
        self.assertTrue(payload["heating"]["rooms"])

    def test_the_new_session_is_written_to_disk(self):
        """Otherwise the next poll starts from the same dead token."""
        self.fake.kill_refresh()
        with mock.patch.object(Hive, "login", self._fake_login()):
            self.dashboard.poll()
        saved = self._file()
        self.assertNotEqual(saved["tokens"]["token"],
                            self.fake.issued[0],
                            "the new session was not saved")
        self.assertEqual(saved["tokens"]["refreshToken"], "a-new-refresh-token")

    def test_it_signs_in_again_with_no_session_file_at_all(self):
        """No file, no token, no refresh - just the login.

        This is the case where the sign-in has to happen before the
        client is even built, so it pins the path that does it.
        """
        self.session.unlink()
        with mock.patch.object(Hive, "login", self._fake_login()):
            payload = self.dashboard.poll()
        self.assertEqual(self._errors(payload), [],
                         "the dashboard did not sign in by itself")
        self.assertTrue(payload["heating"]["rooms"])

    def test_a_working_session_is_not_signed_in_again(self):
        """The refresh is the cheap path; it should be used."""
        calls: list = []
        with mock.patch.object(Hive, "login", self._fake_login(calls=calls)):
            self.dashboard.poll()
        self.assertEqual(calls, [], "it signed in again when it did not have to")

    def test_a_wrong_password_key_is_read_again_and_retried_once(self):
        """The pool's key can change - or the one on disk can be stale.

        Cognito says "Incorrect username or password" for a wrong
        password and for a wrong password key alike, so the one thing
        Ember can do is ask Hive for its sign-in details again and try
        once more.
        """
        self.fake.kill_refresh()
        attempts: list = []

        def login(client, username, password):
            attempts.append((username, password))
            if len(attempts) == 1:
                raise HiveError("NotAuthorizedException: Incorrect username "
                                "or password.")
            client.tokens = {"token": self.fake.issue(),
                             "accessToken": self.fake.issue(),
                             "refreshToken": "a-new-refresh-token"}
            return {"status": "signed_in"}

        reads: list = []
        with mock.patch.object(Hive, "login", login):
            with mock.patch.object(Hive, "read_sso",
                                   lambda _self: reads.append(1)):
                payload = self.dashboard.poll()
        self.assertEqual(len(attempts), 2, "it did not try again")
        self.assertEqual(len(reads), 1, "it did not re-read the sign-in details")
        self.assertEqual(self._errors(payload), [],
                         "the dashboard did not recover")

    def test_a_failed_sign_in_is_not_repeated_straight_away(self):
        """Repeated sign-ins are how an account gets locked."""
        self.fake.kill_refresh()
        calls: list = []
        with mock.patch.object(Hive, "login",
                               self._fake_login(result={"status": "needs_code"},
                                               calls=calls)):
            self.dashboard.poll()
            self.dashboard.poll()
            self.dashboard.poll()
        self.assertEqual(len(calls), 1,
                         "the sign-in was attempted again within the back-off")

    def test_the_back_off_expires(self):
        """After a while, trying again is right."""
        self.fake.kill_refresh()
        calls: list = []
        with mock.patch.object(Hive, "login",
                               self._fake_login(result={"status": "needs_code"},
                                               calls=calls)):
            self.dashboard.poll()
            self.dashboard._last_reauth -= REAUTH_BACKOFF + 1
            self.dashboard.poll()
        self.assertEqual(len(calls), 2, "the sign-in was never tried again")


class WhatItSaysTests(ReauthFixture):
    """When it cannot, it says what is needed."""

    def test_a_code_is_asked_for_by_name(self):
        self.fake.kill_refresh()
        with mock.patch.object(Hive, "login",
                               self._fake_login(result={"status": "needs_code"})):
            payload = self.dashboard.poll()
        errors = self._errors(payload)
        self.assertTrue(errors, "a dead session produced no error at all")
        self.assertIn("one-time code", errors[0]["message"])

    def test_it_says_where_to_go(self):
        self.fake.kill_refresh()
        with mock.patch.object(Hive, "login",
                               self._fake_login(result={"status": "needs_code"})):
            payload = self.dashboard.poll()
        self.assertIn("/settings", self._errors(payload)[0]["message"])

    def test_no_stored_login_is_said_out_loud(self):
        """Without the password there is nothing to sign in with."""
        self.settings.set("hive_password", "")
        self.fake.kill_refresh()
        payload = self.dashboard.poll()
        errors = self._errors(payload)
        self.assertTrue(errors)
        self.assertIn("not stored", errors[0]["message"])

    def test_a_sign_in_that_fails_says_why(self):
        self.fake.kill_refresh()

        def login(self, username, password):
            raise HiveError("the password was wrong")

        with mock.patch.object(Hive, "login", login):
            payload = self.dashboard.poll()
        message = self._errors(payload)[0]["message"]
        self.assertIn("the password was wrong", message)


class WhatGetsWrittenTests(ReauthFixture):
    """The file has to hold the tokens that go with the device.

    The device handshake replaces the tokens with a pair that belongs
    to the device - a refresh token bound to that device, and the
    device key to present with it. Writing the file *before* the
    handshake leaves the older pair on disk: a refresh token with no
    device to go with it, which is refused an hour later, when the
    token first needs renewing.
    """

    def test_the_settings_page_writes_the_session_after_the_handshake(self):
        from ember import discover

        path = Path(self.tmp.name) / "written-session.json"

        def remember_device(self):
            self.tokens = {"token": "the-device-token",
                           "accessToken": "the-device-access",
                           "refreshToken": "the-device-refresh"}
            self.device_key = "the-device"
            return "device registered"

        with mock.patch.object(Hive, "login",
                               return_value={"status": "signed_in"}):
            with mock.patch.object(Hive, "remember_device", remember_device):
                with mock.patch.object(Hive, "read_state", return_value={}):
                    discover.hive_login("someone@example.com", "the-password",
                                        str(path))
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["tokens"]["refreshToken"],
                         "the-device-refresh",
                         "the file holds the tokens from before the handshake")
        self.assertEqual(saved["device_key"], "the-device")

    def test_the_sign_in_again_writes_the_session_after_the_handshake(self):
        def remember_device(self):
            self.tokens = {"token": "the-device-token",
                           "accessToken": "the-device-access",
                           "refreshToken": "the-device-refresh"}
            self.device_key = "the-device"
            return "device registered"

        self.fake.kill_refresh()
        with mock.patch.object(Hive, "login", self._fake_login()):
            with mock.patch.object(Hive, "remember_device", remember_device):
                self.dashboard.poll()
        saved = self._file()
        self.assertEqual(saved["tokens"]["refreshToken"], "the-device-refresh",
                         "the file holds the tokens from before the handshake")
        self.assertEqual(saved["device_key"], "the-device")


class TheDeviceTests(unittest.TestCase):
    """The device is part of the session, and it must survive."""

    def test_a_sign_in_without_device_details_keeps_the_known_key(self):
        """Cognito does not always repeat what it already knows.

        Wiping the device key there would leave the refresh token
        without the device it belongs to - and a refresh token that
        is presented with the wrong device is refused with the same
        "Invalid Refresh Token" as one that has genuinely died.
        """
        client = Hive()
        client.device_key = "the-known-device"
        client.device_group_key = "the-known-group"
        client._after_password({"AuthenticationResult": {
            "IdToken": "id", "AccessToken": "access",
            "RefreshToken": "refresh", "ExpiresIn": 3600}}, "user")
        self.assertEqual(client.device_key, "the-known-device")
        self.assertEqual(client.device_group_key, "the-known-group")

    def test_a_sign_in_with_new_device_details_replaces_the_key(self):
        client = Hive()
        client.device_key = "the-old-device"
        client._after_password({"AuthenticationResult": {
            "IdToken": "id", "AccessToken": "access",
            "RefreshToken": "refresh", "ExpiresIn": 3600,
            "NewDeviceMetadata": {"DeviceKey": "the-new-device",
                                  "DeviceGroupKey": "the-new-group"}}},
            "user")
        self.assertEqual(client.device_key, "the-new-device")
        self.assertEqual(client.device_group_key, "the-new-group")

    def test_the_device_is_presented_in_the_challenge(self):
        """An account with two-step verification asks for a code
        whenever the sign-in does not present a device it recognises.

        Cognito asks for the device *after* the password step, in the
        DEVICE_SRP_AUTH challenge - so that is where the device key
        and the device password go.
        """
        client = Hive()
        client.pool_id = "eu-west-1_SamNfoWtf"
        client.device_key = "the-known-device"
        client.device_group_key = "the-known-group"
        client.device_password = "the-known-password"
        cognito = mock.Mock()
        cognito.initiate_auth.return_value = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                    "SALT": "s", "SECRET_BLOCK": "Yg=="}}
        cognito.respond.side_effect = [
            # The password step is answered with the device challenge.
            {"ChallengeName": "DEVICE_SRP_AUTH",
             "ChallengeParameters": {"USERNAME": "the-canonical-name",
                                     "DEVICE_GROUP_KEY": "the-known-group",
                                     "DEVICE_KEY": "the-known-device",
                                     "SRP_B": "0", "SALT": "s",
                                     "SECRET_BLOCK": "Yg=="}},
            # Which is answered with the device verifier.
            {"ChallengeName": "DEVICE_PASSWORD_VERIFIER",
             "ChallengeParameters": {"USERNAME": "the-canonical-name",
                                     "DEVICE_GROUP_KEY": "the-known-group",
                                     "DEVICE_KEY": "the-known-device",
                                     "SRP_B": "0", "SALT": "s",
                                     "SECRET_BLOCK": "Yg=="}},
            # Which is answered with the tokens.
            {"AuthenticationResult": {"IdToken": "id", "AccessToken": "access",
                                      "RefreshToken": "refresh",
                                      "ExpiresIn": 3600}},
        ]
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(hive_module, "srp_key", return_value=b"k"):
                result = client.login("someone@example.com", "the-password")
        self.assertEqual(result["status"], "signed_in")
        # The device challenge is answered with the device.
        device_answer = cognito.respond.call_args_list[1]
        self.assertEqual(device_answer.args[0], "DEVICE_SRP_AUTH")
        self.assertEqual(device_answer.args[1]["DEVICE_KEY"],
                         "the-known-device")
        # And with the username Cognito asked for, which is not always
        # the address that was typed.
        self.assertEqual(device_answer.args[1]["USERNAME"],
                         "the-canonical-name")

    def test_a_device_that_cannot_be_proved_is_dropped_and_retried(self):
        """A stale device in the session file must not lock the user out.

        The account treats a sign-in with no device as a new device and
        asks for a one-time code - which the settings page can take.
        Better that than a sign-in that cannot finish.
        """
        client = Hive()
        client.pool_id = "eu-west-1_SamNfoWtf"
        client.device_key = "a-stale-device"
        client.device_group_key = "a-stale-group"
        client.device_password = "a-stale-password"
        cognito = mock.Mock()
        cognito.initiate_auth.return_value = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                    "SALT": "s", "SECRET_BLOCK": "Yg=="}}
        cognito.respond.side_effect = [
            # The password step is answered with the device challenge.
            {"ChallengeName": "DEVICE_SRP_AUTH",
             "ChallengeParameters": {"USERNAME": "u", "SRP_B": "0",
                                     "SALT": "s", "SECRET_BLOCK": "Yg=="}},
            # Which fails: the account does not know this device.
            HiveError("NotAuthorizedException: User does not exist."),
            # Then the password step on the second attempt asks for a code.
            {"ChallengeName": "SMS_MFA", "Session": "a-session"},
        ]
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(hive_module, "srp_key", return_value=b"k"):
                result = client.login("someone@example.com", "the-password")
        self.assertEqual(result["status"], "needs_code")
        self.assertEqual(client.device_key, "",
                         "the stale device was not dropped")

    def test_a_wrong_password_is_not_retried_without_the_device(self):
        """The device fallback is for devices, not for passwords.

        A wrong password means a wrong password: trying again with no
        device would only add another way to lock the account.
        """
        client = Hive()
        client.pool_id = "eu-west-1_SamNfoWtf"
        client.device_key = "the-known-device"
        cognito = mock.Mock()
        cognito.initiate_auth.return_value = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                    "SALT": "s", "SECRET_BLOCK": "Yg=="}}
        cognito.respond.side_effect = HiveError(
            "NotAuthorizedException: Incorrect username or password.")
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(hive_module, "srp_key", return_value=b"k"):
                with self.assertRaises(HiveError):
                    client.login("someone@example.com", "the-password")
        self.assertEqual(cognito.initiate_auth.call_count, 1,
                         "it tried the sign-in again")

    def test_the_first_request_does_not_carry_the_device_key(self):
        """Cognito's device flow presents the device in the challenge.

        Putting the device key in the first request is not how the
        flow works: the answer is a challenge this code does not
        handle, and the sign-in ends up needing a one-time code.
        """
        client = Hive()
        client.pool_id = "eu-west-1_SamNfoWtf"
        client.device_key = "the-known-device"
        cognito = mock.Mock()
        cognito.initiate_auth.return_value = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                    "SALT": "s", "SECRET_BLOCK": "Yg=="}}
        cognito.respond.return_value = {"AuthenticationResult": {
            "IdToken": "id", "AccessToken": "access",
            "RefreshToken": "refresh", "ExpiresIn": 3600}}
        with mock.patch.object(Hive, "_cognito", return_value=cognito):
            with mock.patch.object(hive_module, "srp_key", return_value=b"k"):
                client.login("someone@example.com", "the-password")
        first = cognito.initiate_auth.call_args_list[0]
        self.assertIsNone(first.kwargs.get("device_key"))


class TheSettingsPageUsesTheKnownDeviceTests(unittest.TestCase):
    """The settings page signs in as the device it already has.

    An account that tracks devices asks for a one-time code whenever a
    sign-in does not present a device it recognises. The device is in
    the session file, so the sign-in should use it - otherwise every
    sign-in from the settings page needs a code from the user's phone.
    """

    def _session(self, directory: str) -> Path:
        path = Path(directory) / "hive-session.json"
        hive = Hive()
        hive.pool_id = "eu-west-1_SamNfoWtf"
        hive.client_id = "the-public-client"
        hive.region = "eu-west-1"
        hive.device_key = "the-known-device"
        hive.device_group_key = "the-known-group"
        hive.device_password = "the-known-password"
        hive.tokens = {"token": "t", "accessToken": "t", "refreshToken": "r"}
        hive.save_session(path)
        return path

    def test_the_sign_in_presents_the_device_on_file(self):
        from ember import discover
        with tempfile.TemporaryDirectory() as name:
            path = self._session(name)
            seen: dict = {}

            def login(client, username, password):
                seen["device_key"] = client.device_key
                seen["password_key"] = client.password_key
                client.tokens = {"token": "t", "accessToken": "t",
                                 "refreshToken": "r"}
                return {"status": "signed_in"}

            with mock.patch.object(Hive, "login", login):
                with mock.patch.object(Hive, "remember_device",
                                       lambda self: "device registered"):
                    with mock.patch.object(Hive, "read_state",
                                           lambda self: {"rooms": []}):
                        discover.hive_login("a@b.com", "the-password", str(path))
            self.assertEqual(seen.get("device_key"), "the-known-device")
            self.assertEqual(seen.get("password_key"), "SamNfoWtf")

    def test_the_code_step_presents_the_device_on_file(self):
        from ember import discover
        with tempfile.TemporaryDirectory() as name:
            path = self._session(name)
            seen: dict = {}

            def complete_code(client, session, username, code, email=""):
                seen["device_key"] = client.device_key
                return {"status": "signed_in"}

            with mock.patch.object(Hive, "complete_code", complete_code):
                with mock.patch.object(Hive, "remember_device",
                                       lambda self: "device registered"):
                    with mock.patch.object(Hive, "read_state",
                                           lambda self: {"rooms": []}):
                        discover.hive_code("a-session", "u", "123456",
                                           str(path), "a@b.com")
            self.assertEqual(seen.get("device_key"), "the-known-device")

    def test_without_a_session_file_there_is_no_device(self):
        """A first sign-in has no device - and that is expected."""
        from ember import discover
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "hive-session.json"
            seen: dict = {}

            def login(client, username, password):
                seen["device_key"] = client.device_key
                client.tokens = {"token": "t", "accessToken": "t",
                                 "refreshToken": "r"}
                return {"status": "signed_in"}

            with mock.patch.object(Hive, "login", login):
                with mock.patch.object(Hive, "remember_device",
                                       lambda self: "device registered"):
                    with mock.patch.object(Hive, "read_state",
                                           lambda self: {"rooms": []}):
                        discover.hive_login("a@b.com", "the-password", str(path))
            self.assertEqual(seen.get("device_key"), "")


class ThePasswordKeyTests(unittest.TestCase):
    """The password hash needs the pool's key.

    The key is the part of the pool id after the underscore. Without
    it the hash is wrong, and Cognito says "Incorrect username or
    password" - which points at the password rather than at the
    missing key.
    """

    def _session(self, directory: str) -> Path:
        path = Path(directory) / "hive-session.json"
        hive = Hive()
        hive.pool_id = "eu-west-1_SamNfoWtf"
        hive.client_id = "the-public-client"
        hive.region = "eu-west-1"
        hive.tokens = {"token": "t", "accessToken": "t",
                       "refreshToken": "r"}
        hive.save_session(path)
        return path

    def test_a_loaded_session_works_out_the_password_key(self):
        with tempfile.TemporaryDirectory() as name:
            path = self._session(name)
            client = Hive()
            client.load_session(path)
            self.assertEqual(client.password_key, "SamNfoWtf")

    def test_the_hash_uses_that_key(self):
        with tempfile.TemporaryDirectory() as name:
            path = self._session(name)
            client = Hive()
            client.load_session(path)
            cognito = mock.Mock()
            cognito.initiate_auth.return_value = {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                        "SALT": "s", "SECRET_BLOCK": "Yg=="}}
            cognito.respond.return_value = {"AuthenticationResult": {
                "IdToken": "id", "AccessToken": "access",
                "RefreshToken": "refresh", "ExpiresIn": 3600}}
            with mock.patch.object(Hive, "_cognito", return_value=cognito):
                with mock.patch.object(hive_module, "srp_key",
                                       return_value=b"k") as key:
                    client.login("someone@example.com", "the-password")
            self.assertEqual(key.call_args.args[0], "SamNfoWtf")

    def test_without_a_pool_id_it_asks_hive_for_the_details(self):
        """No pool id in the file: the sign-in page has one."""
        client = Hive()

        def read_sso(_client):
            client.pool_id = "eu-west-1_SamNfoWtf"
            client.password_key = client._pool_password_key()
            client.client_id = "the-public-client"
            client.region = "eu-west-1"

        cognito = mock.Mock()
        cognito.initiate_auth.return_value = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeParameters": {"USER_ID_FOR_SRP": "u", "SRP_B": "0",
                                    "SALT": "s", "SECRET_BLOCK": "Yg=="}}
        cognito.respond.return_value = {"AuthenticationResult": {
            "IdToken": "id", "AccessToken": "access",
            "RefreshToken": "refresh", "ExpiresIn": 3600}}
        with mock.patch.object(Hive, "read_sso", read_sso):
            with mock.patch.object(Hive, "_cognito", return_value=cognito):
                with mock.patch.object(hive_module, "srp_key",
                                       return_value=b"k") as key:
                    client.login("someone@example.com", "the-password")
        self.assertEqual(key.call_args.args[0], "SamNfoWtf")

    def test_without_a_pool_id_and_no_sign_in_page_it_says_what_is_missing(self):
        """A missing key must not look like a wrong password."""
        client = Hive()
        with mock.patch.object(Hive, "read_sso",
                               side_effect=HiveError("no network")):
            with self.assertRaises(HiveError) as caught:
                client.login("someone@example.com", "the-password")
        message = str(caught.exception)
        self.assertIn("sign-in details", message)
        self.assertNotIn("Incorrect username or password", message)


class TheRefreshTests(unittest.TestCase):
    """A refresh token belongs to a client and to a device."""

    def test_the_other_client_id_is_tried(self):
        """A token issued to Hive's other client is refused with the
        same "Invalid Refresh Token" as a dead one."""
        client = Hive()
        client.region = "eu-west-1"
        client.client_id = "the-public-client"
        client.app_client_id = "the-app-client"
        client.tokens = {"token": "t", "refreshToken": "r", "accessToken": "a"}

        seen = []

        class Fake:
            def __init__(self, region, client_id, endpoint=None):
                seen.append(client_id)

            def refresh_auth(self, token, device_key=None):
                raise HiveError("Invalid Refresh Token")

        with mock.patch.object(hive_module, "Cognito", Fake):
            with self.assertRaises(HiveError):
                client.refresh()
        self.assertIn("the-app-client", seen)
        self.assertIn("the-public-client", seen)

    def test_the_client_id_is_kept_in_the_session_file(self):
        client = Hive()
        client.app_client_id = "the-app-client"
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "session.json"
            client.save_session(path)
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        self.assertEqual(saved["app_client_id"], "the-app-client")


if __name__ == "__main__":
    unittest.main()

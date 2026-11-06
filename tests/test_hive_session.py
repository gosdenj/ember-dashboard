"""The Hive session has to outlive its own access token.

Hive's access token is good for an hour. The dashboard polls every two
minutes for weeks at a time, so the session has to renew itself - the
alternative is a dashboard that goes deaf once an hour and only comes
back when a person notices and signs in again.

These tests run against a small fake Hive:

  * Cognito's own refresh flow (``REFRESH_TOKEN_AUTH``), which is how
    a session is meant to be renewed - it takes the *refresh* token,
    which lasts weeks, and the device key;
  * Hive's own proxy endpoint, the older way round, kept as a backup;
  * the data endpoint, which says Unauthorized when the token is one it
    no longer accepts.
"""

import base64
import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ember import hive as hive_module
from ember.hive import Hive, HiveError, token_expiry


def make_token(expires_at: float, name: str = "ember") -> str:
    """A JWT-shaped token. The signature is not checked by anyone here."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "ES256", "typ": "JWT"}).encode()).rstrip(b"=")
    claims = base64.urlsafe_b64encode(json.dumps({
        "exp": expires_at,
        "sub": "user-1",
        "username": "someone@example.com",
        "name": name,
    }).encode()).rstrip(b"=")
    return f"{header.decode()}.{claims.decode()}.signature-not-checked"


NODES_PAYLOAD = {
    "products": [
        {
            "type": "trvcontrol",
            "id": "zone-lounge",
            "state": {"name": "Lounge", "target": 20.0, "mode": "SCHEDULE",
                      "frostProtection": 7, "boost": None},
            "props": {"temperature": 19.2, "trvs": ["trv-lounge"]},
        },
        {
            "type": "heating",
            "id": "boiler",
            "state": {"target": 7.0, "frostProtection": 12, "mode": "SCHEDULE",
                      "boost": None},
            "props": {"temperature": 17.55, "working": False},
        },
    ],
    "devices": [
        {"id": "trv-lounge", "type": "trv",
         "props": {"battery": 100, "signal": 100, "calibration": 1.0,
                   "childLock": False, "control": "manual"}},
    ],
    "homes": [],
    "actions": [],
}


class FakeHiveHandler(http.server.BaseHTTPRequestHandler):
    """Just enough of Hive to be believable.

    Two kinds of "no", and the difference matters:

      * an **expired access token** is refused by the data endpoint,
        but the session can still be renewed - that is the whole point
        of the refresh token;
      * a **dead refresh token** cannot be renewed by anything, and
        that is the only thing a person can fix.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def fake(self):
        return self.server.fake

    def _alive(self, token: str) -> bool:
        return bool(token) and token not in self.fake.dead

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/cognito-idp/"):
            self._cognito(body)
            return
        if self.path.endswith("/cognito/refresh-token"):
            token = self.headers.get("authorization", "")
            self.fake.refresh_attempts.append(token)
            if not self._alive(token):
                self._reply(401, {"message": "Unauthorized"})
                return
            new = self.fake.issue()
            self._reply(200, {
                "token": new,
                "accessToken": new,
                "refreshToken": self.fake.refresh_token,
                "platform": {"endpoint": self.server.base},
            })
            return
        self._reply(404, {"message": "not found"})

    def _cognito(self, body: bytes) -> None:
        """Cognito's own refresh flow, with the shape it really uses."""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"__type": "BadRequestException"})
            return
        if payload.get("AuthFlow") != "REFRESH_TOKEN_AUTH":
            self._reply(400, {"__type": "UnknownOperationException",
                              "message": payload.get("AuthFlow") or "no flow"})
            return
        parameters = payload.get("AuthParameters") or {}
        token = parameters.get("REFRESH_TOKEN", "")
        self.fake.cognito_attempts.append(dict(parameters))
        if token not in self.fake.refresh_tokens:
            self._reply(400, {
                "__type": "com.amazonaws.cognito.identity.idp.model#NotAuthorizedException",
                "message": "Invalid Refresh Token"})
            return
        if self.fake.device_key and parameters.get("DEVICE_KEY") != self.fake.device_key:
            self._reply(400, {
                "__type": "com.amazonaws.cognito.identity.idp.model#NotAuthorizedException",
                "message": "Invalid Refresh Token"})
            return
        new = self.fake.issue()
        result = {
            "IdToken": new,
            "AccessToken": new,
            "TokenType": "Bearer",
            "ExpiresIn": 3600,
        }
        if self.fake.rotates_refresh:
            result["RefreshToken"] = "rotated-" + str(len(self.fake.issued))
        self._reply(200, {"AuthenticationResult": result})

    def do_GET(self):
        token = self.headers.get("authorization", "")
        if not self._alive(token) or token in self.fake.revoked:
            self._reply(401, {"message": "Unauthorized"})
            return
        self._reply(200, NODES_PAYLOAD)


class FakeHive:
    """The fake server, plus the knobs a test needs."""

    def __init__(self, lifetime: float = 3600.0, device_key: str = "",
                 rotates_refresh: bool = False):
        self.dead: set[str] = set()
        self.revoked: set[str] = set()
        self.issued: list[str] = []
        self.refresh_attempts: list[str] = []
        self.cognito_attempts: list[dict] = []
        self.refresh_token = "refresh-token"
        self.refresh_tokens: set[str] = {"refresh-token"}
        self.device_key = device_key
        self.rotates_refresh = rotates_refresh
        self.lifetime = lifetime
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      FakeHiveHandler)
        self.server.fake = self
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/1.0"
        self.server.base = self.base
        self.server.issue = self.issue
        self.cognito_endpoint = f"http://127.0.0.1:{self.port}/cognito-idp/{{region}}/"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def issue(self) -> str:
        token = make_token(time.time() + self.lifetime,
                           f"token-{len(self.issued)}")
        self.issued.append(token)
        return token

    def expire(self, token: str) -> None:
        """Pretend this access token has expired.

        The session can still be renewed - that is what the refresh
        token is for.
        """
        self.dead.add(token)

    def revoke(self, token: str) -> None:
        """Pretend this token was thrown away, but can still be renewed."""
        self.revoked.add(token)

    def kill_refresh(self) -> None:
        """Pretend the refresh token has expired as well.

        Now nothing can renew the session, and only a person can. The
        proxy is killed with it - a session that the fallback can
        still rescue is not a dead session.
        """
        self.refresh_tokens.clear()
        self.dead.update(self.issued)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hive-session.json"
        self.fake = FakeHive()
        self.original_refresh_url = hive_module.REFRESH_URL
        self.original_cognito = hive_module.COGNITO_ENDPOINT
        hive_module.REFRESH_URL = self.fake.base + "/cognito/refresh-token"
        hive_module.COGNITO_ENDPOINT = self.fake.cognito_endpoint

    def tearDown(self):
        hive_module.REFRESH_URL = self.original_refresh_url
        hive_module.COGNITO_ENDPOINT = self.original_cognito
        self.fake.stop()
        self.tmp.cleanup()

    def write_session(self, token: str | None = None) -> str:
        token = token or self.fake.issue()
        hive = Hive(self.fake.base)
        hive.pool_id = "eu-west-1_SamNfoWtf"
        hive.client_id = "jivhemv3fvn8cr32qrggqpcf8"
        hive.region = "eu-west-1"
        hive.device_key = self.fake.device_key
        hive.tokens = {
            "token": token,
            "accessToken": token,
            "refreshToken": self.fake.refresh_token,
        }
        hive.save_session(self.path)
        return token

    def read_session(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def file_token(self) -> str:
        return self.read_session()["tokens"]["token"]


class RenewingTests(SessionFixture):
    """How the session renews itself."""

    def test_a_fresh_session_is_used_as_it_is(self):
        """No point asking anybody for a new token every two minutes."""
        self.write_session()
        hive = Hive(self.fake.base)
        self.assertTrue(hive.load_session(self.path))
        self.assertEqual(self.fake.cognito_attempts, [])
        self.assertEqual(self.fake.refresh_attempts, [])

    def test_a_stale_session_is_renewed(self):
        self.fake.lifetime = 30  # inside the skew, so it counts as stale
        original = self.write_session()
        hive = Hive(self.fake.base)
        self.assertTrue(hive.load_session(self.path))
        self.assertEqual(len(self.fake.cognito_attempts), 1)
        self.assertNotEqual(hive.tokens["token"], original,
                            "the session was not renewed")
        self.assertEqual(self.file_token(), hive.tokens["token"])

    def test_the_renewal_goes_through_cognito(self):
        """Cognito's own refresh flow, not Hive's proxy.

        The refresh token lasts weeks; the proxy endpoint is the older
        way round and is the flakiest part of the whole thing. Cognito
        is what the reference library uses, and it is what makes a
        session last.
        """
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertEqual(len(self.fake.cognito_attempts), 1)
        self.assertEqual(len(self.fake.refresh_attempts), 0,
                         "the proxy was used when Cognito would have done")

    def test_the_renewal_carries_the_refresh_token(self):
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        attempt = self.fake.cognito_attempts[0]
        self.assertEqual(attempt.get("REFRESH_TOKEN"), self.fake.refresh_token)

    def test_the_renewal_carries_the_device_key(self):
        """An account with device tracking needs the device presented."""
        self.fake.device_key = "device-key-abc"
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertEqual(self.fake.cognito_attempts[0].get("DEVICE_KEY"),
                         "device-key-abc")

    def test_the_renewal_works_without_a_device_key(self):
        """Not every account has one, and the renewal still has to work."""
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertNotIn("DEVICE_KEY", self.fake.cognito_attempts[0])

    def test_the_refresh_token_is_kept_when_cognito_does_not_rotate_it(self):
        """Cognito only hands back a new refresh token when it rotates.

        When it does not, the old one is still the good one. Throwing
        it away because the answer had no "RefreshToken" in it would
        end the session at the next renewal - a session that dies
        exactly one hour later, every time.
        """
        self.fake.lifetime = 30
        self.fake.rotates_refresh = False
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertEqual(hive.tokens["refreshToken"], self.fake.refresh_token)

    def test_a_rotated_refresh_token_is_used(self):
        self.fake.lifetime = 30
        self.fake.rotates_refresh = True
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertNotEqual(hive.tokens["refreshToken"], self.fake.refresh_token)

    def test_the_renewed_session_is_saved(self):
        """The renewed token has to be written back.

        Without this the dashboard renews, uses the new token for one
        poll, throws it away and does the same thing again next time -
        still holding the original token, which stops working an hour
        later.
        """
        self.fake.lifetime = 30
        original = self.write_session()
        hive = Hive(self.fake.base)
        self.assertTrue(hive.load_session(self.path))
        self.assertNotEqual(hive.tokens["token"], original)
        self.assertEqual(self.file_token(), hive.tokens["token"],
                         "the renewed token was not saved to the session file")

    def test_saving_a_renewed_session_keeps_the_sign_in_details(self):
        """The pool and the client id have to survive the save.

        They are what lets Ember remember the device, and without them
        the next sign-in would ask for the SMS code all over again.
        """
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        saved = self.read_session()
        self.assertEqual(saved["pool_id"], "eu-west-1_SamNfoWtf")
        self.assertEqual(saved["client_id"], "jivhemv3fvn8cr32qrggqpcf8")
        self.assertEqual(saved["region"], "eu-west-1")

    def test_saving_a_renewed_session_keeps_the_device_key(self):
        """The device key is what makes the next renewal work."""
        self.fake.device_key = "device-key-abc"
        self.fake.lifetime = 30
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.assertEqual(self.read_session()["device_key"], "device-key-abc")


class SurvivingTests(SessionFixture):
    """The session keeping itself alive."""

    def test_the_session_survives_its_token_dying(self):
        """The whole point: no person has to sign in again.

        The access token dies. The refresh token has not - that is what
        it is for - so Ember renews and carries on by itself.
        """
        self.fake.lifetime = 30
        original = self.write_session()

        first = Hive(self.fake.base)
        first.load_session(self.path)
        renewed = self.file_token()
        self.assertNotEqual(renewed, original)

        self.fake.expire(original)

        again = Hive(self.fake.base)
        self.assertTrue(again.load_session(self.path))
        self.assertNotEqual(again.tokens["token"], original,
                             "the dead token was used again")
        state = again.api("/nodes/all?products=true&devices=true&actions=true")
        self.assertIn("products", state)

    def test_a_revoked_token_is_renewed_and_the_call_retried(self):
        """A 401 does not have to be the end of the session."""
        self.write_session()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        self.fake.revoke(hive.tokens["token"])

        state = hive.api("/nodes/all?products=true&devices=true&actions=true")
        self.assertIn("products", state)
        self.assertEqual(len(self.fake.cognito_attempts), 1)

    def test_a_dead_session_says_what_to_do(self):
        """A bare "Unauthorized" is a puzzle. This one is a door.

        It has to say that the session is over, when it ended, and what
        to do about it.
        """
        self.fake.lifetime = 30
        self.write_session()
        self.fake.kill_refresh()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        with self.assertRaises(HiveError) as caught:
            hive.api("/nodes/all")
        message = str(caught.exception)
        self.assertIn("not authorised", message)
        self.assertIn("signed in again", message)

    def test_the_failure_names_everything_that_was_tried(self):
        """When a renewal fails, say what was tried and what it said."""
        self.fake.lifetime = 30
        self.write_session()
        self.fake.kill_refresh()
        hive = Hive(self.fake.base)
        hive.load_session(self.path)
        with self.assertRaises(HiveError) as caught:
            hive.api("/nodes/all")
        message = str(caught.exception)
        self.assertIn("Cognito", message)
        self.assertIn("refresh-token", message)


class TokenExpiryTests(unittest.TestCase):
    """Reading the clock out of the token."""

    def test_the_expiry_is_read_from_the_token(self):
        token = make_token(1700000000.0)
        self.assertAlmostEqual(token_expiry(token), 1700000000.0, places=1)

    def test_a_token_that_is_not_a_jwt_has_no_expiry(self):
        self.assertIsNone(token_expiry("not-a-token"))
        self.assertIsNone(token_expiry(""))

    def test_a_jwt_without_an_expiry_has_none(self):
        header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b"=")
        claims = base64.urlsafe_b64encode(b'{"sub":"x"}').rstrip(b"=")
        self.assertIsNone(token_expiry(f"{header.decode()}.{claims.decode()}.sig"))

    def test_a_rubbish_token_does_not_raise(self):
        self.assertIsNone(token_expiry("a.b.c"))
        self.assertIsNone(token_expiry("a.!!!not-base64!!!.c"))


if __name__ == "__main__":
    unittest.main()

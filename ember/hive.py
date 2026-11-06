"""The Hive client - heating state and radiator valves.

Hive's sign-in is a Cognito SRP-6a handshake with no AWS signature on it,
which is what keeps Ember standard-library-only. The contracts here were
verified against the live service:

  * The SSO page carries the Cognito pool id and the public client id.
    The region is the part of the pool id before the underscore, and the
    SRP password key is the part after it.
  * Cognito's own RefreshTokenAuth is not exposed - it answers
    UnknownOperationException - so Hive proxies it at beekeeper. The
    reply also carries the regional API endpoint to use from then on,
    so the base URL is never a guess.
  * The data API wants the raw IdToken in the ``authorization`` header,
    as a bearer-style value rather than a signature.

Signing in can require a one-time SMS code. A server cannot ask for that
on stdin, so the split is: ``probes/probe_hive.py`` does the one
interactive sign-in and writes a session file; Ember loads that file and
refreshes silently from then on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

SSO = "https://sso.hivehome.com/"
REFRESH_URL = "https://beekeeper.hivehome.com/1.0/cognito/refresh-token"
DEFAULT_BASE = "https://beekeeper-uk.hivehome.com/1.0"

#: Where Cognito lives. A template, so a test can point it somewhere
#: else - the renewal has to be testable without the real service.
COGNITO_ENDPOINT = "https://cognito-idp.{region}.amazonaws.com/"

#: The Cognito SRP-6a group, from amazon-cognito-identity-js.
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
G_HEX = "2"
INFO_BITS = bytearray("Caldera Derived Key", "utf-8")

#: Node types we recognise. Hive has renamed a few things over the years,
#: so several spellings are accepted.
BOILER_TYPES = ("boilermodule", "boiler", "heatingcontroller", "thermostat",
                "heating")
TRV_TYPES = ("trv", "radiator", "radiatorvalve", "trvcontroller", "valve")
SENSOR_TYPES = ("temperature", "thermostat", "sensor", "weathersensor")


class HiveError(Exception):
    """Anything that went wrong talking to Hive."""


class DeviceError(HiveError):
    """The device could not be proved.

    Kept apart from the other failures because it is recoverable: the
    sign-in can start again with no device at all, and the account
    will treat it as a new device (asking for a one-time code). A
    wrong password is not recoverable in that way, so the two must
    not look alike.
    """


#: How long before a token expires Ember refreshes it.
#:
#: The access token is good for an hour, so refreshing five minutes
#: early means the refresh always happens with a token that is still
#: alive. Refreshing after the expiry is too late: the refresh
#: endpoint will not accept a token that has already died.
TOKEN_SKEW = 300


def token_expiry(token: str) -> float | None:
    """When a token stops working, according to the token itself.

    The token is a JWT: three base64url parts, of which the middle one
    carries the claims. The signature is not checked - Hive is the one
    that checks it, and it is the one that decides. This is only here
    to read the clock out of the token, so Ember can renew *before* it
    dies rather than discovering it by being refused.

    Returns None when the token is not a JWT or has no expiry in it,
    which means "we do not know" rather than "never".
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    try:
        return float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# SRP-6a, the maths Cognito expects
# --------------------------------------------------------------------------

def sha256_hex(buffer: bytes) -> str:
    digest = hashlib.sha256(buffer).hexdigest()
    return (64 - len(digest)) * "0" + digest


def hex_hash(hex_string: str) -> str:
    return sha256_hex(bytearray.fromhex(hex_string))


def long_to_hex(value: int) -> str:
    return f"{value:x}"


def pad_hex(value: int | str) -> str:
    text = long_to_hex(value) if not isinstance(value, str) else value
    if len(text) % 2 == 1:
        text = f"0{text}"
    elif text[0] in "89ABCDEFabcdef":
        text = f"00{text}"
    return text


def calculate_u(big_a: int, big_b: int) -> int:
    return int(hex_hash(pad_hex(big_a) + pad_hex(big_b)), 16)


def compute_hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, INFO_BITS + bytearray(chr(1), "utf-8"), hashlib.sha256).digest()[:16]


def srp_key(password_key: str, username: str, password: str, server_b: int,
            salt: str, small_a: int, big_a: int, big_n: int, k: int) -> bytes:
    """The session key, from the password verifier challenge.

    The string that gets hashed is ``<password key><username>:<password>``.

    Two things catch people out here:

      * The **username** is the one Cognito sends back as
        ``USER_ID_FOR_SRP``, not the one that was typed. It is the
        canonical form the pool knows the user by.
      * The **password key** is Hive's own addition - the part of the
        pool id after the underscore (``eu-west-1_SamNfoWtf`` gives
        ``SamNfoWtf``). It is not the client id and not the pool id.

    Getting the username wrong here fails with exactly the same message
    as a wrong password, which is a thoroughly unhelpful way to find out.
    """
    u = calculate_u(big_a, server_b)
    if u == 0:
        raise ValueError("U cannot be zero")
    username_password = f"{password_key}{username}:{password}"
    username_password_hash = sha256_hex(username_password.encode("utf-8"))
    x = int(hex_hash(pad_hex(salt) + username_password_hash), 16)
    g_mod_pow_xn = pow(int(G_HEX, 16), x, big_n)
    s = pow(server_b - k * g_mod_pow_xn, small_a + u * x, big_n)
    return compute_hkdf(bytearray.fromhex(pad_hex(s)),
                        bytearray.fromhex(pad_hex(long_to_hex(u))))


def timestamp() -> str:
    """Cognito wants 'Tue Nov 03 21:00:00 UTC 2026' - with no zero padding."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a %b %d %H:%M:%S UTC %Y")
    return re.sub(r" 0(\d) ", r" \1 ", now)


# --------------------------------------------------------------------------
# Cognito
# --------------------------------------------------------------------------

class Cognito:
    """The AWS JSON 1.0 client for Cognito - no SigV4, so no boto3."""

    def __init__(self, region: str, client_id: str, endpoint: str | None = None):
        self.url = endpoint or COGNITO_ENDPOINT.format(region=region)
        self.client_id = client_id

    def call(self, target: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/x-amz-json-1.0",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
                "User-Agent": "ember/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                raise HiveError(f"{parsed.get('__type', exc.code)}: "
                                f"{parsed.get('message', detail)[:300]}") from exc
            except json.JSONDecodeError:
                raise HiveError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise HiveError(f"could not reach Cognito: {exc.reason}") from exc

    def initiate_auth(self, username: str, srp_a: str,
                      device_key: str | None = None) -> dict:
        parameters = {"USERNAME": username, "SRP_A": srp_a}
        if device_key:
            parameters["DEVICE_KEY"] = device_key
        return self.call("InitiateAuth", {
            "AuthFlow": "USER_SRP_AUTH",
            "AuthParameters": parameters,
            "ClientId": self.client_id,
        })

    def refresh_auth(self, refresh_token: str,
                     device_key: str | None = None) -> dict:
        """Renew the session with Cognito's own refresh flow.

        This is the flow that makes a session last: the refresh token
        is a long-lived credential - weeks, not an hour - and handing
        it back with the device key gets a new set of tokens without
        anybody typing a password.

        The device key matters for an account with device tracking: a
        refresh token issued to a device has to be presented with that
        device, or Cognito will not renew it.
        """
        parameters: dict[str, str] = {"REFRESH_TOKEN": refresh_token}
        if device_key:
            parameters["DEVICE_KEY"] = device_key
        return self.call("InitiateAuth", {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "AuthParameters": parameters,
            "ClientId": self.client_id,
        })

    def respond(self, challenge_name: str, responses: dict,
                session: str | None = None) -> dict:
        payload = {
            "ClientId": self.client_id,
            "ChallengeName": challenge_name,
            "ChallengeResponses": responses,
        }
        if session:
            payload["Session"] = session
        return self.call("RespondToAuthChallenge", payload)

    def confirm_device(self, access_token: str, device_key: str,
                       device_group_key: str, device_password: str) -> dict:
        """Tell Cognito to remember this device, so the 2FA does not repeat."""
        salt = pad_hex(secrets.randbits(128))
        combined = f"{device_group_key}{device_key}:{device_password}"
        combined_hash = sha256_hex(combined.encode("utf-8"))
        x = int(hex_hash(salt + combined_hash), 16)
        verifier = pad_hex(pow(int(G_HEX, 16), x, int(N_HEX, 16)))
        return self.call("ConfirmDevice", {
            "AccessToken": access_token,
            "DeviceKey": device_key,
            "DeviceName": "ember",
            "PasswordVerifier": base64.b64encode(
                bytearray.fromhex(verifier)).decode(),
            "Salt": base64.b64encode(bytearray.fromhex(salt)).decode(),
        })


#: What each Cognito challenge means, in words a person can act on.
CHALLENGE_MESSAGES = {
    "SMS_MFA": ("Hive has sent a two-step code to your phone. "
                "Type it below to finish signing in."),
    "SMS_MFA_SETUP": (
        "This account has two-step verification switched on but has no phone "
        "number set up. Add one in the Hive app, or turn SMS two-step "
        "verification off, then try again."),
    "NEW_PASSWORD_REQUIRED": (
        "Hive wants a new password for this account. Change it in the Hive "
        "app first, then sign in again."),
    "PASSWORD_VERIFIER": (
        "Hive accepted the password but did not finish the sign-in. "
        "This is usually a sign-in problem on Hive's side - try again in "
        "a moment."),
    "DEVICE_SRP_AUTH": (
        "Hive wants to remember this device, but the sign-in did not get "
        "that far. Try again."),
}


def explain_challenge(name: str | None) -> str:
    """Turn a challenge name into something a person can act on."""
    if name in CHALLENGE_MESSAGES:
        return CHALLENGE_MESSAGES[name]
    if name:
        return (f"unexpected challenge from Cognito: {name}. "
                f"This is not something Ember knows how to answer - "
                f"the Hive app still works, so this is worth reporting.")
    return "Hive did not say why the sign-in failed."


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

class Hive:
    """Talks to Hive. Read-only, deliberately: Ember never writes back."""

    def __init__(self, base: str = DEFAULT_BASE):
        self.pool_id = ""
        self.client_id = ""
        # Hive publishes two Cognito client ids. A refresh token is bound
        # to the client that issued it, so both are worth trying.
        self.app_client_id = ""
        self.region = ""
        self.password_key = ""
        self.tokens: dict[str, Any] = {}
        self.base = base.rstrip("/")
        self._last_error: str = ""
        self.username = ""
        self.device_key = ""
        self.device_group_key = ""
        self.device_password = ""
        # Where this session came from, so a refreshed one can be written
        # back to the same place.
        self._session_path: Path | None = None
        # When the token we hold stops working.
        self._expiry: float | None = None

    # -- setup ------------------------------------------------------------

    def _pool_password_key(self) -> str:
        """The pool's password key.

        It is the part of the pool id after the underscore
        ("eu-west-1_SamNfoWtf" -> "SamNfoWtf"), and the SRP hash is
        built from it. Without it, every password is wrong - and the
        error says "Incorrect username or password", which points at
        the password rather than at the missing key.

        The pool id is in the session file, so this can always be
        worked out again: a session that can be renewed can also be
        re-signed-in with.
        """
        if self.pool_id and "_" in self.pool_id:
            return self.pool_id.split("_", 1)[1]
        return ""

    def read_sso(self) -> None:
        request = urllib.request.Request(
            SSO, headers={"User-Agent": "ember/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise HiveError(f"HTTP {exc.code} from the Hive sign-in page: "
                            f"{exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except urllib.error.URLError as exc:
            raise HiveError(f"could not reach the Hive sign-in page: "
                            f"{exc.reason}") from exc
        except OSError as exc:
            raise HiveError(f"could not read the Hive sign-in page: {exc}") from exc
        values = {m[0]: m[2] for m in re.findall(
            r"""window\.(\w+)\s*=\s*(["'])(.*?)\2""", html)}
        self.pool_id = values.get("HiveSSOPoolId", "")
        self.client_id = values.get("HiveSSOPublicCognitoClientId", "")
        self.app_client_id = values.get("HiveSSOCognitoClientId", "")
        if not self.pool_id or not self.client_id:
            raise HiveError("could not read the Hive SSO configuration - "
                            "the page layout may have changed")
        self.region = self.pool_id.split("_")[0]
        self.password_key = self.pool_id.split("_")[1]

    # -- tokens -----------------------------------------------------------

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        merged = {"content-type": "application/json", "Accept": "*/*",
                  "User-Agent": "ember/0.1"}
        merged.update(headers)
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=merged)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                raise HiveError(f"{parsed.get('__type', exc.code)}: "
                                f"{parsed.get('message', detail)[:300]}") from exc
            except json.JSONDecodeError:
                raise HiveError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise HiveError(f"could not reach Hive: {exc.reason}") from exc

    def refresh(self) -> bool:
        """Renew the session.

        Cognito's own refresh flow is the way to do it, and it is the
        way the reference library does it: hand back the *refresh*
        token - a long-lived credential - with the device key, and get
        a new set of tokens. That is what makes a session last weeks
        instead of an hour, and it is why nobody has to sign in again.

        Hive's own proxy endpoint is kept as a second try, for the
        accounts where Cognito's flow does not answer.

        Every attempt is recorded, because "not signed in" with no
        reason is the most useless error there is.
        """
        problems: list[str] = []
        refresh_token = self.tokens.get("refreshToken", "")

        # 1. Cognito's refresh flow.
        #
        # Four ways of asking, because a refresh token is bound to the
        # client that issued it and, for an account with device
        # tracking, to the device it was issued to. Getting either
        # wrong produces one unhelpful answer - "Invalid Refresh
        # Token" - whatever the real reason is, so all four are worth
        # trying before giving up on the token.
        attempts = 0
        if refresh_token:
            for label, make_client in self._cognito_clients():
                for device_key in (self.device_key or None, None):
                    attempts += 1
                    how = ("with the device key" if device_key
                           else "without a device key")
                    try:
                        result = make_client().refresh_auth(refresh_token,
                                                            device_key)
                    except HiveError as exc:
                        problems.append(f"Cognito's refresh ({label}, {how}) "
                                        f"said {exc}")
                        continue
                    auth = result.get("AuthenticationResult") or {}
                    if not auth.get("IdToken"):
                        problems.append(f"Cognito's refresh ({label}, {how}) "
                                        "gave nothing back")
                        continue
                    self._apply_cognito_tokens(auth)
                    return True

        # 2. Hive's own proxy, which is the older way round.
        payload = {
            "token": self.tokens.get("token", ""),
            "refreshToken": refresh_token,
            "accessToken": self.tokens.get("accessToken", ""),
        }
        headers = {"authorization": self.tokens.get("token", "")}
        data = None
        for url in self._refresh_urls():
            try:
                data = self._post(url, payload, headers)
                break
            except HiveError as exc:
                problems.append(f"{url} said {exc}")
        if data is None:
            # The same reason usually comes back several times, so the
            # message says what was tried rather than repeating itself.
            unique: list[str] = []
            for problem in problems:
                if problem not in unique:
                    unique.append(problem)
            tried = (f" ({attempts} ways were tried: "
                     f"{len(self._cognito_clients())} client ids, with and "
                     f"without the device key)") if attempts else ""
            raise HiveError("the session could not be refreshed: "
                            + "; ".join(unique) + tried)
        if not data.get("token"):
            raise HiveError(f"refresh gave nothing back: {str(data)[:200]}")
        self._apply_cognito_tokens({
            "IdToken": data.get("token", ""),
            "AccessToken": data.get("accessToken", ""),
            "RefreshToken": data.get("refreshToken", ""),
            "ExpiresIn": data.get("expiresIn"),
        })
        platform = data.get("platform") or {}
        if platform.get("endpoint"):
            self.base = platform["endpoint"].rstrip("/")
        return True

    def _apply_cognito_tokens(self, auth: dict) -> None:
        """Take a fresh set of tokens from Cognito's answer.

        The refresh token is only kept if the answer actually gives a
        new one. Cognito hands back a new refresh token only when it
        rotates them - and when it does not, the old one is still the
        good one. Throwing it away because the answer had no
        "RefreshToken" in it would end the session at the next renewal.
        """
        new_refresh = auth.get("RefreshToken")
        self.tokens.update({
            "token": auth.get("IdToken") or self.tokens.get("token", ""),
            "accessToken": (auth.get("AccessToken")
                            or self.tokens.get("accessToken", "")),
            "refreshToken": new_refresh or self.tokens.get("refreshToken", ""),
            "expiresIn": auth.get("ExpiresIn", self.tokens.get("expiresIn")),
        })
        self._expiry = token_expiry(self.tokens.get("token", ""))

    def token_expires_at(self) -> float | None:
        """When the token we are holding stops working."""
        return self._expiry

    def token_is_fresh(self, skew: int = TOKEN_SKEW) -> bool:
        """Is the token good enough to use as it is?

        True when the token is known to last longer than the skew. When
        the expiry is unknown the answer is False - an unknown lifetime
        is not a long one, and refreshing is cheap.
        """
        if self._expiry is None:
            return False
        return (self._expiry - time.time()) > skew

    def load_session(self, path: Path | str) -> bool:
        """Load a session, renewing it if it is going stale.

        Three things matter here, and getting any of them wrong is how
        a dashboard ends up needing a person once an hour.

        A session with a live token is used as it is. Refreshing on
        every poll was a network round trip every two minutes for a
        token that is good for an hour.

        A refreshed session is SAVED. This is the one that matters.
        The refreshed token used to be used for a single poll and then
        thrown away, with the session file still holding the original
        token - so an hour later the dashboard was still holding a dead
        token, the refresh could no longer be made with it, and the
        session stayed broken until somebody signed in again.

        A refresh that fails does not throw the session away. Hive's own
        refresh endpoint is the flakiest part of the whole thing, so a
        session with a live token is used as it is and the reason the
        refresh failed is kept, to be said out loud.
        """
        path = Path(path)
        self._last_error = ""
        self._session_path = None
        if not path.is_file():
            self._last_error = "there is no session file here"
            return False
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._last_error = f"the session file could not be read ({exc})"
            return False
        self.tokens = saved.get("tokens") or {}
        if saved.get("base"):
            self.base = saved["base"]
        # These have to come back too, or saving a refreshed session
        # would write the file without them.
        self.pool_id = saved.get("pool_id") or self.pool_id
        self.client_id = saved.get("client_id") or self.client_id
        # The password key is worked out from the pool id rather than
        # read from the file, because the file never had it: it is
        # derived, and deriving it again is what lets a session that
        # cannot be renewed sign itself back in.
        self.password_key = self.password_key or self._pool_password_key()
        self.app_client_id = saved.get("app_client_id") or self.app_client_id
        self.region = saved.get("region") or self.region
        self.device_key = saved.get("device_key") or ""
        self.device_group_key = saved.get("device_group_key") or ""
        self.device_password = saved.get("device_password") or ""
        self.username = saved.get("username") or ""
        self._expiry = token_expiry(self.tokens.get("token", ""))
        if not self.tokens.get("token") and not self.tokens.get("refreshToken"):
            self._last_error = "the session file has no token in it"
            return False
        if self.token_is_fresh():
            # Still good for a while. Use it and do not bother Hive.
            self._session_path = path
            return True
        try:
            self.refresh()
            self._session_path = path
            self._save()
            return True
        except HiveError as exc:
            self._last_error = str(exc)
            if self.tokens.get("token"):
                # Still worth having: the token works until it expires.
                self._session_path = path
                return True
            return False

    def _save(self) -> bool:
        """Write the session back where it came from.

        The refreshed token is only useful to the next poll if it is on
        disk. A file that cannot be written is worth knowing about, but
        it is not worth crashing over - the in-memory session still
        works for this poll.
        """
        if self._session_path is None:
            return False
        try:
            self.save_session(self._session_path)
            return True
        except OSError as exc:
            self._last_error = f"the refreshed session could not be saved ({exc})"
            return False

    @property
    def last_error(self) -> str:
        """Why the last attempt to use a session did not work."""
        return self._last_error

    def save_session(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tokens": self.tokens,
            "pool_id": self.pool_id,
            "client_id": self.client_id,
            "app_client_id": self.app_client_id,
            "region": self.region,
            "base": self.base,
            # The device registration is part of the session: without it
            # Hive asks for the SMS code again on every sign-in.
            "device_key": self.device_key,
            "device_group_key": self.device_group_key,
            "device_password": self.device_password,
            "username": self.username,
            "saved_at": timestamp(),
        }
        temporary = path.with_suffix(".json.tmp")
        # Created 0600 from the start: this file holds the tokens and the
        # device password in plain text.
        handle = os.fdopen(os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w", encoding="utf-8")
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # -- signing in -------------------------------------------------------

    def _cognito(self) -> Cognito:
        if not self.client_id:
            self.read_sso()
        return Cognito(self.region, self.client_id)

    def _cognito_clients(self) -> list[tuple[str, Callable[[], Cognito]]]:
        """Every Cognito client a refresh token might belong to.

        The first is the client the session was signed in with. The
        second is Hive's other published client id, kept because a
        session file written by an older version - or by hand - may
        hold a token issued to it, and presenting that token to the
        wrong client is refused with the same "Invalid Refresh Token"
        as a token that has genuinely died.
        """
        clients: list[tuple[str, Callable[[], Cognito]]] = [
            (self.client_id or "the signed-in client", self._cognito)]
        if self.app_client_id and self.app_client_id != self.client_id:
            clients.append((self.app_client_id,
                            lambda: Cognito(self.region, self.app_client_id)))
        return clients

    def login(self, username: str, password: str) -> dict:
        """Start a sign-in. Returns either tokens or a request for a code.

        The two-step shape is not optional: Hive sends a one-time SMS code
        to a phone, and the phone is the only place that code can be read.
        A terminal can prompt for it; a web page can ask for it in a
        field. Neither can guess it.

        The returned dict says which happened:

          ``{"status": "signed_in", "tokens": {...}}``
          ``{"status": "needs_code", "session": "...", "username": "..."}``
        """
        username = (username or "").strip()
        if not username or not password:
            raise HiveError("an email address and a password are both needed")
        self.username = username
        if not self.password_key:
            self.password_key = self._pool_password_key()
        if not self.password_key:
            # No pool id to work the key out from: ask Hive for its
            # sign-in details. The page carries the pool id, the client
            # id and the password key.
            try:
                self.read_sso()
            except HiveError as exc:
                raise HiveError("Hive's sign-in details could not be read "
                                f"({exc}) - sign in from the settings page") from exc
        if not self.password_key:
            # Still nothing: the SRP hash cannot be built. Say so,
            # rather than sending a hash that is wrong and being told
            # the password is.
            raise HiveError("Hive's sign-in details are missing (the pool "
                            "id is needed to hash the password) - sign in "
                            "from the settings page")
        cognito = self._cognito()
        try:
            return self._sign_in_once(cognito, username, password)
        except DeviceError as exc:
            # The device in the session file could not be proved: the
            # account does not know it, or the device password on disk
            # is not the one it knows. Start again with no device at
            # all - the account then treats this as a new device and
            # asks for a one-time code, which the settings page can
            # take. One extra attempt, not a loop.
            self.device_key = ""
            self.device_group_key = ""
            self.device_password = ""
            try:
                return self._sign_in_once(cognito, username, password)
            except HiveError as second:
                raise HiveError(f"{exc}; and signing in without the device "
                                f"failed too ({second})") from second

    def _sign_in_once(self, cognito, username: str, password: str) -> dict:
        """One attempt at the password step.

        The sign-in starts the ordinary way: the password, hashed the
        way this pool expects.

        The device is presented in the *challenge* - Cognito answers
        the password step with DEVICE_SRP_AUTH on an account that
        tracks devices, and that is where the device key and the
        device password go. Putting the device key in the first
        request instead is not how the flow works: Cognito answers
        with a challenge this code does not handle, and the sign-in
        ends up needing a one-time code it did not have to need.
        """
        big_n = int(N_HEX, 16)
        k = int(hex_hash("00" + N_HEX + "0" + G_HEX), 16)
        small_a = secrets.randbits(128) % big_n
        big_a = pow(int(G_HEX, 16), small_a, big_n)
        # Kept for the device handshake: if Hive answers the password
        # with DEVICE_SRP_AUTH, the same SRP_A has to go back out.
        self._srp_small_a = small_a
        self._srp_big_a = big_a

        response = cognito.initiate_auth(username, long_to_hex(big_a))
        if response.get("ChallengeName") != "PASSWORD_VERIFIER":
            raise HiveError(explain_challenge(response.get("ChallengeName")))

        parameters = response["ChallengeParameters"]
        key = srp_key(self.password_key, parameters["USER_ID_FOR_SRP"], password,
                      int(parameters["SRP_B"], 16), parameters["SALT"],
                      small_a, big_a, big_n, k)
        secret_block = base64.b64decode(parameters["SECRET_BLOCK"])
        message = (bytearray(self.password_key, "utf-8")
                   + bytearray(parameters["USER_ID_FOR_SRP"], "utf-8")
                   + bytearray(secret_block)
                   + bytearray(timestamp(), "utf-8"))
        signature = base64.b64encode(
            hmac.new(key, message, hashlib.sha256).digest()).decode()

        response = cognito.respond("PASSWORD_VERIFIER", {
            "TIMESTAMP": timestamp(),
            "USERNAME": parameters["USER_ID_FOR_SRP"],
            "PASSWORD_CLAIM_SECRET_BLOCK": parameters["SECRET_BLOCK"],
            "PASSWORD_CLAIM_SIGNATURE": signature,
        })
        return self._after_password(response, parameters["USER_ID_FOR_SRP"])

    def complete_code(self, session: str, username: str, code: str,
                      email: str = "") -> dict:
        """Finish a sign-in with the two-step code Hive sent by SMS.

        ``username`` is Cognito's canonical ``USER_ID_FOR_SRP``, and
        ``email`` is what was typed. Both are kept: the first answers
        the challenge, the second is the identifier the device
        registration needs. Losing the second is why a sign-in that went
        through the code could never remember the device - and an
        unremembered device means an SMS code every single time.
        """
        code = (code or "").strip()
        if not code:
            raise HiveError("the code was empty")
        self.username = email or username
        response = self._cognito().respond("SMS_MFA", {
            "SMS_MFA_CODE": code,
            "USERNAME": username,
        }, session=session)
        return self._after_password(response, username)

    def _after_password(self, response: dict, user_id: str) -> dict:
        """Turn Cognito's answer into either tokens or a request for a code."""
        challenge = response.get("ChallengeName")
        if challenge == "SMS_MFA":
            # The session and the user id stay in the process: the
            # caller holds them until the code comes back.
            return {
                "status": "needs_code",
                "session": response.get("Session", ""),
                "username": user_id,
                "message": CHALLENGE_MESSAGES["SMS_MFA"],
            }
        if "AuthenticationResult" not in response:
            if challenge == "DEVICE_SRP_AUTH":
                # The account wants this device remembered before it
                # will let the sign-in finish. This is not a failure -
                # it is the last step.
                self._device_handshake(response)
                return {"status": "signed_in", "device": "device registered"}
            raise HiveError(explain_challenge(challenge))
        result = response["AuthenticationResult"]
        self.tokens = {
            "token": result.get("IdToken", ""),
            "accessToken": result.get("AccessToken", ""),
            "refreshToken": result.get("RefreshToken", ""),
            "expiresIn": result.get("ExpiresIn"),
        }
        metadata = result.get("NewDeviceMetadata") or {}
        # A sign-in that comes back without device details does not mean
        # "there is no device" - it means the account did not repeat
        # what it already knows. Wiping a known device key here would
        # leave the refresh token without the device it belongs to.
        self.device_key = metadata.get("DeviceKey") or self.device_key
        self.device_group_key = metadata.get("DeviceGroupKey") or self.device_group_key
        if self.device_key and not self.device_password:
            self.device_password = base64.b64encode(os.urandom(40)).decode()
        # The tokens are on the object, and deliberately not in the
        # returned dict: the refresh token is a long-lived credential
        # for the Hive account, and a dict like this ends up in an HTTP
        # response.
        return {"status": "signed_in"}

    def _device_handshake(self, challenge: dict) -> None:
        """Answer the DEVICE_SRP_AUTH challenge and register the device.

        This is the path an account with device tracking takes: the
        password is accepted, and then the device has to prove itself
        before the tokens are handed over.
        """
        if not self.device_key or not self.device_group_key:
            raise DeviceError(
                "Hive asked to remember this device, but no device "
                "details came with the sign-in")
        if not self.device_password:
            self.device_password = base64.b64encode(os.urandom(40)).decode()
        big_a = getattr(self, "_srp_big_a", None)
        if big_a is None:
            raise HiveError("the device challenge arrived with no handshake "
                            "to continue")
        # Cognito's device challenge carries the username it wants -
        # the canonical one, which is not always the address that was
        # typed. Prefer it, and fall back to what we know.
        challenge_username = (challenge.get("ChallengeParameters") or {}).get(
            "USERNAME") or self.username
        try:
            response = self._cognito().respond("DEVICE_SRP_AUTH", {
                "USERNAME": challenge_username,
                "SRP_A": long_to_hex(big_a),
                "DEVICE_KEY": self.device_key,
            })
        except HiveError as exc:
            # Cognito will not accept this device. That is a device
            # problem, not a password problem, and the caller can act
            # on it: sign in again with no device at all.
            raise DeviceError(f"the device could not be proved ({exc})") from exc
        if response.get("ChallengeName") != "DEVICE_PASSWORD_VERIFIER":
            raise DeviceError(
                "unexpected challenge while remembering the device: "
                + str(response.get("ChallengeName")))
        response = self._sign_device_verifier(
            response.get("ChallengeParameters") or {})
        if "AuthenticationResult" not in response:
            raise DeviceError(
                "Hive did not finish registering the device: "
                + str(response.get("ChallengeName") or response.get("__type")
                      or "no tokens")[:200])
        result = response["AuthenticationResult"]
        self.tokens.update({
            "token": result.get("IdToken", self.tokens.get("token")),
            "accessToken": result.get("AccessToken",
                                      self.tokens.get("accessToken")),
            "refreshToken": result.get("RefreshToken",
                                       self.tokens.get("refreshToken")),
        })
        metadata = result.get("NewDeviceMetadata") or {}
        if metadata.get("DeviceKey"):
            self.device_key = metadata["DeviceKey"]
        if metadata.get("DeviceGroupKey"):
            self.device_group_key = metadata["DeviceGroupKey"]

    def _sign_device_verifier(self, parameters: dict) -> dict:
        """Sign the device password verifier and send it back.

        The device path hashes the same shape as the password one:
        ``<device group key><device key>:<device password>``.
        """
        small_a = getattr(self, "_srp_small_a", None)
        big_a = getattr(self, "_srp_big_a", None)
        if small_a is None or big_a is None:
            raise HiveError("the device challenge arrived with no handshake "
                            "to continue")
        big_n = int(N_HEX, 16)
        k = int(hex_hash("00" + N_HEX + "0" + G_HEX), 16)
        key = srp_key(self.device_group_key, self.device_key,
                      self.device_password,
                      int(parameters["SRP_B"], 16), parameters["SALT"],
                      small_a, big_a, big_n, k)
        secret_block = base64.b64decode(parameters["SECRET_BLOCK"])
        message = (bytearray(self.device_group_key, "utf-8")
                   + bytearray(self.device_key, "utf-8")
                   + bytearray(secret_block)
                   + bytearray(timestamp(), "utf-8"))
        signature = base64.b64encode(
            hmac.new(key, message, hashlib.sha256).digest()).decode()
        return self._cognito().respond("DEVICE_PASSWORD_VERIFIER", {
            "TIMESTAMP": timestamp(),
            "USERNAME": parameters.get("USERNAME", self.username),
            "PASSWORD_CLAIM_SECRET_BLOCK": parameters["SECRET_BLOCK"],
            "PASSWORD_CLAIM_SIGNATURE": signature,
            "DEVICE_KEY": self.device_key,
        })

    def remember_device(self) -> str:
        """Register the device, so the two-step code stops coming round.

        Two separate things happen here, and only the first one
        matters for the code:

        * **ConfirmDevice** tells Cognito to trust this device. It is
          the documented way to stop a new device being asked for a
          code every time. It needs the access token the sign-in
          produced, which is why it has to happen after the sign-in.
        * The **device SRP handshake** is how the *next* sign-in proves
          it is the same device.

        Accounts that do not track devices answer the handshake with a
        password challenge instead. That is not a failure - it means
        there is nothing to remember.
        """
        if not self.device_key or not self.device_group_key:
            return "no device metadata - skipped"
        if not self.device_password:
            self.device_password = base64.b64encode(os.urandom(40)).decode()

        registered = ""
        try:
            self._cognito().confirm_device(
                self.tokens.get("accessToken", ""), self.device_key,
                self.device_group_key, self.device_password)
            registered = "device registered"
        except HiveError as exc:
            registered = f"the device could not be registered ({exc})"

        big_n = int(N_HEX, 16)
        small_a = secrets.randbits(128) % big_n
        big_a = pow(int(G_HEX, 16), small_a, big_n)
        self._srp_small_a = small_a
        self._srp_big_a = big_a
        try:
            response = self._cognito().initiate_auth(
                self.username, long_to_hex(big_a), device_key=self.device_key)
            challenge = response.get("ChallengeName")
            if challenge == "DEVICE_SRP_AUTH":
                response = self._cognito().respond("DEVICE_SRP_AUTH", {
                    "USERNAME": self.username,
                    "SRP_A": long_to_hex(big_a),
                    "DEVICE_KEY": self.device_key,
                })
                challenge = response.get("ChallengeName")
            if challenge == "DEVICE_PASSWORD_VERIFIER":
                response = self._sign_device_verifier(
                    response.get("ChallengeParameters") or {})
                if "AuthenticationResult" not in response:
                    return (registered + "; the device handshake did not "
                            "finish").strip("; ")
                result = response["AuthenticationResult"]
                self.tokens.update({
                    "token": result.get("IdToken", self.tokens.get("token")),
                    "accessToken": result.get(
                        "AccessToken", self.tokens.get("accessToken")),
                    "refreshToken": result.get(
                        "RefreshToken", self.tokens.get("refreshToken")),
                })
                return (registered + "; future sign-ins should not need a "
                        "code").strip("; ")
            if challenge == "PASSWORD_VERIFIER":
                # Hive is asking for the password again rather than
                # challenging the device. Nothing to remember.
                return (registered + "; Hive did not ask to remember this "
                        "device, so no code was needed anyway").strip("; ")
            return (registered + f"; unexpected challenge {challenge}"
                    ).strip("; ")
        except HiveError as exc:
            return f"could not register the device ({exc})"

    # -- data -------------------------------------------------------------

    def _refresh_urls(self) -> list[str]:
        """Where to try refreshing the session.

        The global endpoint is the documented one. The regional one is
        the same path on the host that serves this account's data, and
        it has been the one that answers.
        """
        urls = [REFRESH_URL]
        base = (self.base or "").rstrip("/")
        if base:
            regional = base + "/cognito/refresh-token"
            if regional not in urls:
                urls.append(regional)
        return urls

    def api(self, path: str, _renewed: bool = False) -> dict:
        """Call the Hive API.

        A 401 means the token has died. If the session can be renewed,
        it is renewed and the call is made again - once, so a broken
        session cannot loop. A session that renews itself must not need
        a person: the alternative is a dashboard that goes deaf at the
        top of every hour and stays deaf until somebody notices.
        """
        url = f"{self.base}{path}"
        request = urllib.request.Request(url, headers={
            "authorization": self.tokens.get("token", ""),
            "content-type": "application/json",
            "Accept": "*/*",
            "User-Agent": "ember/0.1",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401 and not _renewed:
                try:
                    self.refresh()
                except HiveError as refresh_error:
                    raise HiveError(self._unauthorised(refresh_error)) from exc
                # Put the renewed token where the next poll will find it.
                self._save()
                return self.api(path, _renewed=True)
            if exc.code == 401:
                raise HiveError(self._unauthorised(detail)) from exc
            raise HiveError(f"HTTP {exc.code} on {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HiveError(f"could not reach the Hive API: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise HiveError(f"Hive sent something that was not JSON on {path}") from exc

    def _unauthorised(self, reason: str) -> str:
        """What to say when Hive will not talk to us.

        "Unauthorized" on its own is a puzzle. The useful facts are
        when the session died and what to do about it.
        """
        when = ""
        expiry = self._expiry
        if expiry:
            when = (" The token in the session file expired at "
                    f"{time.strftime('%H:%M', time.localtime(expiry))}.")
        elif self._last_error:
            when = f" {self._last_error}"
        # The dashboard adds what it did about it - it signs in again by
        # itself - so this says what has to happen, and the dashboard
        # says whether it has already been tried.
        return (f"Hive says the session is not authorised ({reason}).{when} "
                f"This session has to be signed in again.")

    def nodes(self) -> dict:
        return self.api("/nodes/all?products=true&devices=true&actions=true")

    def product_events(self, product_type: str, product_id: str,
                       start: str, end: str) -> dict:
        """Per-product events, which may hand us state changes without polling."""
        query = urllib.parse.urlencode({"from": start, "to": end})
        return self.api(f"/products/{product_type}/{product_id}/events?{query}")

    # -- reading the state ------------------------------------------------

    def read_state(self) -> dict:
        """The heating state, as the dashboard needs it.

        The shape of the reply, confirmed against the live account:

        * The top level is ``{products, devices, user, homes, actions, ...}``.
          There is no ``nodes`` list - that is the old API's name.
        * ``products`` is a list of *zones*. A ``trvcontrol`` product is
          one room (Dining Room, Downstairs Hallway, ...) and carries
          ``state.name`` for the room, ``props.temperature`` for what the
          room is reading now and ``state.target`` for what it is being
          asked for.
        * ``products[type=heating]`` is the boiler controller: the
          thermostat, the schedule, the boost and the holiday mode.
        * ``devices`` is the list of physical things - the hub, the
          thermostat and the individual valves. A zone's ``props.trvs``
          lists which valves belong to it.

        The rules for what the numbers mean come from the reference
        library (Pyhive, the one Home Assistant uses):

        * A zone is **calling for heat** when its temperature is below
          its target. That is exactly how the app decides "ON" versus
          "OFF".
        * **Boost** is ``state.boost`` - null when there is none, and an
          end time when there is.
        * **Boiler working** is ``props.working``.
        """
        payload = self.nodes()
        products = payload.get("products") or []
        devices = payload.get("devices") or []
        if isinstance(devices, dict):
            device_by_id = devices
        else:
            device_by_id = {
                item.get("id"): item for item in devices
                if isinstance(item, dict) and item.get("id")
            }

        heating_product = None
        zones: list[dict] = []

        # Every boost in the house, wherever it is.
        #
        # A boost can be on the boiler or on one room's valve, and they
        # are different things: the boiler's boost lifts the whole
        # house to a higher target, a room's boost holds that one
        # valve open for extra heat. The ring shows whichever ends
        # soonest and says which it is.
        boosts: list[dict] = []

        for product in products:
            if not isinstance(product, dict):
                continue
            kind = str(product.get("type", "")).lower()
            props = product.get("props") or {}
            state = product.get("state") or {}

            if kind in ("heating", "heatingcontroller", "boilermodule",
                        "thermostat"):
                if heating_product is None:
                    heating_product = product
                continue

            if kind not in ("trvcontrol", "trv", "radiator", "radiatorvalve"):
                continue

            name = (state.get("name") or props.get("zoneName") or "").strip()
            current = _number(props.get("temperature"))
            target = _number(state.get("target"))
            if target is None:
                target = _number(state.get("heat"))
            frost = _number(state.get("frostProtection"))
            boost = state.get("boost")

            calling = None
            if current is not None and target is not None:
                calling = current < target

            # The zone points at the valve(s) inside it. Hive does not
            # report a valve position anywhere in the reply - there is
            # no position, motorPosition or open/closed field - so the
            # state shown has to be inferred, the same way the app does
            # it: a room is asking for heat when it is below its target.
            #
            # What the devices *do* carry is how many valves are in the
            # room and how healthy their batteries are.
            trv_ids = props.get("trvs") or []
            batteries: list[float] = []
            position = None
            for trv_id in trv_ids:
                device = device_by_id.get(trv_id)
                if not device:
                    continue
                device_props = device.get("props") or {}
                battery = _number(device_props.get("battery"))
                if battery is not None:
                    batteries.append(battery)
                if position is None:
                    position = _number(_first(
                        device_props.get("position"),
                        device_props.get("motorPosition"),
                        device_props.get("trvPosition"),
                    ))

            if boost:
                boosts.append({"room": name, "until": boost,
                               "source": "room"})

            zones.append({
                "room": name,
                "current": current,
                "target": target,
                "frost": frost,
                "mode": state.get("mode"),
                "calling": calling,
                "boost": boost if boost else None,
                "schedule_override": props.get("scheduleOverride"),
                "online": props.get("online"),
                "position": position,
                "valve_count": len(trv_ids),
                "battery": min(batteries) if batteries else None,
            })

        result = {
            "boiler_on": None,
            "home_temp": None,
            "target_temp": None,
            "outside_temp": None,
            "boost_until": None,
            "boost_minutes": None,
            "boosts": boosts,
            "next_block": None,
            "mode": None,
            "holiday": False,
            "rooms": [],
            "node_count": len(products),
            "device_count": len(devices),
        }

        if heating_product is not None:
            props = heating_product.get("props") or {}
            state = heating_product.get("state") or {}
            result["home_temp"] = _number(props.get("temperature"))
            target = _number(state.get("target"))
            if target is None:
                target = _number(state.get("heat"))
            result["target_temp"] = target
            boost = state.get("boost")
            if boost:
                result["boost_until"] = str(boost)
                boosts.append({"room": "Boiler", "until": boost,
                               "source": "boiler"})
                result["boost_minutes"] = _number(state.get("boostDuration"))
            result["mode"] = state.get("mode")
            holiday = props.get("holidayMode") or {}
            result["holiday"] = bool(holiday.get("active"))
            working = props.get("working")
            if isinstance(working, str):
                result["boiler_on"] = working.upper() in ("HEAT", "ON", "HEATING")
            elif isinstance(working, bool):
                result["boiler_on"] = working

        # If the API did not say whether the boiler is working, infer it:
        # if any zone is below its target, the boiler is on.
        if result["boiler_on"] is None:
            calling = [zone["calling"] for zone in zones
                       if zone.get("calling") is not None]
            result["boiler_on"] = bool(calling) and any(calling)

        result["rooms"] = zones
        return result

    @staticmethod
    def _read_trv(attributes: dict) -> dict | None:
        """Read a TRV from a flat attribute dict.

        Kept for the older node-shaped replies, which some parts of the
        API still use.
        """
        target = attributes.get("setpoint")
        if target is None:
            target = attributes.get("targetTemperature")
        current = attributes.get("temperature")
        if current is None:
            current = attributes.get("currentTemperature")
        if not isinstance(target, (int, float)) and not isinstance(current, (int, float)):
            return None
        position = attributes.get("position")
        valve_open = None
        if isinstance(position, (int, float)):
            valve_open = position > 0
        elif isinstance(attributes.get("state"), str):
            valve_open = attributes["state"].lower() in ("open", "on", "heat", "heating")
        return {
            "target": float(target) if isinstance(target, (int, float)) else None,
            "current": float(current) if isinstance(current, (int, float)) else None,
            "valve_open": valve_open,
        }


def _number(value: Any) -> float | None:
    """A number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    """The first value that is not None."""
    for value in values:
        if value is not None:
            return value
    return None


import urllib.parse  # noqa: E402  (used by product_events)

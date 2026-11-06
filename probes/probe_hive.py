#!/usr/bin/env python3
"""Probe the Hive API for heating state.

A diagnostic, and the older way to set Ember up. The dashboard now has
a settings page that signs in to Hive from the browser, so this is no
longer needed to get Ember working - but it is still the sharpest way
to see what the account actually contains, and it is what to run when
the shape of the API moves.

It shares the app's sign-in code rather than having its own copy. That
matters more than it sounds: a wrong password and a broken handshake
produce exactly the same message from Cognito, so two copies of the
handshake can disagree about which one is broken. One copy cannot.

What it finds:
  * which devices the account has - thermostat, TRVs, hot water, hub
  * their current state: temperatures, mode, boost, schedule, battery
  * the raw shape of one device, so the app's parser can be written to it

The login needs your password, and possibly a 2FA code by SMS. Both come
from the terminal, never from a file. The tokens it gets are written to
hive-session.json so the dashboard can refresh them unattended later.

Usage:
    HIVE_USER=you@example.com HIVE_PASS=your-password python3 probe_hive.py

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ember.hive import (G_HEX, N_HEX, Hive, HiveError, hex_hash, pad_hex,
                        srp_key)

#: The session file has to be somewhere both the probe and the running
#: app can find. The default is the same place the app keeps its data.
SESSION_FILE = os.path.abspath(
    os.environ.get("EMBER_HIVE_SESSION")
    or os.path.join(os.path.expanduser("~"), ".local", "share", "ember",
                    "hive-session.json")
)
#: A second copy next to the probe, so it is easy to find and to copy to
#: another machine (the Pi, usually).
LOCAL_SESSION_FILE = os.path.join(HERE, "hive-session.json")

# --------------------------------------------------------------------------
# A self-check, so the probe never blames your password for its own bug
# --------------------------------------------------------------------------

def reference_session_key(pool_suffix, username, password, salt_hex,
                           server_b_hex, small_a, big_a):
    """The AWS Cognito SRP flow, implemented independently.

    Deliberately written from the specification rather than by calling
    the functions above, so that it is a real cross-check rather than a
    copy of the same code.
    """
    n = int(N_HEX, 16)
    g = int(G_HEX, 16)

    def sha256_hex(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest().rjust(64, "0")

    def hash_of_hex(text: str) -> str:
        return sha256_hex(bytes.fromhex(text))

    def pad(value) -> str:
        text = format(value, "x") if not isinstance(value, str) else value
        if len(text) % 2 == 1:
            text = "0" + text
        elif text[0] in "89abcdefABCDEF":
            text = "00" + text
        return text

    k = int(hash_of_hex("00" + N_HEX + "0" + format(g, "x")), 16)
    server_b = int(server_b_hex, 16)
    u = int(hash_of_hex(pad(big_a) + pad(server_b)), 16)

    secret = sha256_hex(f"{pool_suffix}{username}:{password}".encode("utf-8"))
    x = int(hash_of_hex(pad(salt_hex) + secret), 16)

    s = pow(server_b - k * pow(g, x, n), small_a + u * x, n)

    prk = hmac.new(bytes.fromhex(pad(u)), bytes.fromhex(pad(s)),
                   hashlib.sha256).digest()
    info = b"Caldera Derived Key" + bytes([1])
    return hmac.new(prk, info, hashlib.sha256).digest()[:16]


def self_check() -> tuple[bool, str]:
    """Prove the SRP maths before trying to sign in.

    A wrong password and a broken handshake produce the exact same
    message from Cognito - 'Incorrect username or password' - so without
    this check there is no way to tell which one happened. With it, a
    failure means the maths is wrong, not your password.
    """
    n = int(N_HEX, 16)
    k = int(hex_hash("00" + N_HEX + "0" + G_HEX), 16)
    small_a = 0x1234567890ABCDEF1234567890ABCDEF
    big_a = pow(2, small_a, n)
    server_b_hex = format(pow(2, small_a + 7, n), "x")
    salt_hex = ("0a1b2c3d4e5f60718293a4b5c6d7e8f9"
                "0a1b2c3d4e5f60718293a4b5c6d7e8f9")
    username = "someone@example.com"
    password = "correct horse battery staple"
    suffix = "SamNfoWtf"

    mine = srp_key(suffix, username, password, int(server_b_hex, 16),
                   salt_hex, small_a, big_a, n, k)
    theirs = reference_session_key(suffix, username, password, salt_hex,
                                    server_b_hex, small_a, big_a)
    if mine != theirs:
        return False, "the session key does not match the reference"
    return True, "the session key matches the reference implementation"



def save_both(hive: Hive) -> None:
    """Write the session to the shared place and to a copy next to the probe."""
    for path in (SESSION_FILE, LOCAL_SESSION_FILE):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            hive.save_session(path)
        except OSError:
            pass


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("-" * len(title))


def shape(value, depth: int = 0) -> str:
    """What came back, described by shape rather than by value."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = [f"{k}: {shape(v, depth + 1)}"
                 for k, v in list(value.items())[:8]]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} x {shape(value[0], depth + 1)}]"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "text"


def summarise(value, path: str = "") -> list[str]:
    """Pull the interesting fields out of an arbitrarily shaped reply."""
    lines = []
    if isinstance(value, dict):
        interesting = ("temperature", "target", "state", "mode", "boost",
                       "battery", "online", "name", "type", "hiveType",
                       "schedule", "signal", "level", "position")
        for key, item in value.items():
            if any(word.lower() in key.lower() for word in interesting):
                if isinstance(item, (dict, list)):
                    lines.append(f"{path}{key} = {shape(item)}")
                else:
                    lines.append(f"{path}{key} = {item}")
            lines += summarise(item, f"{path}{key}.")
    elif isinstance(value, list):
        for index, item in enumerate(value[:40]):
            lines += summarise(item, f"{path}[{index}].")
    return lines


def main() -> int:
    hive = Hive()

    rule("Hive SSO")
    hive.read_sso()
    print(f"  pool id      {hive.pool_id}")
    print(f"  region       {hive.region}")
    print(f"  client id    {hive.client_id}")

    rule("Sign in")
    ok, note = self_check()
    print(f"  {'SRP maths OK' if ok else 'SRP MATHS WRONG'} - {note}")
    if not ok:
        print("\n  Stopping before signing in: the handshake is wrong, so a")
        print("  failure would say 'Incorrect username or password' even")
        print("  when your password is perfectly fine.")
        return 1
    reused = False
    for path in (SESSION_FILE, LOCAL_SESSION_FILE):
        if os.path.exists(path) and hive.load_session(path):
            reused = True
            break
    if reused:
        print("  reused the saved session and refreshed the token")
    else:
        username = (os.environ.get("HIVE_USER") or "").strip()
        password = os.environ.get("HIVE_PASS") or ""
        if not username or not password:
            raise HiveError(
                "HIVE_USER and HIVE_PASS must be set:\n"
                "  HIVE_USER=you@example.com HIVE_PASS=your-password \\\n"
                "      python3 probe_hive.py")
        result = hive.login(username, password)
        if result.get("status") == "needs_code":
            print("\n  Hive has sent a 2FA code by SMS.")
            print("  (If it has not arrived in a minute, check the account's phone.)")
            code = input("  Code: ").strip()
            result = hive.complete_code(result["session"], result["username"],
                                        code, email=username)
        print("  signed in")
        result = hive.remember_device()
        print(f"  {result}")
        save_both(hive)

    rule("Devices")
    try:
        data = hive.nodes()
    except HiveError as exc:
        print(f"  !! {exc}")
        print("\n  The login worked but the data call did not. The shape of")
        print("  this API may have moved - the error above says where.")
        save_both(hive)
        return 1

    with open(os.path.join(HERE, "hive-nodes.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, default=str)

    print(f"  top-level keys: {', '.join(sorted(data.keys()))[:300]}")
    for key, value in data.items():
        size = len(value) if isinstance(value, (list, dict)) else 1
        print(f"    {key}: {type(value).__name__} ({size})")

    rule("What the devices look like")
    for line in summarise(data)[:120]:
        print(f"  {line}")

    rule("Done")
    print(f"  Raw reply written to {os.path.join(HERE, 'hive-nodes.json')}")
    print(f"  Session written to  {SESSION_FILE}")
    print(f"  Also copied to      {LOCAL_SESSION_FILE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HiveError as exc:
        print(f"\n\033[1mFailed:\033[0m {exc}")
        sys.exit(1)

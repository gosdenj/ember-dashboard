"""Working out the settings from the little you do know.

The problem this solves: Ember needs an account number, a meter point, a
meter serial, a product code, a tariff code and a unit before it can
show a single gas number. Asking someone to type all of that in is how
Ember ends up with a half-configured dashboard and an empty panel.

So Ember asks for the two things people actually have - the Octopus API
key and the Hive login - and works the rest out by talking to the
services. Everything here returns two things:

  * ``settings``: the settings to store, ready to hand to
    ``Settings.update()``
  * ``summary``: what was found, in words, for the settings page to show

The rule throughout: report what was actually found, and say so when
something is missing rather than filling it in with a plausible guess.
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

from .hive import Hive, HiveError
from .octopus import (Octopus, OctopusError, declared_unit, pick_live_meter)
from .weather import (WeatherError, geocode, looks_like_a_postcode,
                     normalise_postcode)


class DiscoveryError(Exception):
    """Anything that went wrong while working the settings out."""


def _first(values: list[Any]) -> Any:
    for value in values:
        if value:
            return value
    return ""


# --------------------------------------------------------------------------
# Octopus
# --------------------------------------------------------------------------

def discover_octopus(api_key: str, account: str = "",
                     current_unit: str = "") -> dict:
    """Work out the whole gas account from the API key alone.

    The account number is found rather than asked for, because the GraphQL
    API knows which accounts a key can see. The meter point, the meters
    and the tariff all come from the account.

    Every meter on the meter point is kept, not just the first: a meter
    exchange leaves the old meter's history and the new meter's present
    on the same meter point, and both are needed to draw a continuous
    line.

    ``account`` is for the rare case of a key that can see more than one
    account: give it and it is used, leave it empty and the first one the
    key can see is taken.

    ``current_unit`` is what the settings say at the moment, so the
    discovery can say what it did with it. A unit pinned in the settings
    is put back to "work it out" when the meter can be read, so the
    dashboard checks it against the readings. When the meter cannot be
    read there is no evidence either way, so the pin is left alone - and
    the page is told, rather than being told it will work it out.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise DiscoveryError("an Octopus API key is needed")

    account = (account or "").strip()
    accounts: list[str] = []
    if account:
        # The account was named, so use it - but still check the key can
        # see it, because a typo here would otherwise fail much later
        # with a confusing message.
        accounts = Octopus(api_key, account).accounts()
        if accounts and account not in accounts:
            raise DiscoveryError(
                f"that key cannot see account {account}. It can see: "
                f"{', '.join(accounts)}")
        if not accounts:
            # The key sees nothing at all, which is worth saying.
            raise DiscoveryError(
                "Octopus did not recognise that key, or it cannot see any "
                "account. Check it is a live key, not a sandbox one.")
    else:
        # The account number is not known yet, so a placeholder goes in
        # until the GraphQL API says which accounts this key can see.
        accounts = Octopus(api_key, "A-0").accounts()
        if not accounts:
            raise DiscoveryError(
                "Octopus did not recognise that key, or it cannot see any "
                "account. Check it is a live key, not a sandbox one.")
        account = accounts[0]

    client = Octopus(api_key, account)
    settings: dict[str, Any] = {
        "octopus_api_key": api_key,
        "octopus_account": account,
    }
    summary: dict[str, Any] = {
        "account": account,
        "accounts": accounts,
    }

    agreements = client.gas_agreements()
    mprn = _first([a.get("mprn") for a in agreements])
    if not mprn:
        raise DiscoveryError(
            f"account {account} is reachable but has no gas meter point "
            f"on it. If the gas account is on a different Octopus account, "
            f"set the account number by hand.")
    settings["gas_mprn"] = mprn
    summary["mprn"] = mprn

    # -- the meters -------------------------------------------------------
    serials: list[str] = []
    meter_type = ""
    live_serial = ""
    consumption_units = ""
    for agreement in agreements:
        for serial in agreement.get("serials") or []:
            serial = str(serial or "").strip()
            if serial and serial not in serials:
                serials.append(serial)
        if not meter_type:
            meter_type = str(agreement.get("meter_type") or "").strip()
        if not live_serial:
            live_serial = str(agreement.get("live_serial") or "").strip()
    if not serials:
        # The account payload did not list the meters. Ask the meter
        # point directly - it always knows its own meters.
        try:
            detail = client.get(f"gas-meter-points/{mprn}/meters/")
        except OctopusError:
            detail = {}
        listed = detail.get("results") or detail.get("meters") or []
        described: list[dict] = []
        for meter in listed:
            if not isinstance(meter, dict):
                continue
            serial = str(meter.get("serial_number")
                         or meter.get("serialNumber")
                         or meter.get("serial") or "").strip()
            if serial and serial not in serials:
                serials.append(serial)
            described.append({
                "serial": serial,
                "meter_type": str(meter.get("meter_type")
                                  or meter.get("meterType") or ""),
                "consumption_units": str(meter.get("consumption_units")
                                        or meter.get("consumptionUnits") or ""),
                "active_from": str(meter.get("active_from")
                                   or meter.get("activeFrom") or ""),
                "active_to": str(meter.get("active_to")
                                 or meter.get("activeTo") or ""),
                "installation_date": str(meter.get("installation_date")
                                         or meter.get("installationDate") or ""),
            })
        # The same rule as everywhere else: the meter in service, not
        # the first one listed.
        live = pick_live_meter(described)
        if live:
            if not meter_type:
                meter_type = live.get("meter_type") or ""
            live_serial = live.get("serial") or live_serial
            consumption_units = (live.get("consumption_units")
                                 or consumption_units)
    if serials:
        settings["gas_meter_serials"] = ",".join(serials)
        settings["gas_meter_serial"] = serials[0]
    summary["serials"] = serials
    summary["meter_type"] = meter_type

    # -- the tariff -------------------------------------------------------
    found = client.find_tariff(mprn)
    product = str(found.get("product_code") or "")
    tariff = str(found.get("tariff_code") or "")
    consumption_units = str(found.get("consumption_units") or "").strip()
    if product:
        settings["gas_product_code"] = product
    if tariff:
        settings["gas_tariff_code"] = tariff
    summary["product_code"] = product
    summary["tariff_code"] = tariff
    summary["tariff_name"] = found.get("display_name") or ""
    if found.get("meter_type") and not meter_type:
        meter_type = str(found["meter_type"])
    if found.get("live_serial") and not live_serial:
        live_serial = str(found["live_serial"])
    if found.get("consumption_units") and not consumption_units:
        consumption_units = str(found["consumption_units"])
    if found.get("serials"):
        for serial in found["serials"]:
            if serial not in serials:
                serials.append(serial)
        settings["gas_meter_serials"] = ",".join(serials)
    if serials:
        # The single-serial setting points at the meter that is in
        # service - the one whose readings are the current ones. The
        # retired meter is still read, through the list, so the
        # history stays continuous across an exchange.
        settings["gas_meter_serial"] = live_serial or serials[-1]
    summary["live_serial"] = live_serial

    # -- the unit ---------------------------------------------------------
    # The meter's own record says what it measures. That is better
    # evidence than any guess from the size of the numbers.
    unit = declared_unit(meter_type, consumption_units)
    if unit:
        # The meter's type is recorded, and the unit is put back to
        # "work it out". The dashboard prefers the meter's own record
        # *and* checks it against the readings, and says which it used
        # and why - a unit pinned here would skip that check.
        #
        # The pin is also cleared, because a settings file written by an
        # older version of this discovery may carry a unit that was
        # taken from the wrong meter. Leaving it would keep the wrong
        # number on the dashboard for exactly the people who ran it.
        settings["gas_meter_type"] = meter_type or (
            "SMETS2" if unit == "m3" else "SMETS1")
        settings["gas_unit"] = "auto"
        # Say so, rather than quietly overriding whatever was there.
        summary["unit_reset"] = (
            "the unit was put back to \"work it out\", so Ember checks the "
            "meter against the readings and says which it used")
        summary["unit"] = unit
        why = consumption_units or meter_type
        summary["unit_reason"] = f"the meter record says {why}"
    else:
        summary["unit"] = ""
        if current_unit and current_unit != "auto":
            # The meter could not be read, so there is no evidence about
            # the pin either way. It stays - and the page is told, so it
            # does not go on saying the unit will be worked out.
            summary["unit_reason"] = (
                "the meter record does not say what it measures, so the "
                f"unit you have set ({current_unit}) is left as it is")
            summary["unit_kept"] = current_unit
        else:
            summary["unit_reason"] = (
                "the meter record does not say - Ember will work it out "
                "from the readings and say so when it does")

    # -- the rates --------------------------------------------------------
    rates: dict[str, Any] = {}
    if product and tariff:
        try:
            rates = client.rates_now(product, tariff)
        except OctopusError:
            rates = {}
    unit_rate = rates.get("unit_rate_p")
    standing = rates.get("standing_charge_p")
    if unit_rate is None and found.get("unit_rate") is not None:
        unit_rate = found["unit_rate"]
    if standing is None and found.get("standing_charge") is not None:
        standing = found["standing_charge"]
    summary["unit_rate_p"] = unit_rate
    summary["standing_charge_p"] = standing

    # A missing tariff is worth reporting, but it is not worth throwing
    # away the account, the meter point, the meters and the key that
    # were all found. The user would have to type the key again, and
    # the dashboard would still be better off knowing the meter point.
    notes: list[str] = []
    if not product or not tariff:
        missing = []
        if not product:
            missing.append("product")
        if not tariff:
            missing.append("tariff")
        notes.append(
            f"Octopus did not say which {', '.join(missing)} is in force, "
            f"so the cost of the gas is not known yet. That can happen "
            f"when the tariff has just changed.")
        summary["missing"] = missing
    summary["notes"] = notes

    return {"settings": settings, "summary": summary}


# --------------------------------------------------------------------------
# Hive
# --------------------------------------------------------------------------

def _client_with_known_device(session_path: str | None = None) -> Any:
    """A Hive client that knows the device already on file.

    An account that tracks devices asks for a one-time code whenever a
    sign-in does not present a device it recognises. The device is in
    the session file, so a sign-in from the settings page should use
    it - otherwise every sign-in needs a code, and the dashboard that
    was built to be unattended is the one thing that cannot be.

    It also carries the pool id, which is what the password hash is
    built from.
    """
    hive = Hive()
    path = Path(session_path or default_session_path())
    if not path.exists():
        return hive
    try:
        hive.load_session(path)
    except Exception:
        # A session file that cannot be read is not a reason to refuse
        # to sign in: the sign-in will fetch the details it needs.
        pass
    return hive


def hive_login(email: str, password: str, session_path: str | None = None) -> dict:
    """Start a Hive sign-in and store the session if it goes through.

    Returns the result of :meth:`ember.hive.Hive.login`, plus the
    session file that was written when the sign-in succeeded.

    The result may carry the tokens. It is the caller's job to strip
    them before it goes anywhere near the browser - see
    ``ember.server``.
    """
    hive = _client_with_known_device(session_path)
    result = hive.login(email, password)
    if result.get("status") == "signed_in":
        # The device handshake comes **before** the session is written.
        #
        # The handshake replaces the tokens with the pair that belongs
        # to the device - a refresh token that is bound to that device,
        # and the device key to go with it. Writing the file first
        # saved the *older* pair: a refresh token with no device to
        # present, which is refused with "Invalid Refresh Token" an
        # hour later, when the token first needs renewing.
        result["device"] = hive.remember_device()
        path = _write_session(hive, session_path)
        result["session_file"] = str(path)
        result["rooms"] = _count_rooms(hive)
    return result


def hive_code(session: str, username: str, code: str,
              session_path: str | None = None, email: str = "") -> dict:
    """Finish a Hive sign-in with the two-step code, and store it."""
    hive = _client_with_known_device(session_path)
    result = hive.complete_code(session, username, code, email=email)
    if result.get("status") == "signed_in":
        # Same order as above: the device handshake first, the file
        # afterwards, so the file holds the tokens that go with the
        # device.
        result["device"] = hive.remember_device()
        path = _write_session(hive, session_path)
        result["session_file"] = str(path)
        result["rooms"] = _count_rooms(hive)
    return result


def default_session_path() -> str:
    """Where the session goes when nobody says otherwise."""
    from pathlib import Path
    import os

    directory = Path(os.environ.get("EMBER_DATA_DIR")
                     or Path.home() / ".local" / "share" / "ember")
    return str(directory / "hive-session.json")


def _write_session(hive: Hive, session_path: str | None = None) -> Any:
    """Write the session where the app will find it."""
    from pathlib import Path

    path = Path(session_path or default_session_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    hive.save_session(path)
    return path


def _count_rooms(hive: Hive) -> list[str]:
    """The rooms the account actually has, so the page can show them."""
    try:
        state = hive.read_state()
    except HiveError:
        return []
    rooms = []
    for room in state.get("rooms") or []:
        # read_state() calls the key "room"; accept "name" too, so a
        # change in one place does not quietly zero the count.
        name = room.get("room") or room.get("name")
        if name:
            rooms.append(name)
    return rooms


# --------------------------------------------------------------------------
# Where the house is
# --------------------------------------------------------------------------

def discover_place(postcode: str) -> dict:
    """Turn a postcode into a latitude and longitude.

    A UK postcode goes to postcodes.io and a place name goes to
    Open-Meteo. Neither needs a key, so there is nothing else to
    configure here.
    """
    postcode = (postcode or "").strip()
    if not postcode:
        raise DiscoveryError("a postcode is needed")
    try:
        place = geocode(postcode)
    except WeatherError as exc:
        raise DiscoveryError(str(exc)) from exc
    # The postcode is stored in the shape it should be, so that what
    # is saved and what was looked up are the same thing.
    canonical = normalise_postcode(postcode) if looks_like_a_postcode(postcode) \
        else postcode
    return {
        "settings": {
            "postcode": canonical,
            "latitude": place["latitude"],
            "longitude": place["longitude"],
        },
        "summary": {
            "postcode": canonical,
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "name": place.get("name", ""),
            "country": place.get("country", ""),
            "region": place.get("region", ""),
        },
    }


# --------------------------------------------------------------------------
# The half-finished sign-ins
# --------------------------------------------------------------------------

class HiveFlows:
    """Sign-ins waiting for a two-step code.

    The code Hive texts you cannot be guessed, read from a file or worked
    out from a password, so the sign-in has to be paused and picked up
    again when the code arrives. The paused state is the Cognito session
    and the username - nothing secret beyond what the code itself is.

    Flows expire: a code that arrives an hour later is no use, and
    keeping stale sessions around is only an invitation.
    """

    def __init__(self, ttl: float = 600.0,
                 session_path: str | None = None) -> None:
        self.ttl = ttl
        self.session_path = session_path
        self._flows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._flows.items()
                    if now - v["created"] > self.ttl]:
            self._flows.pop(key, None)

    def start(self, email: str, password: str) -> dict:
        """Begin a sign-in. Returns the login result, with a flow id added."""
        result = hive_login(email, password, self.session_path)
        if result.get("status") == "needs_code":
            flow_id = secrets.token_urlsafe(18)
            with self._lock:
                self._prune()
                self._flows[flow_id] = {
                    "session": result.get("session", ""),
                    "username": result.get("username", ""),
                    # What was typed, which is not the same as
                    # Cognito's canonical user id. The device
                    # registration needs the one that was typed.
                    "email": email,
                    "created": time.monotonic(),
                }
            result["flow"] = flow_id
        return result

    def complete_code(self, flow_id: str, code: str) -> dict:
        """Finish a sign-in with the code that was texted.

        A mistyped code does not end the sign-in. The Cognito session
        is still good, so the flow stays open and the code can be
        typed again - which is the difference between a slip and
        starting the whole thing over and waiting for another SMS.
        """
        flow_id = (flow_id or "").strip()
        with self._lock:
            self._prune()
            flow = self._flows.get(flow_id)
        if flow is None:
            raise DiscoveryError(
                "that sign-in has expired - start again and ask for a "
                "new code")
        result = hive_code(flow["session"], flow["username"], code,
                           self.session_path, flow.get("email", ""))
        # Only now is the sign-in finished, so only now does the flow go.
        with self._lock:
            self._flows.pop(flow_id, None)
        return result

    def pending(self) -> int:
        with self._lock:
            self._prune()
            return len(self._flows)

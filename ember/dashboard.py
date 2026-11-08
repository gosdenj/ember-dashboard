"""The dashboard: one payload, assembled from four sources.

Hive knows the heating. Octopus knows the gas. Open-Meteo knows the
weather. The history store knows what happened yesterday. This module
pulls them together into the single JSON document the browser renders, and
pushes a new one over Server-Sent Events whenever it changes.

Every number in the payload carries its origin. The browser is told which
panel came from Hive and which came from the meter, so the dashboard can
say so on the panel itself rather than leaving the reader to guess.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import forecast as forecast_model
from .config import ROOMS
from . import weather as weather_module
from .history import History, midnight
from .hive import Hive, HiveError
from .octopus import (Octopus, OctopusError, declared_unit, _parse_moment,
                      rate_at)


def zone(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


# How many whole days the "a day, on average" figure is worked out
# from. A week of finished days: enough to be steady, short enough to
# follow the season.
AVERAGE_DAYS = 7

# How long to wait before trying to sign in again, after a failed
# attempt. A sign-in that keeps failing is a sign-in that might lock
# the account, so it is not worth repeating every poll.
REAUTH_BACKOFF = 900


class Dashboard:
    """Owns the polling loop and the current payload."""

    def __init__(self, settings, history: History, provider=None):
        self.settings = settings
        self.history = history
        self.provider = provider
        self._lock = threading.RLock()
        self._payload: dict[str, Any] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_gas_fetch = 0.0
        # When the whole month of readings was last fetched.
        self._last_gas_backfill = 0.0
        # When the last attempt to sign in again was made, and why it
        # did not work. The refresh token is a cache; the password is
        # the truth, and this is how the truth gets used.
        self._last_reauth = 0.0
        self._reauth_problem = ""
        # The day the history was last pruned. Pruning is a write, so it
        # happens once a day rather than once a poll.
        self._last_tidy: date | None = None
        # Why the last attempt to use a Hive session did not work.
        self._hive_problem = ""
        # The session file that worked, so the page can say where it is.
        self._hive_session_file = ""

    # -- listeners --------------------------------------------------------

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _publish(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(payload)
            except Exception:
                # A broken listener must not take the poller down.
                pass

    def payload(self) -> dict:
        with self._lock:
            return self._payload

    # -- the loop ---------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="ember-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
                self._tidy()
            except Exception as exc:  # keep the dashboard alive
                self._publish(self._degraded(f"the poller hit an error: {exc}"))
            interval = max(30, self.settings.get_int("poll_seconds"))
            self._stop.wait(interval)

    def _tidy(self) -> None:
        """Drop history that has fallen out of the retention window.

        Once a day, not once a poll: pruning is a write, and doing it
        every two minutes to delete nothing is work for the SD card for
        no reason.
        """
        today = datetime.now(timezone.utc).date()
        if self._last_tidy == today:
            return
        self._last_tidy = today
        try:
            self.history.prune(self.settings.get_int("history_days"),
                               self.settings.get_int("trv_days"))
        except Exception:
            pass

    def poll(self) -> dict:
        """One cycle. Returns the new payload."""
        tz = zone(self.settings.get("timezone", "Europe/London"))
        errors: list[dict] = []

        heating = self._poll_heating(errors)
        gas = self._poll_gas(errors, tz)
        outside = self._poll_weather(errors)
        # Hive has no outside temperature - the weather is a separate
        # reading - so the ring's outside figure comes from here. It
        # used to be left at nothing, which showed an empty dial on the
        # heating panel even though the weather was working.
        if isinstance(outside, dict) and outside.get("current") is not None:
            heating["outside_temp"] = outside.get("current")
        self._record_heating(heating, outside)
        derived = self._derive(heating, gas, outside, tz, errors)
        # It belongs with the gas: it is an estimate of today's gas, not
        # a separate kind of number. It is always present, so the
        # payload's shape does not change with the weather - it is None
        # when there is nothing to estimate.
        gas["today_estimate"] = derived.get("today_estimate")

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "demo" if self.settings.get_bool("demo") else "live",
            # Today and tomorrow, in words and in symbols. The forecast
            # needs to say what the weather will be, not only how warm
            # it will be.
            "weather": weather_module.summary(outside, tz),
            "home": {
                "name": self.settings.get("panel_name", "Home"),
                "timezone": self.settings.get("timezone", "Europe/London"),
            },
            "heating": heating,
            "gas": gas,
            "forecast": derived.get("forecast", {}),
            "budget": derived.get("budget", {}),
            "errors": errors,
        }
        self._publish(payload)
        return payload

    def _gas_history_days(self) -> float:
        """How many days of gas readings the store holds."""
        try:
            oldest = self.history.oldest_gas_reading()
            newest = self.history.newest_gas_reading()
        except Exception:
            return 0.0
        if oldest is None or newest is None:
            return 0.0
        return max(0.0, (newest - oldest) / 86400.0)

    def history_stats(self) -> dict:
        """What is in the history store, and how big it has become.

        The question "will this fill the card?" deserves a number.
        """
        try:
            stats = self.history.stats()
        except Exception as exc:
            return {"rows": {}, "bytes": 0, "path": "", "error": str(exc)}
        stats["history_days"] = self.settings.get_int("history_days")
        stats["trv_days"] = self.settings.get_int("trv_days")
        stats["poll_seconds"] = max(30, self.settings.get_int("poll_seconds"))
        stats["rows_per_day"] = self._rows_per_day()
        return stats

    def _rows_per_day(self) -> dict:
        """How much the store grows in a day, at the current poll rate.

        This is the number that says whether the database has a future.
        """
        interval = max(30, self.settings.get_int("poll_seconds"))
        polls = int(86400 // interval)
        try:
            rooms = len(self.history.latest_trvs()) or len(ROOMS)
        except Exception:
            rooms = len(ROOMS)
        # The room log only writes when a room's state changes, so the
        # steady-state rate is a handful of rows a day, not one per
        # room per poll. The pessimistic figure is quoted alongside it.
        return {
            "polls": polls,
            # The live fetch asks Octopus for hourly figures, so a day
            # is 24 rows. The demo seeds half-hourly ones, which is 48.
            "gas": 24,
            "trv_changes": rooms * 2,
            "trv_if_every_poll": polls * rooms,
        }

    def _degraded(self, message: str) -> dict:
        with self._lock:
            payload = dict(self._payload)
        payload.setdefault("errors", []).append({"source": "ember", "message": message})
        return payload

    # -- sources ----------------------------------------------------------

    def _poll_heating(self, errors: list[dict]) -> dict:
        if self.provider is not None:
            try:
                return self.provider.heating()
            except Exception as exc:
                errors.append({"source": "demo", "message": str(exc)})
                return {}

        result: dict[str, Any] = {
            "boiler_on": False,
            "home_temp": None,
            "target_temp": None,
            "outside_temp": None,
            "boost_minutes": None,
            "boost_until": None,
            "next_block": None,
            "rooms": [],
        }
        client = self._hive_client()
        if client is None:
            message = "not signed in - " + self._hive_problem
            if "/settings" not in message:
                # The reason may already say where to go; saying it
                # twice reads like two separate problems.
                message += (". Sign in from the settings page (/settings), "
                            "then Ember will pick it up")
            errors.append({"source": "hive", "message": message})
            return result
        try:
            state = client.read_state()
        except HiveError as exc:
            # The token can die between the freshness check and the
            # call. Signing in again is cheap, and the alternative is a
            # panel that has gone deaf until somebody notices.
            if self._reauthenticate(client, self._hive_session_file or ""):
                try:
                    state = client.read_state()
                except HiveError as again:
                    errors.append({"source": "hive", "message": str(again)})
                    return result
            else:
                message = str(exc)
                if self._reauth_problem:
                    message += f"; {self._reauth_problem}"
                errors.append({"source": "hive", "message": message})
                return result

        result.update({
            "boiler_on": bool(state.get("boiler_on")),
            "home_temp": state.get("home_temp"),
            "target_temp": state.get("target_temp"),
            "boost_until": state.get("boost_until"),
            # Every boost in the house, so the ring can show whichever
            # one ends soonest - a room's valve or the boiler's.
            "boosts": state.get("boosts") or [],
        })
        rooms = []
        for reading in state.get("rooms", []):
            rooms.append({
                "room": reading.get("room") or "Room",
                "current": reading.get("current"),
                "target": reading.get("target"),
                "valve_open": reading.get("calling"),
                "position": reading.get("position"),
                "valve_count": reading.get("valve_count"),
                "battery": reading.get("battery"),
                "mode": reading.get("mode"),
                # A room whose valve is being boosted, and until when.
                "boost_until": reading.get("boost") or reading.get("boost_until"),
            })
        result["rooms"] = self._ordered_rooms(rooms)
        return result

    def _record_heating(self, heating: dict, outside: dict) -> None:
        """Write this poll down. This is how heating hours come to exist.

        The outside temperature is taken from the weather reading rather
        than from the heating result, because the heating result does
        not have one: the degree-day model needs both halves, and a
        poll recorded without the cold is a poll the forecast cannot
        use.
        """
        if not heating:
            return
        now = time.time()
        current = outside.get("current") if isinstance(outside, dict) else None
        try:
            self.history.record_poll(now, bool(heating.get("boiler_on")),
                                     heating.get("home_temp"), current)
            rooms = heating.get("rooms") or []
            if rooms:
                self.history.record_trvs(now, rooms)
        except Exception:
            # The history store is a convenience, not the point of the
            # poll. If it cannot be written, the panels still show the
            # reading that was just fetched.
            pass

    @staticmethod
    def _ordered_rooms(rooms: list[dict]) -> list[dict]:
        """Put the rooms in the house's own order, not the API's.

        The API has no idea which room you would rather read first, so
        the fixed list wins and anything it does not know about goes
        after it.
        """
        order = {name.lower(): index for index, name in enumerate(ROOMS)}

        def key(room: dict) -> tuple[int, str]:
            index = order.get(str(room.get("room", "")).lower())
            if index is None:
                return (len(order), str(room.get("room", "")).lower())
            return (index, "")

        return sorted(rooms, key=key)

    def _reauthenticate(self, client: Hive, path: str) -> bool:
        """Sign in again, with the login the server already holds.

        A refresh token is a **cache**; the password is the truth. The
        cache can go stale for reasons that have nothing to do with the
        user - Cognito revokes it, the account's device tracking
        changes, Hive changes something on their side - and then the
        only way back is to sign in again. Which is exactly what the
        user would have to do by hand.

        Ember can do it by itself, quietly, because the settings page
        already holds the password, on the server, in a file only the
        owner can read. The browser never sees it.

        Two rules:

        * **A failed attempt is not repeated straight away.** Repeated
          sign-ins are how an account gets locked, so there is a
          back-off, and the panel says what is needed instead.
        * **A new session is saved.** Otherwise the next poll starts
          from the same dead token.
        """
        now = time.time()
        if self._reauth_problem and now - self._last_reauth < REAUTH_BACKOFF:
            # We tried recently and it did not work. Trying again every
            # two minutes would not help, and might hurt.
            return False
        email = self.settings.get("hive_email", "")
        password = self.settings.get("hive_password", "")
        self._last_reauth = now
        if not email or not password:
            self._reauth_problem = (
                "the Hive login is not stored, so the session cannot be "
                "renewed - sign in from the settings page (/settings)")
            return False
        try:
            result = client.login(email, password)
        except HiveError as exc:
            if "Incorrect username or password" in str(exc):
                # The pool's password key may have changed, or the one
                # in the session file may be stale. Ask Hive for its
                # sign-in details again and try once more - one extra
                # attempt, not a loop.
                try:
                    client.read_sso()
                    result = client.login(email, password)
                except HiveError as again:
                    self._reauth_problem = (
                        f"signing in again did not work ({again}) - sign in "
                        f"from the settings page (/settings)")
                    return False
            else:
                self._reauth_problem = (
                    f"signing in again did not work ({exc}) - sign in from "
                    f"the settings page (/settings)")
                return False
        except Exception as exc:
            # A shape nobody has seen before is an issue to report, not
            # a reason to take the whole panel down.
            self._reauth_problem = (
                f"signing in again did not work ({exc}) - sign in from the "
                f"settings page (/settings)")
            return False
        if result.get("status") == "needs_code":
            self._reauth_problem = (
                "Hive wants a one-time code - sign in from the settings "
                "page (/settings)")
            return False
        # The device handshake comes first, for the same reason it does
        # on the settings page: it replaces the tokens with the pair
        # that belongs to the device, and the file has to hold that
        # pair - not the older one.
        try:
            client.remember_device()
        except Exception:
            # Failing to register the device is not fatal: the tokens
            # we have are good for an hour, and the session file will
            # still say which device this is.
            pass
        try:
            client.save_session(path)
        except OSError as exc:
            self._reauth_problem = (f"the new session could not be saved ({exc}) "
                                    "- it will be signed in again next time")
        self._reauth_problem = ""
        return True

    def _hive_client(self) -> Hive | None:
        """The Hive client, if there is a session that works.

        When there is not, the reason is kept - "not signed in" on its
        own is the least useful thing a dashboard can say, because it
        does not distinguish "you never signed in" from "the session
        file is there but the token would not refresh".

        And when the token cannot be refreshed, Ember signs in again by
        itself rather than waiting for a person: the refresh token is
        a cache, and the password is the truth.
        """
        client = Hive(self.settings.get("hive_base", ""))
        tried: list[str] = []
        reasons: list[str] = []
        # A session that loads but cannot be renewed is a last resort:
        # it works until its token dies and then never works again. A
        # session that *can* be renewed is worth more, so keep looking.
        fallback: tuple[Hive, str, str] | None = None
        for path in self._session_paths():
            tried.append(path)
            loaded = client.load_session(path)
            reason = getattr(client, "last_error", "")
            if loaded and not reason:
                self._hive_problem = ""
                self._hive_session_file = str(path)
                return client
            if reason:
                reasons.append(f"{path}: {reason}")
            # The refresh did not work. Sign in again before giving up
            # on the session - this is the difference between a
            # dashboard that needs a person once a month and one that
            # needs one every hour.
            if self._reauthenticate(client, path):
                self._hive_problem = ""
                self._hive_session_file = str(path)
                return client
            if loaded and fallback is None:
                fallback = (client, path, reason)
        if fallback is not None:
            client, path, reason = fallback
            self._hive_problem = (f"the session at {path} could not be "
                                  f"renewed: {reason}")
            if self._reauth_problem:
                self._hive_problem += f"; {self._reauth_problem}"
            self._hive_session_file = str(path)
            return client
        if reasons:
            self._hive_problem = "; ".join(reasons)
        elif tried:
            self._hive_problem = (
                "no session file was found in " + ", ".join(tried))
        else:
            self._hive_problem = "nowhere is set up to look for one"
        if self._reauth_problem:
            self._hive_problem += f"; {self._reauth_problem}"
        return None

    def hive_status(self) -> dict:
        """What the last poll managed to do with Hive.

        "The session file exists" is not the same as "Hive is
        answering" - and the settings page should say which one it is.
        """
        with self._lock:
            payload = dict(self._payload)
        problems = [str(e.get("message", "")) for e in payload.get("errors") or []
                    if e.get("source") == "hive"]
        return {
            "signed_in": not problems,
            "problem": problems[0] if problems else "",
            "session_file": self._hive_session_file,
        }

    def _session_paths(self) -> list[str]:
        """Everywhere the Hive session might be.

        The probe writes it to the shared data directory and to a copy
        next to itself. The app looks in the configured place, the shared
        data directory, and that copy - so it works whether the probe was
        run on this machine or the file was copied over from another.
        """
        paths: list[str] = []
        configured = self.settings.get("hive_session_file", "")
        if configured:
            paths.append(configured)
        paths.append(str(Path.home() / ".local" / "share" / "ember"
                         / "hive-session.json"))
        paths.append(str(Path(__file__).resolve().parent.parent
                         / "probes" / "hive-session.json"))
        seen: set[str] = set()
        unique = []
        for path in paths:
            if path and path not in seen:
                seen.add(path)
                unique.append(path)
        return unique

    def _poll_gas(self, errors: list[dict], tz) -> dict:
        result: dict[str, Any] = {
            "unit": self.settings.get("gas_unit", "auto"),
            "calorific": self.settings.get_float("calorific_value"),
            "today_m3": 0.0,
            "today_kwh": 0.0,
            # Money and rates start as None, not zero. A zero reads as
            # "nothing spent" and "no standing charge", which are both
            # claims rather than absences.
            "cost_today": None,
            "energy_p": None,
            "standing_p": None,
            "unit_rate_p": None,
            "hourly": [],
            "days": [],
            "provisional": False,
        }
        if self.provider is not None:
            # The demo supplies the rates and the unit; the totals are
            # worked out from the same history store the live path uses,
            # so the two behave identically.
            try:
                supplied = self.provider.gas()
                result.update({
                    "unit": supplied.get("unit", result["unit"]),
                    "calorific": supplied.get("calorific", result["calorific"]),
                    "unit_rate_p": supplied.get("unit_rate_p"),
                    "standing_p": supplied.get("standing_p"),
                    # The panel says how the unit was settled, so the
                    # numbers can be argued with.
                    "unit_reason": "the demo's meter reads cubic metres",
                    "unit_confidence": "the demo",
                })
                rates = {"unit_rate_p": supplied.get("unit_rate_p"),
                         "standing_charge_p": supplied.get("standing_p")}
                self._fill_gas(result, result["unit"], result["calorific"],
                               rates, tz)
                result["provisional"] = bool(supplied.get("provisional"))
                return result
            except Exception as exc:
                errors.append({"source": "demo", "message": str(exc)})
                return result

        api_key = self.settings.get("octopus_api_key", "")
        account = self.settings.get("octopus_account", "")
        if not api_key or not account:
            errors.append({"source": "octopus",
                           "message": "no Octopus API key or account number yet"})
            return result

        client = Octopus(api_key, account)
        mprn = self.settings.get("gas_mprn", "")
        serial = self.settings.get("gas_meter_serial", "")
        product = self.settings.get("gas_product_code", "")
        tariff = self.settings.get("gas_tariff_code", "")
        meter_type = self.settings.get("gas_meter_type", "")
        # A meter point can carry more than one meter: the old one and
        # the one that replaced it. Both have readings and both are
        # needed - the old meter's are the history, the new one's are
        # the present.
        serials = [item.strip() for item in
                   (self.settings.get("gas_meter_serials", "") or "").split(",")
                   if item.strip()]

        found: dict = {}
        try:
            if not (mprn and serial):
                mprn, serial, meter_type, serials = self._find_meter(client)
            # The account payload does not carry the tariff codes, so
            # this is where they come from - and it brings the meter's
            # own type with it.
            found = client.find_tariff(mprn)
            if found.get("meter_type"):
                meter_type = found["meter_type"]
            if found.get("serials") and not serials:
                serials = list(found["serials"])
            if not (product and tariff):
                product, tariff = client.current_gas_product()
        except OctopusError as exc:
            errors.append({"source": "octopus", "message": str(exc)})
            return result

        # The meter's own units field is a direct answer to the unit
        # question, if it has one.
        declared = declared_unit(meter_type, found.get("consumption_units", ""))

        if not serials and serial:
            serials = [serial]

        # Readings come half-hourly or hourly depending on the meter.
        # An hour of polling is plenty; the API has a rate limit.
        now = time.time()
        if now - self._last_gas_fetch > 900:
            self._last_gas_fetch = now
            # How far back to ask for.
            #
            # The store starts empty, and a chart of the week needs a
            # week. Octopus keeps about a month of readings, so the
            # first fetch asks for the lot. After that only the last
            # two days are new, and that is all the quick poll needs.
            # Once a day the whole month is fetched again: that fills
            # any gap the meter left and picks up readings Octopus
            # revised.
            days = 2
            if (self._gas_history_days() < 29
                    or now - self._last_gas_backfill > 86400):
                days = 30
                self._last_gas_backfill = now
            since = datetime.now(timezone.utc) - timedelta(days=days)
            until = datetime.now(timezone.utc)
            for one in serials:
                try:
                    readings = client.consumption(mprn, one, since, until)
                    self.history.record_gas(readings)
                except OctopusError as exc:
                    errors.append({"source": "octopus",
                                   "message": f"{one}: {exc}"})
                except Exception as exc:
                    # A shape nobody has seen before. The dashboard is a
                    # wall panel - it must not go blank because one
                    # reply was odd - so the poll carries on and the
                    # problem is reported instead.
                    errors.append({"source": "octopus",
                                   "message": f"{one}: {exc}"})
                # Rows the meter sent that Ember could not place in
                # time are left out of the history, and that is said
                # out loud: a shorter history the user knows about is
                # better than a longer one they do not.
                skipped = getattr(client, "skipped_readings", 0)
                if skipped:
                    errors.append({
                        "source": "octopus",
                        "message": (f"{skipped} readings from {one} could not "
                                    f"be read and were skipped"
                                    + (f" (for example '{client.skipped_sample}')"
                                       if getattr(client, "skipped_sample", "") else "")),
                    })

        try:
            rates = client.rates_now(product, tariff)
        except OctopusError as exc:
            errors.append({"source": "octopus", "message": str(exc)})
            rates = {}
        except Exception as exc:
            errors.append({"source": "octopus", "message": str(exc)})
            rates = {}

        unit = self.settings.get("gas_unit", "auto")
        calorific = self.settings.get_float("calorific_value")
        if unit == "auto":
            recent = self.history.gas_readings_between(now - 86400, now)
            detected = Octopus.detect_unit(recent)
            unit, calorific, reason, confidence = self._choose_unit(
                detected, meter_type, calorific, declared)
            result["unit_reason"] = reason
            result["unit_confidence"] = confidence
        elif unit == "kwh":
            # The meter already reports kWh, so there is nothing to
            # convert and the calorific value does not apply.
            calorific = 1.0

        self._fill_gas(result, unit, calorific, rates, tz)

        # A gap that is a meter exchange is not a fault, and saying so
        # is more useful than a blank with no explanation.
        newest = self.history.newest_gas_reading()
        exchange = self._exchange_note(found, newest)
        if exchange:
            result["meter_note"] = exchange
            detail = self._exchange_detail(found, newest)
            if detail:
                result["meter_note_detail"] = detail
        return result

    @staticmethod
    def _choose_unit(detected: dict, meter_type: str,
                     self_calorific: float,
                     declared: str = "") -> tuple[str, float, str, str]:
        """Settle which unit the readings are in.

        Octopus does not put the unit on the readings. The size of the
        numbers gives a good guess, but the meter's own record says what
        it is - a SMETS2 meter reads cubic metres and a SMETS1 meter reads
        kWh - and that beats a guess.
        """
        unit = detected.get("unit", "unknown")
        reason = detected.get("reason") or ""
        confidence = detected.get("confidence") or ""

        if not declared:
            declared = declared_unit(meter_type)

        if not declared:
            # No meter type, so the guess stands. A guess of kWh means
            # there is nothing to convert; anything else needs the
            # configured calorific value.
            if unit == "kwh":
                return unit, 1.0, reason, confidence
            if unit not in ("m3", "kwh"):
                # Neither the meter nor the readings say. Nothing is
                # converted, because there is nothing to convert from,
                # and the dashboard says the unit is unknown rather
                # than picking one.
                return ("unknown", 1.0,
                        "the meter record does not say what it measures, "
                        "and there are not enough readings to tell",
                        "no evidence")
            return unit, self_calorific, reason, confidence

        reads = "cubic metres" if declared == "m3" else "kWh"
        if declared != unit:
            reason = (f"the meter is {meter_type}, which reads {reads}, so that "
                      f"is what is used - the readings alone would have "
                      f"suggested {unit}")
        else:
            reason = (f"the meter is {meter_type}, which reads {reads} - and "
                      f"that agrees with the readings")

        # A meter that reads kWh needs no conversion. One that reads cubic
        # metres needs the configured calorific value.
        if declared == "kwh":
            return declared, 1.0, reason, "the meter type"
        return declared, self_calorific, reason, "the meter type"

    def _last_reported_day(self, unit: str, calorific: float,
                           unit_rate: float | None,
                           standing: float | None, tz) -> dict | None:
        """The most recent day the meter reported, and what it cost.

        Only used when today has not been reported, so that the panel
        can carry a real number rather than a row of dashes. It is
        always labelled as the last reported day - it is never shown
        as today's.
        """
        for entry in reversed(self.history.gas_by_day(30, tz)):
            value = entry.get("value") or 0.0
            if value <= 0:
                continue
            when = datetime.strptime(entry["date"], "%Y-%m-%d")
            cost = None
            if unit_rate is not None:
                energy = value * (calorific or 1.0) * unit_rate
                cost = round((energy + (standing or 0.0)) / 100.0, 2)
            kwh = None
            if unit == "m3":
                kwh = round(value * (calorific or 1.0), 1)
            elif unit == "kwh":
                kwh = round(value, 1)
            return {
                "date": entry["date"],
                "label": f"{when.day} {when.strftime('%B')}",
                "m3": round(value, 2) if unit == "m3" else None,
                "kwh": kwh,
                "cost": cost,
            }
        return None

    def _exchange_note(self, found: dict, newest: float | None) -> str:
        """Explain a gap that is a meter exchange rather than a fault.

        When the meter is replaced, the old one stops reporting and the
        new one can take a fortnight to start. The graph is then blank
        for a fortnight, and a blank with no explanation looks like a
        fault - so the exchange is named, with the date it happened.

        The clue is that the readings stop on the very day the old
        meter was retired.
        """
        if not newest:
            return ""
        meters = found.get("meters") or []
        live = found.get("live_serial") or ""
        newest_day = datetime.fromtimestamp(newest, tz=timezone.utc).date()
        for meter in meters:
            if not isinstance(meter, dict):
                continue
            serial = str(meter.get("serial") or "")
            if not serial or serial == live:
                continue
            end = _parse_moment(meter.get("active_to"))
            if end is None:
                continue
            if abs((newest_day - end.date()).days) > 1:
                continue
            return (f"the meter was replaced on {end.day} "
                    f"{end.strftime('%B %Y')}")
        return ""

    def _exchange_detail(self, found: dict, newest: float | None) -> str:
        """The longer version of the exchange note.

        It goes on the panel with room for it, so the Today panel is
        not pushed off the bottom of the screen by three lines of
        explanation.
        """
        if not self._exchange_note(found, newest):
            return ""
        return ("a new meter can take a fortnight to start reporting, and "
                "Octopus backfills the gap afterwards")

    @staticmethod
    def _find_meter(client: Octopus) -> tuple[str, str, str, list[str]]:
        """The MPRN, the meter serial, the meter type and every serial.

        The last one matters: when a meter is exchanged, the meter point
        keeps both meters, and the history lives under the old serial
        while the present lives under the new one.
        """
        agreements = client.gas_agreements()
        if not agreements:
            raise OctopusError(
                "no gas meter found on this account - set gas_mprn and "
                "gas_meter_serial in the settings, or run probes/probe_octopus.py")
        first = agreements[0]
        serials = first.get("serials") or ([first["serial"]] if first["serial"] else [])
        return first["mprn"], first["serial"], first.get("meter_type", ""), serials

    def _fill_gas(self, result: dict, unit: str, calorific: float,
                  rates: dict, tz) -> None:
        now = time.time()
        start, end = midnight(tz), midnight(tz) + timedelta(days=1)
        today_m3 = self.history.gas_total_between(start.timestamp(), end.timestamp())
        unit_rate = rates.get("unit_rate_p")
        standing = rates.get("standing_charge_p")

        # How far behind is the meter? Octopus does not deliver the
        # readings as they happen, so the newest reading may be hours
        # old - and a day with no readings at all is a day that has not
        # been reported yet, not a day with no gas in it.
        newest = self.history.newest_gas_reading()
        lag_hours = round((now - newest) / 3600.0, 1) if newest else None
        reported_today = bool(newest and newest >= start.timestamp())

        # The last day the meter actually reported. When today has no
        # reading - a new meter waiting to start - the panels would
        # otherwise be a row of dashes with nothing in them. This is
        # the most recent real number, labelled as what it is.
        if not reported_today:
            last = self._last_reported_day(unit, calorific, unit_rate,
                                            standing, tz)
            if last:
                result["last_reported"] = last

        result["unit"] = unit
        result["calorific"] = calorific
        # The primary unit is whatever the meter reports. A SMETS2 meter
        # reports cubic metres and a SMETS1 meter reports kWh, and the
        # dashboard has to say which it is looking at rather than assume.
        if unit == "kwh":
            result["unit_label"] = "kWh"
        elif unit == "m3":
            result["unit_label"] = "m³"
        else:
            # Nothing is known about the unit, so the dashboard does not
            # claim one. A confident "m³" with no evidence behind it is
            # the sort of thing that makes a bill wrong.
            result["unit_label"] = ""
            result["unit_unknown"] = True
        result["data_lag_hours"] = lag_hours
        result["today_reported"] = reported_today
        if reported_today:
            result["today_m3"] = round(today_m3, 3)
            if result.get("unit_unknown"):
                # There is no unit, so there is no kWh either. The
                # reading is what it is, and it is not converted.
                result["today_kwh"] = None
            else:
                result["today_kwh"] = round(today_m3 * calorific, 2)
        else:
            # Showing 0.00 for a day that has not been reported would be
            # a lie, and a dangerous one - it reads as "no gas used".
            result["today_m3"] = None
            result["today_kwh"] = None
            result["today_note"] = (
                "no reading for today yet"
                + (f" - {lag_hours:.0f} hours behind" if lag_hours is not None
                   else ""))
        result["unit_rate_p"] = unit_rate
        result["standing_p"] = (round(standing, 1) if standing is not None
                                 else None)

        if result.get("unit_unknown"):
            # Without a unit there is no energy in kWh, so there is no
            # cost. Publishing one would mean assuming the meter reads
            # kWh, which is the very thing the panel is refusing to do.
            #
            # This comes first, before the rate is considered: a zero
            # here would read as "no gas used" rather than "not known".
            result["cost_today"] = None
            result["cost_note"] = (
                "the unit is unknown, so the cost cannot be worked out")
            result["energy_p"] = None
        elif unit_rate is not None:
            energy_p = today_m3 * calorific * unit_rate
            result["energy_p"] = round(energy_p, 1) if reported_today else None
            if standing is not None:
                result["standing_p"] = round(standing, 1)
            if reported_today:
                result["cost_today"] = round(
                    (energy_p + (standing or 0.0)) / 100.0, 2)
            else:
                result["cost_today"] = None
        else:
            # No rate, so no cost. Zero would read as "nothing spent",
            # which is a different claim entirely.
            result["cost_today"] = None
            result["energy_p"] = None
            result["cost_note"] = "the tariff rate is not known yet"

        readings = self.history.gas_readings_between(now - 86400, now)
        hourly: list[dict] = []
        buckets: dict[int, float] = {}
        provisional = False
        for reading in readings:
            if reading.get("provisional"):
                provisional = True
            stamp = datetime.fromtimestamp(reading["ts"], timezone.utc).astimezone(tz)
            buckets.setdefault(stamp.hour, 0.0)
            buckets[stamp.hour] += reading["value"]
        for hour in range(24):
            hourly.append({
                "hour": hour,
                "m3": round(buckets.get(hour, 0.0), 3),
            })
        result["hourly"] = hourly
        # How much the window actually holds. Without this, a window
        # with no readings at all looks like 24 hours of zero - and
        # the peak hour reads as midnight, which is a claim.
        result["hourly_count"] = len(readings)
        result["provisional"] = provisional
        # The chart is a rolling 24 hours, so the axis has to say when
        # the window actually started - not pretend it is midnight.
        window_start = (datetime.fromtimestamp(now - 86400, timezone.utc)
                        .astimezone(tz))
        result["window_start"] = window_start.isoformat()

        # The comparison has to be like for like. At two in the afternoon,
        # today is compared with two in the afternoon on the other days -
        # not with their whole selves, which would always look bad.
        partial = self.history.gas_partial_by_day(8, tz)
        previous = [entry["value"] for entry in partial[:-1] if entry["value"] > 0]
        today_partial = partial[-1]["value"] if partial else today_m3
        if not reported_today:
            # Comparing a day that has not been reported against the
            # average would compare nothing with something.
            previous = []
            result["vs_average_note"] = "no reading today"
        if previous:
            same_time_average = sum(previous) / len(previous)
            result["same_time_average_m3"] = round(same_time_average, 3)
            if same_time_average > 0:
                percent = (today_partial - same_time_average) / same_time_average * 100.0
                result["vs_average_percent"] = round(percent, 1)
        else:
            result["same_time_average_m3"] = None
            result["vs_average_percent"] = None
            result.setdefault("vs_average_note", "not enough history yet")

        # The 7-day average is of complete days only.
        complete = self.history.gas_by_day(8, tz)[:-1]
        values = [entry["value"] for entry in complete if entry["value"] > 0]
        result["average_m3"] = round(sum(values) / len(values), 3) if values else None

        # The three views the chart can be.
        #
        # A week of days, a month of days, and the shape of a day. The
        # days come with a "complete" flag, because a day the meter has
        # not finished reporting is not a day with no gas in it.
        result["week"] = self.history.gas_by_day(7, tz)
        result["month"] = self.history.gas_by_day(30, tz)
        # The shape of a day: each hour averaged over the last week, so
        # the chart is never empty while there is any history.
        result["hourly_profile"] = self.history.gas_hourly_profile(7, tz)

        # What each day of the week cost.
        #
        # The rate in force *on that day*, not today's rate: a tariff
        # switch in the middle of a week would otherwise price the
        # days before it at the rate after it.
        result["days"] = self._day_costs(result["week"], rates, unit, tz)

    def _day_costs(self, week: list[dict], rates: dict, unit: str,
                   tz) -> list[dict]:
        """What each day of the week cost, at the rate of that day.

        The days the meter has not reported yet get no cost here - they
        are estimated further up, where the forecast model lives.
        """
        if unit == "kwh":
            # The meter already reports kWh, so there is nothing to
            # convert.
            calorific = 1.0
        elif unit == "unknown":
            # Without a unit there is no energy in kWh, so there is no
            # money. Publishing a cost would mean assuming the meter
            # reads one thing when it might read another.
            calorific = None
        else:
            calorific = self.settings.get_float("calorific_value") or 0.0
        unit_rows = rates.get("unit_rate_rows") or []
        standing_rows = rates.get("standing_charge_rows") or []
        fallback_rate = rates.get("unit_rate_p")
        fallback_standing = rates.get("standing_charge_p")

        days = []
        for entry in week:
            day = dict(entry)
            day["cost"] = None
            day["estimated"] = False
            day["status"] = "none"
            if calorific is None or not entry.get("complete"):
                # No unit, or the meter has not reported the whole of
                # this day. The rate is still worked out, so an estimate
                # for this day can be costed with it.
                #
                # "Complete" rather than "reported": a day that is half
                # read is not a cheaper day, and the week strip
                # compares whole days with whole days.
                moment = datetime.fromisoformat(entry["date"]).replace(
                    hour=12, tzinfo=tz)
                day["rate_p"] = rate_at(unit_rows, moment)
                if day["rate_p"] is None:
                    day["rate_p"] = fallback_rate
                day["standing_p"] = rate_at(standing_rows, moment)
                if day["standing_p"] is None:
                    day["standing_p"] = fallback_standing
                days.append(day)
                continue
            # The middle of the day, in local time, is the moment the
            # rate for that day is looked up at.
            moment = datetime.fromisoformat(entry["date"]).replace(
                hour=12, tzinfo=tz)
            rate = rate_at(unit_rows, moment)
            if rate is None:
                rate = fallback_rate
            standing = rate_at(standing_rows, moment)
            if standing is None:
                standing = fallback_standing
            if rate is None:
                days.append(day)
                continue
            energy = entry.get("value", 0.0) * calorific * rate
            day["cost"] = round((energy + (standing or 0.0)) / 100.0, 2)
            day["status"] = "measured"
            day["value"] = entry.get("value", 0.0)
            # The rate that was used, so an estimate for the same day
            # can be costed the same way.
            day["rate_p"] = rate
            day["standing_p"] = standing
            days.append(day)
        return days

    def _poll_weather(self, errors: list[dict]) -> dict:
        if self.provider is not None:
            try:
                return self.provider.weather()
            except Exception as exc:
                errors.append({"source": "demo", "message": str(exc)})
                return {}
        latitude = self.settings.get_float("latitude")
        longitude = self.settings.get_float("longitude")
        if not latitude or not longitude:
            postcode = self.settings.get("postcode", "")
            if not postcode:
                errors.append({"source": "weather",
                               "message": "no postcode or coordinates yet"})
                return {}
            try:
                place = weather_module.geocode(postcode)
                self.settings.set("latitude", place["latitude"])
                self.settings.set("longitude", place["longitude"])
                latitude = place["latitude"]
                longitude = place["longitude"]
            except weather_module.WeatherError as exc:
                errors.append({"source": "weather", "message": str(exc)})
                return {}
        try:
            return weather_module.fetch(latitude, longitude,
                                        self.settings.get("timezone", "Europe/London"))
        except weather_module.WeatherError as exc:
            errors.append({"source": "weather", "message": str(exc)})
            return {}

    # -- derived ----------------------------------------------------------

    def _derive(self, heating: dict, gas: dict, outside: dict, tz,
                errors: list[dict]) -> dict:
        anchor = midnight(tz)
        days = 21
        heat_days = self.history.heating_minutes_by_day(days, tz)
        gas_days = self.history.gas_by_day(days, tz)
        by_date = {entry["date"]: entry for entry in gas_days}

        combined = []
        for entry in heat_days:
            gas_entry = by_date.get(entry["date"], {})
            combined.append({
                "date": entry["date"],
                "label": entry["label"],
                # The meter's own reading, in the meter's own unit.
                "value": round(gas_entry.get("value", 0.0), 3),
                # Whether the meter has reported the whole of that day.
                "complete": gas_entry.get("complete", True),
                # Whether it reported anything at all for that day.
                "reported": gas_entry.get("reported", True),
                "heat_minutes": entry["minutes"],
            })
        week = combined[-7:]

        today_start = anchor.timestamp()
        today_rows = self.history.polls_between(today_start, time.time())
        heating_today = self.history._heating_from_rows(
            today_rows, today_start, time.time())
        week_start = (anchor - timedelta(days=6)).timestamp()
        week_rows = self.history.polls_between(week_start, time.time())
        heating_week = self.history._heating_from_rows(
            week_rows, week_start, time.time())

        # None, not zero, when nothing was recorded. A zero is a claim:
        # "the boiler never ran". The truth is "Ember was not watching".
        heating["heating_today_minutes"] = (round(heating_today, 1)
                                            if today_rows else None)
        heating["heating_week_minutes"] = (round(heating_week, 1)
                                           if week_rows else None)
        # How much of today and of the week Ember was watching.
        #
        # The heating minutes are counted from Ember's own polls, so a
        # period when Ember was not running is missing from the total -
        # and the total looks like a whole day's heating. Saying "at
        # least" is the honest version of that number.
        heating["heating_today_partial"] = bool(today_rows) and not all(
            entry.get("complete", True) for entry in heat_days[-1:])
        heating["heating_week_partial"] = bool(week_rows) and not all(
            entry.get("complete", True) for entry in heat_days[-7:])
        # What a day of heating usually costs in time.
        #
        # The last seven *whole* days. Today's figure is half a day, so
        # it is left out - comparing half a day with whole days would
        # always look cheap. A fixed window of finished days, so the
        # number does not wobble with the time of day.
        window = heat_days[-1 - AVERAGE_DAYS:-1]
        finished = [entry["minutes"] for entry in window
                    if entry["minutes"] > 0]
        heating["heating_daily_average_minutes"] = (
            round(sum(finished) / len(finished), 1) if finished else None)
        heating["heating_daily_average_days"] = len(finished)
        # The week the gas chart draws and the week the strip shows are
        # the same days, so the two can never disagree about what
        # happened. The chart's own list is the one that gets the
        # costs and the estimates.
        week = gas.get("week") or combined[-7:]
        costs = {day["date"]: day for day in (gas.get("days") or [])}
        for entry in week:
            day = costs.get(entry["date"]) or {}
            entry["cost"] = day.get("cost")
            entry["status"] = day.get("status", "none")
            entry["rate_p"] = day.get("rate_p")
            entry["standing_p"] = day.get("standing_p")
            entry["estimated"] = False
            entry["m3"] = entry.get("value", 0.0)

        heat_by_date = {entry["date"]: entry for entry in heat_days}
        heating["days"] = []
        for entry in week:
            heat = heat_by_date.get(entry["date"], {})
            heating["days"].append({**entry,
                                     "heat_minutes": heat.get("minutes", 0.0),
                                     # False means "Ember was not running
                                     # that day", which is not the same
                                     # as "the heating never came on".
                                     "heat_known": bool(heat.get("recorded")),
                                     # True means Ember did not watch the
                                     # whole day, so the minutes are a
                                     # lower bound rather than the day's
                                     # heating.
                                     "heat_partial": bool(heat.get("recorded"))
                                     and not heat.get("complete", True)})

        # The forecast needs degree-days, which need outside temperatures.
        # The temperature Ember recorded at its own polls is the better
        # figure - it is what was actually measured at the house.
        #
        # But a day Ember was not running for has no recorded figure,
        # and a missing one must not stand in for a real one: the
        # weather service's value for those days is used instead.
        # Otherwise the fit only ever covers the days since Ember was
        # installed, and says it needs more history while thirty days
        # of it are sitting right there.
        outside_by_date: dict[str, Any] = {}
        for entry in self.history.outside_temperatures_by_day(30, tz):
            mean = entry.get("mean_outside")
            if mean is not None:
                outside_by_date[entry["date"]] = mean
        for entry in outside.get("daily", []) or []:
            outside_by_date.setdefault(entry["date"], entry.get("mean"))

        base_temperature = self.settings.get_float("base_temperature")
        # Today is excluded: the day's gas is only half read, so a day
        # that is not finished would drag the whole line down.
        pairs = []
        for entry in combined:
            if entry["date"] == anchor.strftime("%Y-%m-%d"):
                continue
            # A day the meter has not finished reporting is not a day
            # with no gas in it. Octopus delivers readings with a lag, so
            # the most recent days can be missing or half read - and
            # putting a zero in the fit would drag the whole line down.
            if not entry.get("complete", True):
                continue
            mean = outside_by_date.get(entry["date"])
            if mean is None:
                continue
            dd = forecast_model.degree_days(mean, base_temperature)
            pairs.append((dd, entry["value"]))

        result: dict[str, Any] = {}
        try:
            model = forecast_model.fit(pairs, base_temperature)
            tomorrow_mean = weather_module.tomorrow_mean(outside.get("daily", []) or [],
                                                         self.settings.get("timezone"))
            if tomorrow_mean is None:
                tomorrow_mean = self._estimate_tomorrow(outside, tz)
            dd = forecast_model.degree_days(tomorrow_mean, base_temperature)
            predicted = model.predict(dd)
            band = model.band(dd)
            calorific = gas.get("calorific") or 1.0
            rate = gas.get("unit_rate_p")
            unit_unknown = bool(gas.get("unit_unknown"))

            # The same model, used for today instead of tomorrow.
            #
            # The meter is always a day or two behind, so "today" has
            # no measured gas in it. But today is not a mystery: the
            # model says what a day like today should use, from the
            # temperature and the fit. That is an *estimate*, so it is
            # published as one, with its band, and never as a reading.
            #
            # Today's degree-days have to be for the whole day. The
            # mean of the hours so far is not comparable with a full
            # day's mean, and using it would under-predict badly.
            today_mean = self._today_mean(outside, tz)
            estimate = None
            if today_mean is not None:
                today_dd = forecast_model.degree_days(today_mean,
                                                       base_temperature)
                estimate_m3 = model.predict(today_dd)
                estimate_band = model.band(today_dd)
                estimate = {
                    "m3": round(estimate_m3, 2),
                    "band": round(estimate_band, 2),
                    "kwh": (None if unit_unknown or gas.get("unit") != "m3"
                            else round(estimate_m3 * calorific, 1)),
                    "cost": (None if (unit_unknown or not rate)
                             else round((estimate_m3 * calorific * rate
                                         + (gas.get("standing_p") or 0.0))
                                        / 100.0, 2)),
                    "outside_mean": today_mean,
                    "degree_days": round(today_dd, 1),
                    "days_used": model.days_used,
                    "model": model.to_dict(),
                }
            # The gas panel shows the estimate *instead of* a reading
            # when the meter has not reported today. The week strip
            # wants it either way - a half-read day is not a cheaper
            # day - so it is worked out here and published only when it
            # is needed.
            result["today_estimate"] = (None if gas.get("today_reported")
                                         else estimate)

            # "vs average" needs something to compare, and on a lagging
            # meter there is no reading for today to compare.
            #
            # The estimate is a **whole day**, so it is compared with
            # the average of whole days - the same thing with the same
            # thing. Comparing it with the same-time partial average
            # would be comparing a whole day with half a day.
            if (estimate and not gas.get("today_reported")
                    and gas.get("average_m3")):
                average = gas["average_m3"]
                if average > 0:
                    percent = (estimate["m3"] - average) / average * 100.0
                    gas["vs_average_percent"] = round(percent, 1)
                    gas["vs_average_note"] = (
                        "estimated, against whole days")

            # The days the meter has not reported yet are estimated,
            # the same way today is.
            #
            # Octopus delivers gas a day or two late, so without this
            # the last two days of the week would be blank - and a
            # blank is not a number. The model already knows what a day
            # like that should use, from the temperature and the fit.
            #
            # Today is not worked out here: its estimate needs the
            # whole-day mean, not the mean of the hours so far, and
            # that was already done above.
            self._estimate_the_week(week, model, outside_by_date,
                                     base_temperature, gas, estimate,
                                     anchor.strftime("%Y-%m-%d"))
            # The chart and the strip show the same days, so the
            # estimate has to be on both.
            for entry in heating["days"]:
                source = next((day for day in week
                               if day["date"] == entry["date"]), None)
                if source:
                    entry["m3"] = source.get("m3")
                    entry["cost"] = source.get("cost")
                    entry["status"] = source.get("status")
                    entry["estimated"] = source.get("estimated")
                    entry["band"] = source.get("band")

            result["forecast"] = {
                "available": True,
                "tomorrow_m3": round(predicted, 2),
                # Without a unit there is no kWh and no money. Publishing
                # them would mean assuming the meter reads kWh, which is
                # the one thing the panel is refusing to do.
                "tomorrow_kwh": (None if unit_unknown
                                 else round(predicted * calorific, 1)),
                "tomorrow_cost": (None if (unit_unknown or not rate)
                                  else round((predicted * calorific * rate) / 100.0, 2)),
                "unit_unknown": unit_unknown,
                "outside_mean": tomorrow_mean,
                "degree_days": round(dd, 1),
                "base_temperature": base_temperature,
                "band": round(band, 2),
                "model": model.to_dict(),
            }
        except forecast_model.ForecastUnavailable as exc:
            result["forecast"] = {
                "available": False,
                "reason": str(exc),
                "base_temperature": base_temperature,
            }

        # The budget: metered gas, at the rate in force.
        month_start = anchor.replace(day=1).timestamp()
        month_m3 = self.history.gas_total_between(month_start, time.time())
        budget = self.settings.get_float("monthly_budget")
        rate = gas.get("unit_rate_p")
        spend = None
        if rate is not None and not gas.get("unit_unknown"):
            energy = month_m3 * (gas.get("calorific") or 1.0) * rate
            standing = (gas.get("standing_p") or 0.0)
            days_so_far = max(1, (time.time() - month_start) / 86400)
            spend = round((energy + standing * days_so_far) / 100.0, 2)
        result["budget"] = {
            "budget": budget,
            "month_m3": round(month_m3, 2),
            "spend": spend,
            "percent": (round((spend / budget * 100.0), 1)
                        if (spend is not None and budget) else None),
        }

        # The per-day costs were worked out on the way to here and are
        # not needed by the browser: the week strip carries them.
        gas.pop("days", None)
        for entry in heating.get("days") or []:
            entry.pop("rate_p", None)
            entry.pop("standing_p", None)
        return result

    def _estimate_the_week(self, week: list[dict], model, outside_by_date: dict,
                           base_temperature: float, gas: dict,
                           today_estimate: dict | None, today: str) -> None:
        """Fill in the days the meter has not reported yet.

        Each one gets the model's prediction for that day, with its
        band, and is marked as an estimate. A day that has been
        measured keeps its measured cost - an estimate never replaces
        a reading.
        """
        unit_unknown = bool(gas.get("unit_unknown"))
        calorific = gas.get("calorific") or 0.0
        for entry in week:
            if entry.get("status") == "measured":
                continue
            date = entry["date"]
            rate = entry.get("rate_p")
            standing = entry.get("standing_p") or 0.0
            if date == today:
                # Today's estimate needs the whole-day mean, which only
                # the weather service can give. It was worked out
                # already.
                if not today_estimate:
                    continue
                m3 = today_estimate.get("m3")
                band = today_estimate.get("band")
                cost = today_estimate.get("cost")
            else:
                mean = outside_by_date.get(date)
                if mean is None or rate is None or unit_unknown:
                    continue
                degree_days = forecast_model.degree_days(mean, base_temperature)
                m3 = model.predict(degree_days)
                band = model.band(degree_days)
                cost = round((m3 * calorific * rate + standing) / 100.0, 2)
            if m3 is None:
                continue
            entry["m3"] = round(m3, 2)
            entry["cost"] = cost
            entry["band"] = band
            entry["estimated"] = True
            entry["status"] = "estimated"

    def _today_mean(self, outside: dict, tz) -> float | None:
        """Today's mean temperature, for the whole day.

        The weather service's daily figure for today is part
        observation and part forecast, which is what is wanted: the
        degree-days for today have to be for the whole day, and the
        mean of the hours so far is not comparable with that.
        """
        today = midnight(tz).strftime("%Y-%m-%d")
        for entry in outside.get("daily", []) or []:
            if entry.get("date") == today:
                mean = entry.get("mean")
                if mean is not None:
                    return float(mean)
        return None

    @staticmethod
    def _estimate_tomorrow(outside: dict, tz) -> float | None:
        """Fall back to today's mean if tomorrow is not in the forecast yet."""
        daily = outside.get("daily") or []
        if not daily:
            return None
        today = midnight(tz).strftime("%Y-%m-%d")
        for entry in daily:
            if entry.get("date") == today:
                return entry.get("mean")
        return daily[-1].get("mean")

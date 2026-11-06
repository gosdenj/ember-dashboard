"""Settings: what Ember knows about the house and the accounts.

The settings file is the single source of truth for anything that could
change how a number is calculated - the gas calorific value, the base
temperature for degree-days, which meter to read. Secrets live here too,
in the same file, and it is written 0600 so only its owner can read it.

Every setting can be overridden by an environment variable, so the app can
be started with `EMBER_GAS_MPRN=... python3 run.py` without touching the
file. The environment wins.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path(
    os.environ.get("EMBER_DATA_DIR") or Path.home() / ".local" / "share" / "ember"
)

#: The nine radiator valves, in the order they are shown.
#: Full names, never truncated - the rooms have short names for a reason.
ROOMS: tuple[str, ...] = (
    "Dining Room",
    "Downstairs Hallway",
    "Kitchen",
    "Loft Room",
    "Lounge",
    "Master Bedroom",
    "Matteo Bedroom",
    "Sofia Bedroom",
    "Upstairs Hallway",
)

DEFAULTS: dict[str, Any] = {
    # -- Hive -------------------------------------------------------------
    "hive_email": "",
    "hive_password": "",
    "hive_base": "https://beekeeper-uk.hivehome.com/1.0",
    #: Written by probes/probe_hive.py, which can ask for the one-time SMS
    #: code that a background server cannot ask for.
    "hive_session_file": "",
    # -- Where we are -----------------------------------------------------
    "postcode": "",
    "latitude": 0.0,
    "longitude": 0.0,
    # -- Octopus ----------------------------------------------------------
    "octopus_api_key": "",
    "octopus_account": "",
    "gas_mprn": "",
    "gas_meter_serial": "",
    #: Every meter on the meter point, comma separated. A meter exchange
    #: leaves the old meter's history and the new meter's present on the
    #: same meter point, and both are needed.
    "gas_meter_serials": "",
    "gas_product_code": "",
    "gas_tariff_code": "",
    #: The meter's own type - SMETS2 reads cubic metres, SMETS1 reads kWh.
    #: Octopus does not put the unit on the readings, but the meter record
    #: carries its type, and that is better evidence than a guess.
    "gas_meter_type": "",
    #: SMETS2 meters report m3. Tariffs are priced in p/kWh, so a conversion
    #: is unavoidable - and this is the one number in Ember that has to come
    #: from the meter rather than from a default.
    "calorific_value": 11.16,
    "gas_unit": "auto",  # auto | m3 | kwh
    # -- Heating model ----------------------------------------------------
    "base_temperature": 16.0,
    "heating_minutes_threshold": 2.0,
    # -- Budget -----------------------------------------------------------
    "monthly_budget": 60.0,
    "budget_currency": "GBP",
    # -- Display ----------------------------------------------------------
    "theme": "dark",
    "clock_24h": True,
    "timezone": "Europe/London",
    "slides": ["heating", "gas", "today"],
    "rotation_seconds": 20,
    "panel_name": "Home",
    # -- System -----------------------------------------------------------
    "data_dir": "",
    "poll_seconds": 120,
    # How long to keep the history the dashboard is built on: the gas
    # readings and the heating polls. Four hundred days is more than a
    # year of gas - long enough for a year-on-year comparison.
    "history_days": 400,
    # The room log only records changes, so it needs far less room. A
    # month of it is plenty for "how long was this room on".
    "trv_days": 30,
    "demo": False,
}

#: Settings that must never be sent to the browser.
SECRET_KEYS = frozenset({"hive_password", "octopus_api_key"})

#: Environment variable name for a setting: EMBER_GAS_MPRN -> gas_mprn
_ENV_PREFIX = "EMBER_"


def _env_key(name: str) -> str:
    return _ENV_PREFIX + name.upper()


class Settings:
    """The settings, loaded once and kept in memory.

    Writes are atomic (write a temporary file, then rename) so a crash
    mid-write cannot leave a half-written settings file behind.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_DATA_DIR / "settings.json"
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self.load()

    # -- reading ----------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            values = dict(DEFAULTS)
            try:
                with open(self.path, encoding="utf-8") as handle:
                    stored = json.load(handle)
                if isinstance(stored, dict):
                    for key, value in stored.items():
                        if key in DEFAULTS:
                            values[key] = value
            except FileNotFoundError:
                pass
            except (OSError, json.JSONDecodeError):
                # A broken settings file must not stop the dashboard from
                # starting. The defaults are good enough to show something.
                pass
            self._values = values
            self._apply_env()

    def _apply_env(self) -> None:
        """Environment variables override the file."""
        for key in DEFAULTS:
            raw = os.environ.get(_env_key(key))
            if raw is None or raw == "":
                continue
            self._values[key] = self._coerce(key, raw)

    @staticmethod
    def _coerce(key: str, raw: Any) -> Any:
        """Turn a string from the environment or a JSON file into the right type."""
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, float):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default
        if isinstance(default, int):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default
        if isinstance(default, list):
            if isinstance(raw, list):
                return [str(item) for item in raw]
            return [part.strip() for part in str(raw).split(",") if part.strip()]
        return str(raw)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                return self._values[key]
            return DEFAULTS.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, 0))

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return float(DEFAULTS.get(key, 0.0))

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    # -- writing ----------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key not in DEFAULTS:
                raise KeyError(f"unknown setting: {key}")
            self._values[key] = self._coerce(key, value)

    def update(self, values: dict[str, Any]) -> None:
        """Set several settings at once.

        An unknown name is an error rather than something to swallow: a
        typo in a setting name should be reported, not silently ignored.
        """
        unknown = [key for key in values if key not in DEFAULTS]
        if unknown:
            raise KeyError(f"unknown setting: {', '.join(sorted(unknown))}")
        with self._lock:
            for key, value in values.items():
                self._values[key] = self._coerce(key, value)

    def save(self) -> None:
        """Write the settings out, keeping the file readable only by us."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            # Created 0600 from the start. The file holds the password
            # and the API key in plain text, and creating it with the
            # default permissions leaves it readable by everyone on the
            # machine for as long as it takes to write it.
            handle = os.fdopen(os.open(
                str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                "w", encoding="utf-8")
            with handle:
                json.dump(self._values, handle, indent=2, sort_keys=True)
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # -- for the browser --------------------------------------------------

    def public(self) -> dict[str, Any]:
        """Everything except the secrets.

        The browser gets this and nothing else - a password never leaves
        the process.
        """
        with self._lock:
            values = {
                key: value
                for key, value in self._values.items()
                if key not in SECRET_KEYS
            }
        values["has_hive_password"] = bool(self.get("hive_password"))
        values["has_octopus_key"] = bool(self.get("octopus_api_key"))
        values["rooms"] = list(ROOMS)
        return values

    # There used to be a `masked()` here, which returned the secrets as
    # eight asterisks. It was written for a settings page that did not
    # exist, and it has been removed on purpose: a masked secret is
    # still a value that looks like a value, and if anything ever posts
    # it back the real password is replaced by eight asterisks.
    # `public()` says only whether a secret is set, which cannot be
    # mistaken for the secret itself.

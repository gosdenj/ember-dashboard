"""Outside temperature, from Open-Meteo.

Open-Meteo needs no API key, which matters for something that has to
keep working on a Raspberry Pi without anyone renewing a subscription.

Postcodes are a different matter: Open-Meteo's geocoder knows towns and
cities, but it does not know British postcodes - not one of them, not
even SW1A 1AA. So a postcode goes to postcodes.io, the UK's own
directory, which also needs no key.

Ember records the outside temperature at every poll anyway, so the
history it uses is its own. The forecast API is asked for the past few
weeks as well, purely to seed the degree-day fit on day one - otherwise
the forecast could not start until Ember had been running for three
weeks.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"

# UK postcodes. The outward code is one to four characters and the
# inward code is always three, so the space goes three characters from
# the end: PO40PL is PO4 0PL.
POSTCODES_IO = "https://api.postcodes.io/postcodes/"
UK_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}$")


class WeatherError(Exception):
    """Anything that went wrong talking to Open-Meteo."""


def _get(url: str, params: dict[str, Any], what: str = "Open-Meteo") -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "ember/0.1",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise WeatherError(f"HTTP {exc.code} from {what}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WeatherError(f"could not reach {what}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise WeatherError(f"{what} sent something that was not JSON") from exc


def normalise_postcode(text: str) -> str:
    """Put a UK postcode into its canonical shape.

    People type PO40PL, po4 0pl and PO4 0PL for the same place, and
    half of them fail to be found. The inward code is always the last
    three characters, so the space goes just before them.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    if len(cleaned) > 3:
        candidate = cleaned[:-3] + " " + cleaned[-3:]
        # Only if it really is a postcode. "Poole" must not become
        # "PO OLE".
        if UK_POSTCODE.match(candidate):
            return candidate
    return cleaned


def looks_like_a_postcode(text: str) -> bool:
    return bool(UK_POSTCODE.match(normalise_postcode(text)))


def geocode(postcode: str) -> dict:
    """Turn a postcode into a latitude and longitude.

    Open-Meteo's geocoder knows towns and cities, but it does not know
    British postcodes - not one of them, not even SW1A 1AA. So a
    postcode goes to postcodes.io, which is the UK's own postcode
    directory, and only a place name falls back to Open-Meteo.
    """
    text = (postcode or "").strip()
    if looks_like_a_postcode(text):
        return _geocode_uk(text)
    payload = _get(GEOCODING, {"name": text, "count": 1, "language": "en"})
    results = payload.get("results") or []
    if not results:
        raise WeatherError(
            f"no place found for {text!r}. If that is a postcode, check it - "
            "a UK postcode is like PO4 0PL")
    first = results[0]
    return {
        "latitude": float(first["latitude"]),
        "longitude": float(first["longitude"]),
        "name": first.get("name", ""),
        "country": first.get("country", ""),
        "timezone": first.get("timezone", "Europe/London"),
    }


def _geocode_uk(postcode: str) -> dict:
    """Look a postcode up in the UK's own postcode directory."""
    canonical = normalise_postcode(postcode)
    url = POSTCODES_IO + urllib.parse.quote(canonical)
    try:
        payload = _get(url, {}, what="postcodes.io")
    except WeatherError as exc:
        # A postcode that is not there comes back as a 404, which is
        # not a failure of the service - it is an answer.
        if "HTTP 404" in str(exc):
            raise WeatherError(
                f"no such postcode: {canonical!r}. Check it against your "
                "postcode, or type the town instead") from exc
        raise
    if payload.get("status") != 200 or not payload.get("result"):
        raise WeatherError(f"no such postcode: {canonical!r}")
    found = payload["result"]
    try:
        latitude = float(found["latitude"])
        longitude = float(found["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError(
            f"the postcode directory sent something odd for {canonical!r}"
        ) from exc
    district = found.get("admin_district") or found.get("parish") or ""
    return {
        "latitude": latitude,
        "longitude": longitude,
        "name": district or canonical,
        "country": found.get("country") or "",
        "region": found.get("region") or "",
        "timezone": "Europe/London",
    }


# The WMO weather codes Open-Meteo publishes, and what each one is
# called. A forecast that says "4 degrees" but not "rain" is half a
# forecast; this is the other half.
WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear", "sun"),
    1: ("Mainly clear", "sun"),
    2: ("Partly cloudy", "cloud-sun"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm with hail", "storm"),
    99: ("Thunderstorm with hail", "storm"),
}


def describe(code: int | None) -> dict:
    """What a weather code means, in words and in a symbol.

    An unknown code is not an error and not a guess: it is "no idea",
    and the panel says so rather than drawing a sunny sky.
    """
    if code is None:
        return {"code": None, "label": "", "icon": "thermometer"}
    try:
        number = int(code)
    except (TypeError, ValueError):
        return {"code": None, "label": "", "icon": "thermometer"}
    label, icon = WEATHER_CODES.get(number, ("", "thermometer"))
    return {"code": number, "label": label, "icon": icon}


def summary(outside: dict, tz=None) -> dict:
    """Today and tomorrow, in the shape a forecast wants.

    Only two days. The panel has room for two, and a week of weather on
    a dashboard whose job is heating and gas is a week of noise.

    A day that is not there is not filled in with a guess: it comes
    back as None, and the panel leaves it blank.
    """
    tz = tz or timezone.utc
    daily = outside.get("daily") or [] if isinstance(outside, dict) else []
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    def day(date: str) -> dict:
        entry = None
        for candidate in daily:
            if candidate.get("date") == date:
                entry = candidate
                break
        if not entry:
            return {"date": date, "temp": None, "min": None, "max": None,
                    "code": None, "label": "", "icon": "thermometer"}
        described = describe(entry.get("code"))
        return {
            "date": date,
            "temp": entry.get("mean"),
            "min": entry.get("min"),
            "max": entry.get("max"),
            "code": described["code"],
            "label": described["label"],
            "icon": described["icon"],
        }

    current = None
    current_code = None
    if isinstance(outside, dict):
        current = outside.get("current")
        current_code = outside.get("current_code")
    described_current = describe(current_code)
    return {
        "current": {"temp": current, "code": described_current["code"],
                    "label": described_current["label"],
                    "icon": described_current["icon"]},
        "today": day(today),
        "tomorrow": day(tomorrow),
    }


def fetch(latitude: float, longitude: float, tz: str = "Europe/London",
          past_days: int = 30, forecast_days: int = 7) -> dict:
    """Hourly and daily temperatures, past and future.

    Returns:
        {"current": float|None, "daily": [{"date", "mean", "min", "max"}]}
    """
    payload = _get(FORECAST, {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min,weathercode",
        "current": "temperature_2m,weathercode",
        "timezone": tz,
        "past_days": past_days,
        "forecast_days": forecast_days,
    })

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("temperature_2m") or []

    # Group the hourly readings into local days and average them. The mean
    # of the day is what degree-days are built on.
    buckets: dict[str, list[float]] = {}
    for stamp, value in zip(times, values):
        if value is None:
            continue
        buckets.setdefault(stamp[:10], []).append(float(value))

    daily_meta = payload.get("daily") or {}
    daily_times = daily_meta.get("time") or []
    daily_max = daily_meta.get("temperature_2m_max") or []
    daily_min = daily_meta.get("temperature_2m_min") or []
    daily_codes = daily_meta.get("weathercode") or []

    daily = []
    for index, day in enumerate(daily_times):
        hours = buckets.get(day, [])
        mean = round(sum(hours) / len(hours), 2) if hours else None
        daily.append({
            "date": day,
            "mean": mean,
            "min": daily_max[index] if index < len(daily_max) else None,
            "max": daily_max[index] if index < len(daily_max) else None,
            "code": (daily_codes[index] if index < len(daily_codes)
                     else None),
        })
    # The min list is the same length as the max list.
    for index, entry in enumerate(daily):
        if index < len(daily_min) and daily_min[index] is not None:
            entry["min"] = daily_min[index]

    current = None
    current_code = None
    current_block = payload.get("current") or {}
    if isinstance(current_block.get("temperature_2m"), (int, float)):
        current = float(current_block["temperature_2m"])
    if isinstance(current_block.get("weathercode"), (int, float)):
        current_code = int(current_block["weathercode"])

    return {
        "current": current,
        "current_code": current_code,
        "daily": daily,
        "timezone": payload.get("timezone", tz),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def tomorrow_mean(daily: list[dict], tz: str = "Europe/London") -> float | None:
    """The forecast mean temperature for tomorrow, in the local timezone."""
    tomorrow = (datetime.now(timezone.utc).astimezone(
        _zone(tz)) + timedelta(days=1)).strftime("%Y-%m-%d")
    for entry in daily:
        if entry.get("date") == tomorrow:
            return entry.get("mean")
    return None


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc

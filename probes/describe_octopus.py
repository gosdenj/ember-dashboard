#!/usr/bin/env python3
"""Print the shape of the Octopus account, without dumping the whole thing.

This is the Octopus version of describe_hive.py. The account endpoint does
not look the way it is documented, and guessing has already cost one
crash and one false "you have no gas agreement".

It prints:

  * every top-level key, with its type and size
  * every property, with all of its keys
  * every gas meter point, with all of its keys
  * every meter on it, with all of its keys
  * one sample consumption row, with all of its keys
  * and a guess at where the agreements and the tariff are hiding

Nothing is written. It only reads.

Usage:
    OCTOPUS_API_KEY=sk_live_... python3 describe_octopus.py
    OCTOPUS_API_KEY=sk_live_... OCTOPUS_MPRN=1234567890 python3 describe_octopus.py

Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

#: Octopus has more than one hostname for the same API. Try them in order.
BASES = (
    "https://api.octopus.energy/v1",
    "https://api.oeus-kraken.energy/v1",
)

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = (os.environ.get("OCTOPUS_API_KEY") or "").strip()
MPRN = (os.environ.get("OCTOPUS_MPRN") or "").strip()


def api_key() -> str:
    if not KEY:
        raise SystemExit(
            "OCTOPUS_API_KEY is not set.\n"
            "OCTOPUS_API_KEY=sk_live_... python3 describe_octopus.py"
        )
    return KEY


def fetch(path: str, params: dict | None = None, base: str | None = None) -> dict:
    """GET something, and say exactly what came back."""
    bases = (base,) if base else BASES
    last = ""
    for candidate in bases:
        url = f"{candidate}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = base64.b64encode(f"{api_key()}:".encode()).decode()
        request = urllib.request.Request(url, headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "ember-describe/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"__not_json__": body[:400]}
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} from {candidate}"
        except urllib.error.URLError as exc:
            last = f"could not reach {candidate} ({exc.reason})"
    raise SystemExit(f"could not fetch {path}: {last}")


def describe(value, depth: int = 0) -> str:
    """A short description of a value, for one line of output."""
    if isinstance(value, dict):
        return f"dict ({len(value)} keys)"
    if isinstance(value, list):
        return f"list ({len(value)} items)"
    if isinstance(value, str):
        return f"str {value[:60]!r}"
    if value is None:
        return "null"
    return f"{type(value).__name__} {value}"


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("-" * len(title))


def keys_of(value) -> list[str]:
    return sorted(value.keys()) if isinstance(value, dict) else []


def main() -> int:
    account = (os.environ.get("OCTOPUS_ACCOUNT") or "").strip()
    if not account:
        account = find_account()

    section("Account")
    print(f"  account      {account}")

    payload = fetch(f"accounts/{account}/")

    section("TOP LEVEL")
    for key, value in payload.items():
        print(f"  {key:24s} {describe(value)}")

    section("EVERY KEY, ANYWHERE")
    # A flat list of every key that appears in the payload, with the
    # places it turned up. This is the fastest way to see where the
    # agreements and the tariff actually live.
    found: dict[str, list[str]] = {}
    walk(payload, "", found)
    for key in sorted(found):
        places = found[key]
        sample = describe(payload.get(key)) if key in payload else ""
        print(f"  {key:34s} {len(places):2d}x  {sample}")
        if len(places) <= 4:
            for place in places:
                print(f"      at {place}")

    section("PROPERTIES")
    properties = payload.get("properties")
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        properties = []
    print(f"  {len(properties)} propert(ies)")
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            print(f"  [{index}] not a dict: {describe(prop)}")
            continue
        print(f"  [{index}] keys: {', '.join(keys_of(prop))}")
        for key, value in prop.items():
            print(f"       {key:24s} {describe(value)}")

    section("GAS METER POINTS")
    mprns: list[str] = []
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            continue
        points = prop.get("gas_meter_points")
        if isinstance(points, dict):
            points = [points]
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            mprn = str(point.get("mprn") or point.get("meter_point") or "")
            if mprn:
                mprns.append(mprn)
            print(f"  mprn {mprn or '-'}")
            print(f"    keys: {', '.join(keys_of(point))}")
            for key, value in point.items():
                if key == "meters":
                    continue
                print(f"      {key:24s} {describe(value)}")
            meters = point.get("meters")
            if isinstance(meters, dict):
                meters = [meters]
            for meter in (meters or []):
                if not isinstance(meter, dict):
                    continue
                print(f"    meter {meter.get('serial_number') or '-'}")
                print(f"      keys: {', '.join(keys_of(meter))}")
                for key, value in meter.items():
                    print(f"        {key:24s} {describe(value)}")

    if not mprns and MPRN:
        mprns.append(MPRN)

    if MPRN:
        section(f"THE METER POINT {MPRN} ON ITS OWN")
        for path in (f"gas-meter-points/{MPRN}/",
                     f"accounts/{account}/gas-meter-points/{MPRN}/",
                     f"gas-meter-points/{MPRN}/meters/"):
            print(f"\n  GET {path}")
            try:
                detail = fetch(path)
            except SystemExit as exc:
                print(f"    {exc}")
                continue
            for key, value in detail.items():
                print(f"    {key:24s} {describe(value)}")
            if path.endswith("meters/"):
                for meter in detail.get("results") or []:
                    print(f"      meter {meter.get('serial_number') or '-'}")
                    for key, value in meter.items():
                        print(f"        {key:20s} {describe(value)}")

    if mprns:
        serial = ""
        for index, prop in enumerate(properties):
            if not isinstance(prop, dict):
                continue
            points = prop.get("gas_meter_points") or []
            for point in points:
                if isinstance(point, dict) and str(point.get("mprn")) == mprns[0]:
                    for meter in point.get("meters") or []:
                        if isinstance(meter, dict):
                            serial = meter.get("serial_number") or ""
        if not serial:
            section("METER SERIAL")
            try:
                meters = fetch(f"accounts/{account}/gas-meter-points/{mprns[0]}/meters/")
                for meter in meters.get("results") or []:
                    print(f"  {meter.get('serial_number')}  ({meter.get('meter_type')})")
                    for key, value in meter.items():
                        print(f"    {key:20s} {describe(value)}")
                    serial = meter.get("serial_number") or ""
            except SystemExit as exc:
                print(f"  {exc}")

        if serial:
            section("A SAMPLE OF THE READINGS")
            now = datetime.now(timezone.utc)
            start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                readings = fetch(
                    f"gas-meter-points/{mprns[0]}/meters/{serial}/consumption/",
                    {"period_from": start, "period_to": end, "order_by": "period",
                     "page_size": 100},
                )
            except SystemExit as exc:
                print(f"  {exc}")
                return 0
            results = readings.get("results") or []
            print(f"  {len(results)} readings")
            if results:
                print(f"  row keys: {', '.join(keys_of(results[0]))}")
                for key, value in results[0].items():
                    print(f"    {key:20s} {describe(value)}")
                values = []
                for row in results:
                    try:
                        values.append(float(row.get("consumption")))
                    except (TypeError, ValueError):
                        pass
                if values:
                    print(f"\n  values {min(values):.4f} .. {max(values):.4f}")
                    total = sum(values)
                    print(f"  total over {len(values)} readings: {total:.3f}")
                    if len(values) >= 48:
                        print(f"  daily average: {total / len(values) * 48:.3f}")
                    else:
                        print(f"  daily average (if 48 a day): {total / len(values) * 48:.3f}")

    print()
    return 0


def walk(value, path: str, found: dict[str, list[str]]) -> None:
    """Collect every key name and where it turned up."""
    if isinstance(value, dict):
        for key, item in value.items():
            places = found.setdefault(key, [])
            if len(places) < 6:
                places.append(path or "(top)")
            walk(item, f"{path}.{key}" if path else key, found)
    elif isinstance(value, list):
        for index, item in enumerate(value[:6]):
            walk(item, f"{path}[{index}]", found)


def find_account() -> str:
    """Ask GraphQL which accounts the key can see."""
    query = """
    mutation krakenTokenAuthentication($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) { token }
    }
    """
    body = json.dumps({"query": query, "variables": {"apiKey": api_key()}}).encode()
    request = urllib.request.Request(
        "https://api.octopus.energy/v1/graphql/",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    token = ""
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        token = ((payload.get("data") or {}).get("obtainKrakenToken") or {}).get("token") or ""
    except Exception:
        pass
    if not token:
        raise SystemExit("Could not find the account. Set OCTOPUS_ACCOUNT=A-XXXXXXXX.")

    query = "{ viewer { accounts { number } } }"
    for header in (token, f"JWT {token}", f"Token {token}"):
        body = json.dumps({"query": query, "variables": {}}).encode()
        request = urllib.request.Request(
            "https://api.octopus.energy/v1/graphql/",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": header},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        viewer = (payload.get("data") or {}).get("viewer") or {}
        numbers = [a.get("number") for a in (viewer.get("accounts") or [])
                   if isinstance(a, dict) and a.get("number")]
        if numbers:
            if len(numbers) == 1:
                return numbers[0]
            raise SystemExit("More than one account: " + ", ".join(numbers))
    raise SystemExit("Could not find the account. Set OCTOPUS_ACCOUNT=A-XXXXXXXX.")


if __name__ == "__main__":
    sys.exit(main())

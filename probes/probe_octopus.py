#!/usr/bin/env python3
"""Probe the Octopus Energy API for gas data.

Phase 0 of the Ember dashboard. This is a diagnostic, not part of the app:
it finds out what the account actually has, so the real code is written
against facts rather than guesses.

It answers, in one run:

  * which gas meter points and meters the account has
  * which product codes (tariffs) they have been on, and when
  * what unit the readings arrive in - m3 or kWh - and how often
  * whether the readings are rounded
  * how far behind the meter actually is
  * what the unit rate and standing charge are

Everything it finds is written to gas-profile.json, which is what the
dashboard's settings will be seeded from.

Usage:
    OCTOPUS_API_KEY=sk_live_... python3 probe_octopus.py

The account number is found for you if you leave it out. If the key can
see more than one account the probe lists them and asks you to pick.

    OCTOPUS_API_KEY=sk_live_... OCTOPUS_ACCOUNT=A-12345678 python3 probe_octopus.py

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

REST = "https://api.octopus.energy/v1"
GRAPHQL = "https://api.octopus.energy/v1/graphql/"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "gas-profile.json")

#: Set these to skip the account discovery entirely.
MPRN = (os.environ.get("OCTOPUS_MPRN") or "").strip()

# The app has the same job, so the probe uses the app's helpers rather
# than its own copy. A diagnostic that disagrees with the thing it is
# diagnosing is worse than no diagnostic.
sys.path.insert(0, os.path.dirname(HERE))
try:
    from ember.octopus import (_find_gas_agreements, _find_gas_meter_points,
                               pick_live_meter)
except ImportError:  # pragma: no cover - the probe can stand alone
    _find_gas_meter_points = None
    _find_gas_agreements = None

    def pick_live_meter(meters):
        """A stand-in, used only if the app is not importable."""
        usable = [m for m in meters if isinstance(m, dict)]
        return usable[-1] if usable else {}

try:
    from ember.octopus import Octopus as _Octopus
except ImportError:  # pragma: no cover
    _Octopus = None


def find_tariff(mprn: str) -> dict:
    """Ask the API where the product and tariff codes are."""
    if _Octopus is None:
        return {}
    client = _Octopus(_key(), _account())
    try:
        return client.find_tariff(mprn)
    except SystemExit:
        return {}
    except Exception:
        return {}

#: Set EMBER_DEBUG=1 to keep every reply the probe fetches. It saves a
#: paste-back when the shape turns out to be different again.
DEBUG_DIR = os.environ.get("EMBER_DEBUG") and os.path.join(HERE, "octopus-debug")

#: A UK home with gas heating burns roughly 10,000-30,000 kWh a year, and
#: a home using gas only for cooking burns about 3,000. Per day that is
#: somewhere between 8 and 82 kWh - which is between 0.7 and 7.4 m3.
#:
#: Those bands barely overlap, so the size of the numbers is a decent clue.
#: It is still only a clue, and the probe says so when it is guessing.
KWH_PER_DAY = (5.0, 150.0)


def _key() -> str:
    key = (os.environ.get("OCTOPUS_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "OCTOPUS_API_KEY is not set.\n"
            "Create a key at https://octopus.energy/your-account/developer/api-keys/\n"
            "then: OCTOPUS_API_KEY=sk_live_... python3 probe_octopus.py"
        )
    return key


# -- finding the account without being told it -------------------------

def _post_graphql(query: str, variables: dict, headers: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL,
        data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "ember-probe/1.0",
                 **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def graphql_token(key: str) -> str:
    """Swap the API key for a short-lived Kraken token.

    The REST API will not tell us which accounts the key can see, but the
    GraphQL API will. This is only used to find the account number - all
    the actual data comes from REST.
    """
    query = """
    mutation krakenTokenAuthentication($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) { token }
    }
    """
    try:
        payload = _post_graphql(query, {"apiKey": key}, {})
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return ""
    token = ((payload.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
    return token or ""


def discover_accounts(key: str) -> list[str]:
    """Ask the GraphQL API which account numbers this key can see."""
    token = graphql_token(key)
    if not token:
        return []
    query = "{ viewer { accounts { number } } }"
    # The header format is not spelled out in Octopus's own docs and the
    # community disagrees, so try the plausible ones.
    for header in (token, f"JWT {token}", f"Token {token}"):
        try:
            payload = _post_graphql(query, {}, {"Authorization": header})
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            continue
        viewer = (payload.get("data") or {}).get("viewer") or {}
        accounts = viewer.get("accounts") or []
        numbers = [a.get("number") for a in accounts if isinstance(a, dict) and a.get("number")]
        if numbers:
            return numbers
    return []


def _save_debug(name: str, body: str) -> None:
    """Keep the raw reply, if debugging is on."""
    if not DEBUG_DIR:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in name)
        with open(os.path.join(DEBUG_DIR, f"{safe}.json"), "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError:
        pass


def _properties_of(payload: dict) -> list[dict]:
    """The account's properties.

    The account endpoint returns 'properties' as a **list** of property
    objects, each holding its own electricity and gas meter points. An
    earlier version of this probe assumed a single object and fell over
    on the real reply.
    """
    properties = payload.get("properties")
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return []
    return [prop for prop in properties if isinstance(prop, dict)]


def _account() -> str:
    account = (os.environ.get("OCTOPUS_ACCOUNT") or "").strip()
    if account:
        return account

    print("OCTOPUS_ACCOUNT is not set, so asking Octopus which accounts the key can see...")
    numbers = discover_accounts(_key())
    if not numbers:
        raise SystemExit(
            "Could not work out the account number automatically.\n"
            "It is the A-XXXXXXXX number at the top of your Octopus bill, or\n"
            "in the top right of the Octopus dashboard:\n"
            "    OCTOPUS_ACCOUNT=A-12345678 python3 probe_octopus.py"
        )
    if len(numbers) == 1:
        print(f"  found one account: {numbers[0]}")
        return numbers[0]
    print(f"  the key can see {len(numbers)} accounts:")
    for number in numbers:
        print(f"    {number}")
    raise SystemExit(
        "More than one account, so pick one:\n"
        "    OCTOPUS_ACCOUNT=<the one> python3 probe_octopus.py"
    )


# -- the REST API -------------------------------------------------------

def get(path: str, params: dict | None = None, key: str | None = None) -> dict:
    """GET a Kraken v1 endpoint. Basic auth: the key is the username."""
    url = f"{REST}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    token = base64.b64encode(f"{(key or _key())}:".encode()).decode()
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "ember-probe/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8")
            _save_debug(path, body)
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"HTTP {exc.code} for {url}\n{detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach Octopus: {exc.reason}")


def get_optional(path: str, params: dict | None = None) -> tuple[dict | None, str]:
    """GET, but do not die if it fails. Returns (payload, note)."""
    try:
        return get(path, params), ""
    except SystemExit as exc:
        return None, str(exc).split("\n")[0]


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("-" * len(title))


# -- the unit question --------------------------------------------------

def guess_unit(daily_total: float) -> tuple[str, str]:
    """Work out whether the readings are m3 or kWh - and say how sure we are.

    Octopus does not tell you. The account payload says nothing about the
    unit the meter reports in, and neither does the meter point. So the
    only clue is the size of the numbers: a home with gas heating burns
    tens of kWh a day, which is a couple of cubic metres.

    This is a guess. It is printed with its reasoning so it can be
    checked, and it can be overridden with OCTOPUS_GAS_UNIT.
    """
    override = (os.environ.get("OCTOPUS_GAS_UNIT") or "").strip().lower()
    if override in ("m3", "m^3", "m³", "cubic"):
        return "m3", "set by OCTOPUS_GAS_UNIT"
    if override in ("kwh", "kilowatt"):
        return "kWh", "set by OCTOPUS_GAS_UNIT"

    if daily_total <= 0:
        return "m3", "no readings to go on - assuming m3"

    # If these are kWh, the daily total is the number of kWh the home used.
    # If they are m3, the daily total is cubic metres, and the kWh figure
    # is about eleven times bigger.
    as_kwh = daily_total
    as_m3 = daily_total * 11.2
    low, high = KWH_PER_DAY

    kwh_plausible = low <= as_kwh <= high
    m3_plausible = low <= as_m3 <= high

    if kwh_plausible and not m3_plausible:
        return "kWh", (f"a daily total of {as_kwh:.1f} is a normal amount of "
                       f"kWh for a home, and {as_m3:.0f} kWh would be far too much")
    if m3_plausible and not kwh_plausible:
        return "m3", (f"a daily total of {daily_total:.2f} m3 is about "
                      f"{as_m3:.0f} kWh - a normal amount. Read as kWh it would "
                      f"be {as_kwh:.1f}, which is far too little")
    if kwh_plausible and m3_plausible:
        return "m3", (f"a daily total of {daily_total:.2f} - both readings are "
                      f"within a normal range, so this one is a coin toss. "
                      f"Check the bill")
    return "m3", (f"a daily total of {daily_total:.2f} is below the range a "
                  f"home would use, so these are probably not kWh")


def main() -> int:
    key = _key()
    account = _account()
    now = datetime.now(timezone.utc)

    window = {"period_from": iso(now - timedelta(days=7)),
              "period_to": iso(now + timedelta(days=1))}

    rule("Account")
    print(f"  account      {account}")
    payload = get(f"accounts/{account}/")
    properties = _properties_of(payload)
    for index, prop in enumerate(properties):
        label = prop.get("name") or f"property {index}"
        print(f"  property      {label}  ({prop.get('region', '-')})")

    if not payload.get("gas_agreements"):
        print("\n  (the account payload has no 'gas_agreements' in it - the")
        print("   meter may be under the property instead, which is checked next)")

    rule("Gas agreements")
    gas = []
    graph_meters: list = []
    if _find_gas_meter_points is not None:
        points = _find_gas_meter_points(payload)
        found_agreements = _find_gas_agreements(payload)
    else:
        points, found_agreements = [], {}

    for point in points:
        mprn = point.get("mprn") or ""
        agreement = found_agreements.get(mprn) or {}
        agreed = point.get("agreed_product_codes") or []
        row = {
            "id": mprn,
            "meter_point": mprn,
            "product_code": (agreement.get("product_code")
                             or point.get("product_code")
                             or (agreed[0] if agreed else "")),
            "valid_from": agreement.get("valid_from") or "",
            "valid_to": agreement.get("valid_to") or point.get("valid_to") or "",
            "agreement_type": "",
            "tariff_code": agreement.get("tariff_code") or point.get("tariff_code") or "",
            "meters": [],
        }
        if not row["tariff_code"] and row["product_code"]:
            # A gas product code looks like VAR-21-01-01-A and the tariff
            # code is G-1R- followed by the last three parts.
            parts = row["product_code"].split("-")
            if len(parts) >= 3:
                row["tariff_code"] = "G-1R-" + "-".join(parts[-3:])
        gas.append(row)
        print(f"  {mprn or '-'}")
        print(f"    product      {row['product_code'] or '-'}")
        print(f"    valid        {row['valid_from'] or '-'} -> {row['valid_to'] or 'open'}")
        print(f"    tariff code  {row['tariff_code'] or '-'}")

    # The account payload did not carry a product code, so look for one
    # in the places that do.
    if not any(row["product_code"] for row in gas):
        print("\n  No product code on the account payload - asking the meter "
              "point and the GraphQL API...")
        for row in gas:
            if not row["meter_point"]:
                continue
            found = find_tariff(row["meter_point"])
            if not found:
                continue
            print(f"    found via {found.get('source', '?')}")
            row["product_code"] = found.get("product_code") or row["product_code"]
            row["tariff_code"] = found.get("tariff_code") or row["tariff_code"]
            if found.get("display_name"):
                print(f"    tariff        {found['display_name']}")
            if found.get("product_code"):
                print(f"    product       {found['product_code']}")
            if found.get("tariff_code"):
                print(f"    tariff code   {found['tariff_code']}")
            if found.get("standing_charge") is not None:
                print(f"    standing      {found['standing_charge']}p/day")
            if found.get("unit_rate") is not None:
                print(f"    unit rate     {found['unit_rate']}p/kWh")
            for meter in found.get("meters") or []:
                print(f"    meter {meter.get('serial')}")
                print(f"      type        {meter.get('meter_type') or '-'}")
                print(f"      units       {meter.get('consumption_units') or '-'}")
                print(f"      made        {meter.get('manufacturer') or '-'} "
                      f"{meter.get('model') or ''} {meter.get('year') or ''}".rstrip())
                print(f"      status      {meter.get('status') or '-'}")
                print(f"      active      {meter.get('active_from') or '-'} -> "
                      f"{meter.get('active_to') or 'open'}")
                print(f"      installed   {meter.get('installation_date') or '-'}")
            graph_meters.extend(found.get("meters") or [])

    rule("Gas meters")
    for prop in properties:
        for point in (prop.get("gas_meter_points") or []):
            if not isinstance(point, dict):
                continue
            mprn = str(point.get("mprn") or point.get("meter_point") or "")
            for meter in (point.get("meters") or []):
                if not isinstance(meter, dict):
                    continue
                serial = meter.get("serial_number") or meter.get("serialNumber") or ""
                kind = meter.get("meter_type") or meter.get("meterType") or ""
                record = {"serial": serial, "type": kind,
                          "active_from": (meter.get("active_from")
                                          or meter.get("activeFrom") or ""),
                          "active_to": (meter.get("active_to")
                                        or meter.get("activeTo") or ""),
                          "installation_date": (meter.get("installation_date")
                                                or meter.get("installationDate") or ""),
                          "consumption_units": (meter.get("consumption_units")
                                                or meter.get("consumptionUnits") or "")}
                for row in gas:
                    if row["meter_point"] == mprn:
                        row["meters"].append(record)
                print(f"  {mprn or '-'}  {serial}  ({kind or '-'})")
                if meter.get("manufacturer"):
                    print(f"      made by    {meter['manufacturer']} {meter.get('model') or ''}".rstrip())
                if kind:
                    print(f"      METER TYPE  {kind}"
                          + ("  -> reads cubic metres" if "SMETS2" in kind.upper()
                             else ("  -> reads kWh" if "SMETS1" in kind.upper() else "")))

    # A meter reference can be given directly, which skips all of this.
    if not gas and MPRN:
        print(f"  using the meter reference you gave: {MPRN}")
        gas.append({
            "id": MPRN, "meter_point": MPRN, "product_code": "",
            "valid_from": "", "valid_to": "", "agreement_type": "",
            "tariff_code": "", "meters": [],
        })

    # Fall back to the meter endpoint for anything the property did not
    # carry, so a meter is never missed.
    for row in gas:
        mprn = row["meter_point"]
        if not mprn or row["meters"]:
            continue
        meters, note = get_optional(
            f"accounts/{account}/gas-meter-points/{mprn}/meters/"
        )
        if not meters:
            print(f"  {mprn}: no meters ({note})")
            continue
        for meter in meters.get("results", []):
            serial = meter.get("serial_number") or meter.get("serialNumber") or ""
            kind = meter.get("meter_type") or meter.get("meterType") or ""
            row["meters"].append({"serial": serial, "type": kind,
                                  "active_from": (meter.get("active_from")
                                                  or meter.get("activeFrom") or ""),
                                  "active_to": (meter.get("active_to")
                                                or meter.get("activeTo") or ""),
                                  "installation_date": (meter.get("installation_date")
                                                        or meter.get("installationDate") or ""),
                                  "consumption_units": (meter.get("consumption_units")
                                                        or meter.get("consumptionUnits") or "")})
            print(f"  {mprn}  {serial}  ({kind or '-'})")

    rule("Consumption")
    series = []
    # A meter point can have more than one meter on it - the old one and
    # the one that replaced it - and each has its own history. Both are
    # needed: the old meter's readings are the history, the new meter's
    # are the present.
    per_serial = []
    for row in gas:
        for meter in row["meters"]:
            serial = meter["serial"]
            if not serial:
                continue
            params = {
                "period_from": iso(now - timedelta(days=30)),
                "period_to": iso(now),
                "order_by": "period",
                "page_size": 2000,
            }
            payload, note = get_optional(
                f"gas-meter-points/{mprn_of(row)}/meters/{serial}/consumption/", params
            )
            if not payload:
                print(f"  {serial}: no readings ({note})")
                per_serial.append({"serial": serial, "count": 0, "newest": "",
                                   "meters": []})
                continue
            results = payload.get("results") or []
            newest = max((str(item.get("interval_end", "")) for item in results),
                         default="")
            print(f"  {serial}: {len(results)} readings over 30 days"
                  + (f", newest {newest}" if newest else ""))
            per_serial.append({"serial": serial, "count": len(results),
                               "newest": newest, "results": results})
            for item in results:
                series.append(item)

    live = max((entry for entry in per_serial if entry["count"]),
               key=lambda entry: entry["newest"], default=None)
    if live:
        print(f"\n  live meter   {live['serial']}"
              f"  (newest reading {live['newest']})")
        profile_live = live["serial"]
    else:
        profile_live = ""

    stale = [entry for entry in per_serial
             if entry["count"] and live and entry["serial"] != live["serial"]]
    if stale and live:
        print("\n  This meter point has more than one meter on it:")
        for entry in stale:
            print(f"    {entry['serial']}  {entry['count']} readings, newest "
                  f"{entry['newest']}")
        print(f"    {live['serial']}  {live['count']} readings, newest {live['newest']}")
        print("  That is what a meter exchange looks like: the old meter's")
        print("  history stops, and the new one takes over. Ember reads both,")
        print("  so the history and the present are both complete.")

    if not series:
        print("\n  !! No gas readings came back at all.")
        print("     Check the meter serial, and whether the meter is on a")
        print("     SMETS2 smart meter that is actually reporting.")
        return 1

    rule("What the readings look like")
    values = []
    for item in series:
        try:
            values.append(float(item.get("consumption")))
        except (TypeError, ValueError):
            pass

    decimals = set()
    for value in values:
        text = f"{value:.6f}".rstrip("0")
        decimals.add(len(text.split(".")[1]) if "." in text else 0)
    precision = min(decimals) if decimals else 0
    print(f"  precision    {precision} decimal places")
    print(f"  values seen  {min(values):.4f} .. {max(values):.4f}")
    print(f"  readings     {len(values)}")

    whole = sum(1 for value in values if float(value).is_integer())
    print(f"  whole numbers {whole}/{len(values)}"
          f"{'  (rounded to whole units)' if whole == len(values) else ''}")

    gaps = {}
    for item in series:
        try:
            start = datetime.fromisoformat(str(item["interval_start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["interval_end"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        minutes = int((end - start).total_seconds() // 60)
        gaps[minutes] = gaps.get(minutes, 0) + 1
    if gaps:
        print(f"  interval     {', '.join(f'{m}m x{c}' for m, c in sorted(gaps.items()))}")

    latest = max((str(i.get("interval_end", "")) for i in series), default="")
    if latest:
        lag = now - datetime.fromisoformat(latest.replace("Z", "+00:00"))
        print(f"  newest       {latest}  ({lag.days}d {lag.seconds // 3600}h behind)")

    by_day = {}
    for item in series:
        try:
            day = str(item["interval_start"])[:10]
            by_day[day] = by_day.get(day, 0.0) + float(item.get("consumption") or 0)
        except (KeyError, TypeError, ValueError):
            continue

    daily_total = 0.0
    if by_day:
        print("\n  last 7 days with readings:")
        for day, total in sorted(by_day.items())[-7:]:
            print(f"    {day}  {total:.3f}")
        daily_total = sum(by_day.values()) / len(by_day)
        print(f"  daily average over {len(by_day)} days: {daily_total:.3f}")

    unit, why = guess_unit(daily_total)
    unit = unit.lower()          # the app compares against "m3" and "kwh"

    # The meter's own type is evidence, if it is there. A SMETS2 meter
    # reads cubic metres and a SMETS1 meter reads kWh, and the meter
    # record does carry its type even though the consumption endpoint
    # does not carry a unit.
    declared = ""
    meter_types = []
    live_meter = {}
    for row in gas:
        for meter in row["meters"]:
            kind = (meter.get("type") or "").upper()
            if kind and kind not in meter_types:
                meter_types.append(kind)
        # The meter in service, not merely the first one listed: after
        # a meter exchange the retired meter is still on the record,
        # with its own type and its own unit.
        live = pick_live_meter([{
            "serial": meter.get("serial", ""),
            "meter_type": meter.get("type", ""),
            "consumption_units": meter.get("consumption_units", ""),
            "active_from": meter.get("active_from", ""),
            "active_to": meter.get("active_to", ""),
            "installation_date": meter.get("installation_date", ""),
        } for meter in row["meters"]])
        if live:
            live_meter = live
            kind = (live.get("meter_type") or "").upper()
            if "SMETS2" in kind:
                declared = "m3"
            elif "SMETS1" in kind:
                declared = "kwh"
        if declared:
            break

    rule("The unit question")
    print("  Octopus does not say it in the readings. The account payload")
    print("  carries no unit, and the consumption endpoint returns only")
    print("  consumption, interval_start and interval_end. SMETS1 meters")
    print("  report kWh and SMETS2 meters report m3, and nothing in the")
    print("  reading tells the two apart.")
    print()
    print(f"  daily average {daily_total:.3f}")
    print(f"    reads as m3  -> {daily_total:.2f} m3 = about {daily_total * 11.2:.0f} kWh")
    print(f"    reads as kWh -> {daily_total:.2f} kWh")
    print()
    if declared:
        if declared == unit:
            print(f"  The meter is {declared == 'm3' and 'SMETS2' or 'SMETS1'}, which reads "
                  f"{declared == 'm3' and 'cubic metres' or 'kWh'}.")
            print("  That AGREES with what the numbers suggest.")
        else:
            print(f"  !! The meter is {declared == 'm3' and 'SMETS2' or 'SMETS1'}, which reads "
                  f"{declared == 'm3' and 'cubic metres' or 'kWh'} -")
            print(f"     but the numbers suggest {unit}. The meter type wins.")
        unit = declared
        why = "the meter's own type"
    print(f"\n  UNIT: {unit}")
    print(f"    ({why})")
    if unit == "m3":
        print("    To confirm: the number on your gas bill should be about")
        print(f"    {daily_total:.1f} m3 a day. If it is a much bigger number, it is kWh.")
        print("    Or override it: OCTOPUS_GAS_UNIT=kWh python3 probe_octopus.py")

    rule("Calorific value")
    if unit == "m3":
        print("  Tariffs are priced in p/kWh but the meter reports m3, so a")
        print("  calorific value is needed to turn one into the other:")
        print("      kWh = m3 x CV x 1.02264 / 3.6      (CV in MJ/m3)")
        print("  Your gas bill states it. It is normally between 39 and 40:")
        for cv in (39.0, 39.3, 39.5, 40.0):
            print(f"    CV {cv:.1f} MJ/m3  ->  {cv * 1.02264 / 3.6:.3f} kWh/m3")
        print("  Copy the exact number off the bill into the settings.")
        profile_cv = None
    else:
        print("  Not needed - the meter already reports kWh.")
        profile_cv = None

    rule("Tariff")
    profile = {
        "account_number": account,
        "checked_at": iso(now),
        "agreements": gas,
        "unit": unit,
        "unit_confidence": why,
        "meter_type": meter_types,
        "live_serial": profile_live,
        "all_serials": [entry["serial"] for entry in per_serial if entry["serial"]],
        "daily_average": round(daily_total, 4),
        "decimals": precision,
        "interval_minutes": min(gaps) if gaps else None,
        "latest_reading": latest,
        "calorific_mj_m3": profile_cv,
    }

    for row in gas:
        code = row["product_code"]
        if not code:
            continue
        print(f"\n  product {code}")
        product, note = get_optional(f"products/{code}/")
        if product:
            print(f"    display name   {product.get('display_name') or product.get('full_name') or '-'}")
            print(f"    unit           {product.get('unit') or '-'}")
            print(f"    active         {product.get('active')}")
            if product.get("standing_charge_inc_vat") is not None:
                print(f"    standing       {product['standing_charge_inc_vat']}p/day")
            profile["product_display_name"] = product.get("display_name") or product.get("full_name")
            profile["product_unit"] = product.get("unit")

        rates, note = None, ""
        # A gas product usually answers on the tariff-specific path.
        for path in (f"products/{code}/gas-tariffs/{row['tariff_code']}/standard-unit-rates/",
                     f"products/{code}/standard-unit-rates/"):
            if "{row['tariff_code']}" in path and not row["tariff_code"]:
                continue
            rates, note = get_optional(path, window)
            if rates and rates.get("results"):
                break
        if rates and rates.get("results"):
            for rate in rates["results"][-3:]:
                print(f"    rate           {rate.get('value_inc_vat')}p/kWh "
                      f"from {rate.get('valid_from')}")
            profile["unit_rate_p"] = rates["results"][-1].get("value_inc_vat")
        else:
            print(f"    rates          not found ({note})")

    charges, note = None, ""
    if gas[0].get("product_code"):
        # A gas product usually answers on the tariff-specific path.
        for path in (f"products/{gas[0]['product_code']}/gas-tariffs/{gas[0]['tariff_code']}/standing-charges/",
                     f"products/{gas[0]['product_code']}/standing-charges/"):
            if "{gas[0]['tariff_code']}" in path and not gas[0]["tariff_code"]:
                continue
            charges, note = get_optional(path, window)
            if charges and charges.get("results"):
                break
    else:
        note = "no product code"
    if charges and charges.get("results"):
        print(f"    standing charge {charges['results'][-1].get('value_inc_vat')}p/day")
        profile["standing_charge_p"] = charges["results"][-1].get("value_inc_vat")
    else:
        print(f"    standing charge not found ({note})")

    # Write the settings the dashboard needs, so wiring it up is one
    # copy rather than a dozen keystrokes.
    settings = {
        "octopus_api_key": key,
        "octopus_account": account,
        "gas_mprn": gas[0]["meter_point"] if gas else "",
        "gas_meter_serial": profile_live or (gas[0]["meters"][0]["serial"] if gas and gas[0]["meters"] else ""),
        "gas_meter_serials": ",".join(entry["serial"] for entry in per_serial if entry["serial"]),
        "gas_product_code": gas[0]["product_code"] if gas else "",
        "gas_tariff_code": gas[0]["tariff_code"] if gas else "",
        "gas_meter_type": ", ".join(meter_types),
        "gas_unit": unit,
    }
    settings_path = os.path.join(HERE, "ember-settings.json")
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.chmod(settings_path, 0o600)
    except OSError:
        pass
    print(f"\n  Settings written to {settings_path}")

    rule("Verdict")
    if unit == "m3":
        print("  The meter most likely reports CUBIC METRES (SMETS2).")
        print("  Ember will show m3 as the primary unit and kWh as derived,")
        print("  using the calorific value from the bill.")
    else:
        print("  The meter most likely reports kWh (SMETS1).")
        print("  Energy and cost follow directly, with no conversion.")
    print(f"\n  Profile written to {PROFILE}")

    with open(PROFILE, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


def mprn_of(row: dict) -> str:
    return row.get("meter_point") or ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        # A probe that dies with a traceback tells you nothing about the
        # account. This one says what it hit instead.
        print(f"\n\nThe probe hit an unexpected shape: {type(exc).__name__}: {exc}")
        print("Nothing has been written.")
        if DEBUG_DIR:
            print(f"Whatever was fetched before the problem is in {DEBUG_DIR}")
        raise SystemExit(1)

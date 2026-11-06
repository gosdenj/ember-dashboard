"""The Octopus gas client.

Octopus's REST v1 API (Kraken). No GraphQL, no client library, no
authentication beyond the API key - which travels as the username of a
Basic auth header with an empty password.

Two things about gas meters are worth writing down, because they catch
people out:

  * **SMETS2 meters report m3, not kWh.** SMETS1 meters report kWh.
    Tariffs are priced in p/kWh, so an m3 meter needs a conversion, and
    the number doing the converting is the calorific value - roughly
    11.0 to 11.2 kWh/m3 depending on the gas and the month. It is a
    setting, not a constant, and it comes from the meter rather than
    from a default.
  * **A day's total is a sum of readings, never a count of rows.** On a
    clock change a day is 23 or 25 hours long, and the API returns
    23 or 25 hourly rows to match.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

REST = "https://api.octopus.energy/v1"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

#: Used only to work out whether a meter reads m3 or kWh. The real value
#: comes from the meter and lives in the settings.
FALLBACK_CALORIFIC = 11.16

#: A UK home with gas heating burns roughly 10,000-30,000 kWh a year, and
#: a home using gas only for cooking burns about 3,000. Per day that is
#: somewhere between 8 and 82 kWh - which is between 0.7 and 7.4 m3.
KWH_PER_DAY = (5.0, 150.0)


def m3_to_kwh(m3: float, calorific_mj_m3: float) -> float:
    """Cubic metres of gas to kWh, the way Octopus does it.

    A gas bill states the calorific value in MJ/m3 (about 39 to 40).
    Octopus converts with::

        kWh = m3 x CV x 1.02264 / 3.6

    so a bill value of 39.5 MJ/m3 works out at 11.22 kWh/m3.
    """
    return m3 * calorific_mj_m3 * 1.02264 / 3.6


def kwh_per_m3(calorific_mj_m3: float) -> float:
    """The same conversion, expressed as kWh per cubic metre."""
    return calorific_mj_m3 * 1.02264 / 3.6


def _walk(value, path: str = ""):
    """Yield every dict in a payload, with the path it was found at."""
    if isinstance(value, dict):
        yield value, path
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _find_gas_meter_points(payload: dict) -> list[dict]:
    """Find every gas meter point in the account payload.

    The account endpoint has been seen with the gas meter points under
    `properties[].gas_meter_points[]`, and also under `gas_agreements`.
    Rather than guess, look for anything that carries an MPRN - that is
    what identifies a gas meter point - and pair it with the meter
    serial that sits beside it.
    """
    found: dict[str, dict] = {}

    for node, path in _walk(payload):
        # A meter point is called 'mprn' in one shape and 'meter_point'
        # in the other.
        mprn = node.get("mprn") or node.get("meter_point")
        if not mprn:
            continue
        mprn = str(mprn)
        if mprn in found:
            continue
        serial = ""
        meter_type = ""
        serials = []
        meters = node.get("meters")
        if isinstance(meters, dict):
            meters = [meters]
        described: list[dict] = []
        for meter in (meters or []):
            if not isinstance(meter, dict):
                continue
            one = meter.get("serial_number") or meter.get("serialNumber") or ""
            kind = meter.get("meter_type") or meter.get("meterType") or ""
            if one and one not in serials:
                serials.append(one)
            described.append({
                "serial": one,
                "meter_type": kind,
                "consumption_units": (meter.get("consumption_units")
                                      or meter.get("consumptionUnits") or ""),
                "active_from": (meter.get("active_from")
                                or meter.get("activeFrom") or ""),
                "active_to": meter.get("active_to") or meter.get("activeTo") or "",
                "installation_date": (meter.get("installation_date")
                                      or meter.get("installationDate") or ""),
            })
        # The meter in service, not merely the first one listed: after a
        # meter exchange the retired one is still on the record.
        live = pick_live_meter(described)
        live_serial = ""
        live_units = ""
        if live:
            serial = live.get("serial") or serial
            meter_type = live.get("meter_type") or ""
            live_serial = live.get("serial") or ""
            live_units = live.get("consumption_units") or ""
        agreed = node.get("agreed_product_codes")
        found[mprn] = {
            "mprn": mprn,
            "serial": serial,
            "serials": serials,
            "meter_type": meter_type,
            "consumption_units": live_units,
            "live_serial": live_serial,
            "agreed_product_codes": agreed if isinstance(agreed, list) else [],
            "product_code": node.get("product_code") or "",
            "tariff_code": node.get("tariff_code") or "",
            "valid_to": node.get("valid_to") or "",
            "path": path,
        }
    return list(found.values())


def _find_gas_agreements(payload: dict) -> dict:
    """Find the gas agreements, keyed by the meter point they belong to."""
    found: dict[str, dict] = {}

    for node, _path in _walk(payload):
        mprn = node.get("meter_point") or node.get("mprn")
        product = node.get("product_code")
        tariff = node.get("tariff_code")
        if not (mprn and (product or tariff)):
            continue
        mprn = str(mprn)
        if mprn in found:
            continue
        found[mprn] = {
            "product_code": product or "",
            "tariff_code": tariff or "",
            "valid_from": node.get("valid_from") or "",
            "valid_to": node.get("valid_to") or "",
            "agreements": [],
        }

    # The other shape: agreements as a list, each holding meter points
    # and a list of start and end dates.
    for node, _path in _walk(payload):
        points = node.get("meter_points")
        if not isinstance(points, list):
            points = node.get("gas_meter_points")
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            mprn = point.get("mprn")
            if not mprn:
                continue
            mprn = str(mprn)
            if mprn in found:
                continue
            agreed = point.get("agreed_product_codes")
            found[mprn] = {
                "product_code": (agreed[0] if isinstance(agreed, list) and agreed else
                                 point.get("product_code") or ""),
                "tariff_code": point.get("tariff_code") or "",
                "valid_from": "",
                "valid_to": point.get("valid_to") or "",
                "agreements": [
                    {"start": item.get("start_date", ""),
                     "end": item.get("end_date", "")}
                    for item in (node.get("agreements") or []) if isinstance(item, dict)
                ],
            }
    return found


def declared_unit(meter_type: str = "", consumption_units: str = "") -> str:
    """What the meter says it reads: "m3", "kwh" or "".

    A SMETS2 meter reads cubic metres and a SMETS1 meter reads kWh, and
    the meter record says which. The meter's own units field says it
    too. Either is better evidence than working it out from the size of
    the readings.
    """
    for text in (meter_type or "", consumption_units or ""):
        upper = text.strip().upper()
        if not upper:
            continue
        if "SMETS2" in upper:
            return "m3"
        if "SMETS1" in upper:
            return "kwh"
        if upper in ("M3", "M^3", "M³", "CUM", "CUMET", "CUBIC METRE",
                     "CUBIC METER", "CUBIC METRE"):
            return "m3"
        if upper in ("KWH", "KW H", "KILOWATT HOUR", "KILOWATT HOURS"):
            return "kwh"
    return ""


def _rate_value(row: dict) -> float | None:
    """The number on a rate row.

    The REST endpoints call it ``value_inc_vat``; GraphQL calls it
    ``unit_rate`` or ``standing_charge``. Reading only one of them is
    how the cost quietly comes out as nothing.
    """
    for key in ("value_inc_vat", "unit_rate", "standing_charge", "value"):
        if row.get(key) is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                return None
    return None


#: The timestamp shapes Octopus uses. GraphQL returns ISO strings with
#: a Z; the REST API returns the same, or a date on its own.
_MOMENT_PATTERNS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def _parse_moment(value: Any) -> datetime | None:
    """Read one of Octopus's timestamps, if there is one.

    Anything without a timezone is read as UTC, because that is what
    Octopus means by it. A timestamp that cannot be read is None rather
    than a guess - a wrong date here would pick the wrong meter.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        # "2026-11-03T00:00:00Z" -> "2026-11-03T00:00:00+00:00", and a
        # date on its own keeps its date rather than gaining a suffix
        # it cannot parse.
        text = text[:-1].rstrip()
        if "T" in text:
            text += "+00:00"
    for pattern in _MOMENT_PATTERNS:
        try:
            moment = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment
    return None


def pick_live_meter(meters: list[dict]) -> dict:
    """The meter that is in service now.

    A meter exchange leaves **two** meters on the same meter point: the
    one that was retired and the one that replaced it. The retired one
    stays on the record, keeps its own type, and keeps reading in the
    old unit. So the type of the first meter in the list is a coin
    toss - and getting it wrong means reading cubic metres as kilowatt
    hours, which is not a small error.

    So the meter is chosen by when it was in service, not by where it
    happens to sit in a list:

      1. the one whose active window covers today
      2. failing that, the most recently installed one
      3. failing that, the last one listed

    Each meter is a dict with any of: serial, meter_type,
    consumption_units, active_from, active_to, installation_date.
    """
    usable = [m for m in meters if isinstance(m, dict)]
    if not usable:
        return {}

    now = datetime.now(timezone.utc)

    def active_now(meter: dict) -> bool:
        start = _parse_moment(meter.get("active_from"))
        end = _parse_moment(meter.get("active_to"))
        if start and start > now:
            return False
        if end and end < now:
            return False
        return True

    def installed(meter: dict) -> datetime:
        return (_parse_moment(meter.get("installation_date"))
                or _parse_moment(meter.get("active_from"))
                or datetime.min.replace(tzinfo=timezone.utc))

    def newest(candidates: list[dict]) -> dict:
        # The later one wins a tie. `max` returns the *first* of equal
        # elements, and on a meter point with no dates at all that
        # would pick the oldest meter - which after an exchange is the
        # retired one.
        best = max(range(len(candidates)),
                   key=lambda i: (installed(candidates[i]), i))
        return candidates[best]

    live = [m for m in usable if active_now(m)]
    if live:
        return newest(live)
    return newest(usable)


def _tariff_from(detail: dict) -> dict:
    """Pull what the meter point says out of a meter point reply.

    The product and tariff codes are the point of this, but the meter
    point also knows **what the meter measures** - its type and its
    units. That is the best evidence there is for whether the readings
    are cubic metres or kWh, and it is here rather than only in the
    GraphQL reply, because the REST route is the one that answers
    first.
    """
    found = {"product_code": "", "tariff_code": "", "standing_charge": None,
             "display_name": "", "meter_type": "", "consumption_units": "",
             "serials": []}
    described: list[dict] = []
    for node, _path in _walk(detail):
        product = node.get("product_code") or node.get("productCode")
        tariff = node.get("tariff_code") or node.get("tariffCode")
        agreed = node.get("agreed_product_codes") or node.get("agreedProductCodes")
        if isinstance(agreed, list) and agreed and not product:
            product = agreed[0]
        if product and not found["product_code"]:
            found["product_code"] = str(product)
        if tariff and not found["tariff_code"]:
            found["tariff_code"] = str(tariff)
        if node.get("standing_charge") is not None and found["standing_charge"] is None:
            found["standing_charge"] = node.get("standing_charge")
        name = node.get("display_name") or node.get("displayName")
        if name and not found["display_name"]:
            found["display_name"] = str(name)
        for meter in node.get("meters") or []:
            if not isinstance(meter, dict):
                continue
            serial = str(meter.get("serial_number")
                         or meter.get("serialNumber")
                         or meter.get("serial") or "").strip()
            if serial and serial not in found["serials"]:
                found["serials"].append(serial)
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
        # The meter's own record wins over the meter point's, because
        # the meter point is the thing that changed in an exchange.
        live = pick_live_meter(described)
        if live and live.get("meter_type"):
            found["meter_type"] = live["meter_type"]
            found["consumption_units"] = live.get("consumption_units") or ""
        elif not found["meter_type"]:
            meter_type = node.get("meter_type") or node.get("meterType")
            if meter_type:
                found["meter_type"] = str(meter_type)
            units = node.get("consumption_units") or node.get("consumptionUnits")
            if units:
                found["consumption_units"] = str(units)
    return found


#: Verified against Kraken's live schema by introspection on 2026-11-04.
#:
#: For **gas**, the tariff is a plain object - ``GasTariffType`` - and not
#: a union, so no inline fragments are needed. (The electricity tariff is
#: a union, and guessing that gas works the same way produces a query the
#: server rejects.)
#:
#: The types are: ``AccountType.gasAgreements`` is a LIST of
#: ``GasAgreementType``; that has ``meterPoint: GasMeterPointType`` and
#: ``tariff: GasTariffType``.
_GAS_AGREEMENTS_QUERY = """
query EmberGasAgreements($account: String!) {
  account(accountNumber: $account) {
    number
    gasAgreements {
      id
      validFrom
      validTo
      meterPoint {
        mprn
        status
        meters {
          serialNumber
          meterType
          consumptionUnits
          manufacturerCode
          modelName
          manufacturedYear
          mechanism
          status
          activeFrom
          activeTo
          installationDate
        }
      }
      tariff {
        productCode
        tariffCode
        standingCharge
        preVatStandingCharge
        displayName
        fullName
        unitRate
      }
    }
  }
}
"""


def _tariff_from_graphql(api_key: str, account: str) -> dict:
    """Ask the GraphQL API for the gas agreements, meters and tariff.

    This is the route that definitely has the product code and the
    tariff code, because Kraken keeps them on the tariff type itself.
    It also has the meter's own type, which is what settles whether the
    readings are cubic metres or kWh.

    The reply can also come back with an authorisation error if the
    token has gone stale, so it is retried once with a fresh one.
    """
    found = {"product_code": "", "tariff_code": "", "standing_charge": None,
             "display_name": "", "mprn": "", "meter_type": "",
             "consumption_units": "", "unit_rate": None, "serials": [],
             "meters": [], "live_serial": ""}
    for attempt in (1, 2):
        token = _kraken_token(api_key)
        if not token:
            return found
        try:
            payload = _graphql(_GAS_AGREEMENTS_QUERY, {"account": account},
                                {"Authorization": token})
        except Exception:
            return found
        if _needs_a_new_token(payload):
            if attempt == 1:
                continue
            return found
        agreements = (((payload.get("data") or {}).get("account") or {})
                      .get("gasAgreements") or [])
        for agreement in agreements:
            if not isinstance(agreement, dict):
                continue
            tariff = agreement.get("tariff") or {}
            if isinstance(tariff, dict):
                if not found["product_code"]:
                    found["product_code"] = str(tariff.get("productCode") or "")
                if not found["tariff_code"]:
                    found["tariff_code"] = str(tariff.get("tariffCode") or "")
                if found["standing_charge"] is None:
                    found["standing_charge"] = tariff.get("standingCharge")
                if found["unit_rate"] is None:
                    found["unit_rate"] = tariff.get("unitRate")
                if not found["display_name"]:
                    found["display_name"] = str(tariff.get("displayName") or "")
            point = agreement.get("meterPoint") or {}
            if not isinstance(point, dict):
                continue
            if not found["mprn"]:
                found["mprn"] = str(point.get("mprn") or "")
            for meter in (point.get("meters") or []):
                if not isinstance(meter, dict):
                    continue
                serial = str(meter.get("serialNumber") or "")
                if serial and serial not in found["serials"]:
                    found["serials"].append(serial)
                found["meters"].append({
                    "serial": serial,
                    "meter_type": str(meter.get("meterType") or ""),
                    "consumption_units": str(meter.get("consumptionUnits") or ""),
                    "manufacturer": str(meter.get("manufacturerCode") or ""),
                    "model": str(meter.get("modelName") or ""),
                    "year": meter.get("manufacturedYear"),
                    "mechanism": meter.get("mechanism"),
                    "status": meter.get("status"),
                    "active_from": meter.get("activeFrom") or "",
                    "active_to": meter.get("activeTo") or "",
                    "installation_date": meter.get("installationDate") or "",
                })
            # The meter in service wins. After a meter exchange the
            # retired meter is still listed, with its own type and its
            # own unit - and the readings from the new meter are in a
            # different unit from the old ones.
            live = pick_live_meter(found["meters"])
            if live:
                found["meter_type"] = live.get("meter_type") or found["meter_type"]
                found["consumption_units"] = (live.get("consumption_units")
                                              or found["consumption_units"])
                found["live_serial"] = live.get("serial") or ""
        if found["product_code"] or found["mprn"]:
            break
    return found


def _needs_a_new_token(payload: dict) -> bool:
    """Has the token gone stale?"""
    for error in (payload.get("errors") or []):
        code = (error.get("extensions") or {}).get("errorCode")
        if code == "KT-CT-1112":
            return True
        if "Authorization" in str(error.get("message") or ""):
            return True
    return False


_TOKEN_QUERY = """
mutation KrakenToken($apiKey: String!) {
  obtainKrakenToken(input: { APIKey: $apiKey }) { token }
}
"""


def _graphql(query: str, variables: dict, headers: dict) -> dict:
    """POST a GraphQL query to Kraken."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "ember/0.1",
                 **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _kraken_token(api_key: str) -> str:
    """Swap the API key for a short-lived Kraken token."""
    try:
        payload = _graphql(_TOKEN_QUERY, {"apiKey": api_key}, {})
    except Exception:
        return ""
    token = ((payload.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
    return token or ""


def _reason_about_the_unit(daily_total: float) -> tuple[str, str]:
    """Work out whether a daily total is m3 or kWh, and say why.

    Shared by the app and the probe, so the dashboard and the diagnostic
    can never disagree about it.
    """
    if daily_total <= 0:
        return "m3", "no readings to go on"

    as_kwh = daily_total
    as_m3 = daily_total * 11.2
    low, high = KWH_PER_DAY

    kwh_plausible = low <= as_kwh <= high
    m3_plausible = low <= as_m3 <= high

    if kwh_plausible and not m3_plausible:
        return "kwh", (f"a daily total of {as_kwh:.1f} is a normal amount of kWh "
                       f"for a home, and {as_m3:.0f} kWh would be far too much")
    if m3_plausible and not kwh_plausible:
        return "m3", (f"a daily total of {daily_total:.2f} m3 is about {as_m3:.0f} kWh, "
                      f"which is normal. Read as kWh it would be {as_kwh:.1f}, far too little")
    if kwh_plausible and m3_plausible:
        return "m3", (f"a daily total of {daily_total:.2f} - both readings are within a "
                      f"normal range, so this one is a coin toss. Check the bill")
    return "m3", (f"a daily total of {daily_total:.2f} is below what a home would use, "
                  f"so these are probably not kWh")


class OctopusError(Exception):
    """Anything that went wrong talking to Octopus."""


def _rate_rows(results: list[dict]) -> list[dict]:
    """The rate rows, each with the window it was in force for."""
    rows = []
    for row in results or []:
        value = _rate_value(row)
        if value is None:
            continue
        rows.append({
            "valid_from": row.get("valid_from") or "",
            "valid_to": row.get("valid_to") or "",
            "value": value,
        })
    return rows


def rate_at(rows: list[dict], moment: datetime) -> float | None:
    """The rate in force at a moment, from a list of rate rows.

    A week of costs needs the rate that was in force on each day of
    that week. If the rows do not say, None is the answer - a guess
    would be a number nobody checked.
    """
    for row in rows or []:
        start = _parse_moment(row.get("valid_from")) if row.get("valid_from") else None
        end = _parse_moment(row.get("valid_to")) if row.get("valid_to") else None
        if start is not None and moment < start:
            continue
        if end is not None and moment >= end:
            continue
        return row.get("value")
    return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(moment: str) -> datetime | None:
    """Read one timestamp from a consumption row, or None.

    Octopus is not consistent about the timezone it writes. Some rows
    come back as "2026-11-03T00:00:00Z" and some as
    "2026-10-11T17:00:00+01:00" - the same moment, written the way
    that day's settlement was. Both are absolute, so both are read, and
    both become UTC. A timestamp that cannot be read at all is None
    rather than an exception: one unreadable row must not take the
    whole fetch down with it.
    """
    return _parse_moment(moment)


class Octopus:
    """A thin, honest wrapper over the endpoints Ember needs."""

    def __init__(self, api_key: str, account: str):
        self.api_key = (api_key or "").strip()
        self.account = (account or "").strip()
        if not self.api_key:
            raise OctopusError("no Octopus API key")
        if not self.account:
            raise OctopusError("no Octopus account number")
        # Rows the meter sent that Ember could not place in time. The
        # count is kept so the dashboard can say what it left out
        # instead of quietly showing a slightly shorter history.
        self.skipped_readings = 0
        self.skipped_sample = ""

    # -- transport --------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{REST}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        request = urllib.request.Request(url, headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "ember/0.1",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OctopusError(f"HTTP {exc.code} from Octopus: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OctopusError(f"could not reach Octopus: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise OctopusError("Octopus sent something that was not JSON") from exc

    # -- account ----------------------------------------------------------

    def accounts(self) -> list[str]:
        """Every account number this API key can see.

        The REST API will not say - only the GraphQL API knows which
        accounts a key is good for. That is what makes it possible to
        set Ember up from nothing but the key: the account falls out of
        the lookup rather than having to be typed in.
        """
        token = _kraken_token(self.api_key)
        if not token:
            return []
        query = "{ viewer { accounts { number } } }"
        # The header format is not spelled out in Octopus's own docs and
        # the community disagrees, so try the plausible ones.
        for header in (token, f"JWT {token}", f"Token {token}"):
            try:
                payload = _graphql(query, {}, {"Authorization": header})
            except Exception:
                continue
            viewer = (payload.get("data") or {}).get("viewer") or {}
            listed = viewer.get("accounts") or []
            numbers = [a.get("number") for a in listed
                       if isinstance(a, dict) and a.get("number")]
            if numbers:
                return numbers
        return []

    def account_details(self) -> dict:
        return self.get(f"accounts/{self.account}/")

    def gas_agreements(self) -> list[dict]:
        """Every gas meter on the account, flattened out of the account payload.

        The account endpoint has been seen in more than one shape. The
        gas meter point may sit under ``gas_agreements`` - as a dict
        keyed by agreement id, or as a list of agreements each holding
        ``meter_points`` - or under ``properties[].gas_meter_points[]``.
        Rather than bet on one, look for the pieces wherever they are.
        """
        payload = self.account_details()
        points = _find_gas_meter_points(payload)
        if not points:
            return []

        # The tariff lives on the agreement, and the agreement may sit
        # beside the meter point rather than inside it.
        agreements = _find_gas_agreements(payload)

        result = []
        for point in points:
            mprn = point.get("mprn") or ""
            serial = point.get("serial") or ""
            meter_type = point.get("meter_type") or ""
            serials = list(point.get("serials") or [])
            if serial and serial not in serials:
                serials.insert(0, serial)

            agreement = agreements.get(mprn) or {}
            product = (agreement.get("product_code")
                       or point.get("product_code")
                       or (point.get("agreed_product_codes") or [None])[0] or "")
            tariff = agreement.get("tariff_code") or point.get("tariff_code") or ""

            result.append({
                "mprn": mprn,
                "serial": serial,
                "serials": serials,
                "meter_type": meter_type,
                "consumption_units": point.get("consumption_units") or "",
                "live_serial": point.get("live_serial") or "",
                "products": [product] if product else [],
                "product_code": product,
                "tariff_code": tariff,
                "agreements": agreement.get("agreements") or [],
                "valid_from": agreement.get("valid_from") or "",
                "valid_to": agreement.get("valid_to") or point.get("valid_to") or "",
            })
        return result

    def current_gas_product(self) -> tuple[str, str]:
        """The product code and tariff code in force right now."""
        agreements = self.gas_agreements()
        if not agreements:
            raise OctopusError(
                "no gas meter found on this account - set gas_product_code and "
                "gas_tariff_code in the settings, or run probes/probe_octopus.py")
        first = agreements[0]
        codes = first.get("products") or []
        product = first.get("product_code") or (codes[0] if codes else "")
        tariff = first.get("tariff_code") or ""
        if not product or not tariff:
            found = self.find_tariff(first["mprn"])
            if found:
                product = found.get("product_code") or product
                tariff = found.get("tariff_code") or tariff
        if not product:
            raise OctopusError(
                "no gas product code found - set gas_product_code and "
                "gas_tariff_code in the settings, or run probes/probe_octopus.py")
        # The tariff code is G-1R- followed by the last three parts of the
        # product code, e.g. G-1R-AGILE-24-10-01-A.
        if not tariff:
            parts = product.split("-")
            tariff = "G-1R-" + "-".join(parts[-3:]) if len(parts) >= 3 else product
        return product, tariff

    # -- finding the tariff ----------------------------------------------

    def find_tariff(self, mprn: str) -> dict:
        """Find the product and tariff code for a gas meter.

        The account payload does not always carry them, so three routes
        are tried in turn:

          1. the gas meter point on its own
          2. the gas meter point under the account
          3. the GraphQL API, which keeps the agreements as the tariff
             type rather than as loose fields

        Whichever answers first wins.
        """
        for path in (f"gas-meter-points/{mprn}/",
                     f"accounts/{self.account}/gas-meter-points/{mprn}/"):
            try:
                detail = self.get(path)
            except OctopusError:
                continue
            found = _tariff_from(detail)
            if found.get("product_code") or found.get("tariff_code"):
                found["source"] = path
                return found
        found = _tariff_from_graphql(self.api_key, self.account)
        if found:
            found["source"] = "the GraphQL API"
        return found

    # -- consumption ------------------------------------------------------

    def consumption(self, mprn: str, serial: str, start: datetime, end: datetime,
                    group_by: str = "hour", page_size: int = 5000) -> list[dict]:
        """Gas consumption between two moments.

        group_by may be 'half-hour', 'hour', 'day', 'week' or 'month'.
        Returns a list of {ts, m3, kwh, provisional}.
        """
        payload = self.get(
            f"gas-meter-points/{mprn}/meters/{serial}/consumption/",
            {
                "period_from": _iso(start),
                "period_to": _iso(end),
                "order_by": "period",
                "page_size": page_size,
                "group_by": group_by,
            },
        )
        results = payload.get("results") or []
        readings = []
        skipped = 0
        sample = ""
        for item in results:
            try:
                reading = self._to_reading(item)
            except Exception:
                # A row in a shape nobody has seen before. Skip it and
                # keep the rest: the alternative is losing the month's
                # readings because of one bad line.
                reading = None
            if reading is None:
                skipped += 1
                if not sample and isinstance(item, dict):
                    sample = str(item.get("interval_start", ""))[:40]
                continue
            readings.append(reading)
        self.skipped_readings = skipped
        self.skipped_sample = sample
        return readings

    @staticmethod
    def _to_reading(item: dict) -> dict | None:
        """Normalise one consumption row, or None if it cannot be read.

        The consumption endpoint returns three fields and no more:
        'consumption', 'interval_start' and 'interval_end'. There is no
        second number in kWh to compare against, so the unit of
        'consumption' is whatever the meter reports - kWh on a SMETS1
        meter, cubic metres on a SMETS2 one - and nothing in the reply
        says which.

        A row whose moment cannot be read is None rather than a reading
        stamped at the epoch. A reading at 1970 is not an absence, it
        is a lie, and it would sit in the history table forever.
        """
        if not isinstance(item, dict):
            return None
        raw = item.get("consumption")
        interval_start = item.get("interval_start", "")
        interval_end = item.get("interval_end", "")
        start = _parse(interval_start) if interval_start else None
        if start is None:
            return None
        end = _parse(interval_end) if interval_end else None
        provisional = bool(item.get("is_estimated")) or bool(item.get("provisional"))
        return {
            "ts": start.timestamp() if start else 0.0,
            "interval_start": interval_start,
            "interval_end": interval_end,
            # The meter's own reading, in the meter's own unit. It is
            # called 'value' because a SMETS1 meter reports kWh and a
            # SMETS2 meter reports cubic metres, and the row cannot
            # claim to be one or the other.
            "value": float(raw) if raw is not None else None,
            "consumption": float(raw) if raw is not None else None,
            "provisional": provisional,
        }

    # -- tariffs ----------------------------------------------------------

    def unit_rate(self, product: str, tariff: str,
                  start: datetime | None = None) -> list[dict]:
        """The unit rates in force, newest first."""
        params = {}
        if start:
            params["period_from"] = _iso(start)
        payload = self.get(
            f"products/{product}/gas-tariffs/{tariff}/standard-unit-rates/", params)
        return payload.get("results") or []

    def standing_charge(self, product: str, tariff: str,
                        start: datetime | None = None) -> list[dict]:
        params = {}
        if start:
            params["period_from"] = _iso(start)
        payload = self.get(
            f"products/{product}/gas-tariffs/{tariff}/standing-charges/", params)
        return payload.get("results") or []

    def rates_now(self, product: str, tariff: str) -> dict:
        """The rate in force right now, in pence per kWh and pence per day.

        A gas product does not always answer on the product-wide
        endpoints, so the tariff-specific ones are tried first and the
        product-wide ones second. If neither answers, GraphQL has the
        standing charge on the tariff itself.
        """
        now = datetime.now(timezone.utc)
        window = {"period_from": _iso(now - timedelta(days=7)),
                  "period_to": _iso(now + timedelta(days=1))}

        unit_p = None
        unit_raw = None
        unit_results: list[dict] = []
        for path in (f"products/{product}/gas-tariffs/{tariff}/standard-unit-rates/",
                     f"products/{product}/standard-unit-rates/"):
            results = self._try(path, window)
            if results:
                unit_results = results
                unit_raw = results[-1]
                unit_p = _rate_value(unit_raw)
                break

        standing_p = None
        standing_raw = None
        standing_results: list[dict] = []
        for path in (f"products/{product}/gas-tariffs/{tariff}/standing-charges/",
                     f"products/{product}/standing-charges/"):
            results = self._try(path, window)
            if results:
                standing_results = results
                standing_raw = results[-1]
                standing_p = _rate_value(standing_raw)
                break

        if unit_p is None or standing_p is None:
            found = _tariff_from_graphql(self.api_key, self.account)
            if unit_p is None:
                unit_p = found.get("unit_rate")
            if standing_p is None:
                standing_p = found.get("standing_charge")

        return {"unit_rate_p": unit_p, "standing_charge_p": standing_p,
                "unit_rate_raw": unit_raw,
                "standing_charge_raw": standing_raw,
                # The rows themselves, with the window each one covers.
                # A week of costs needs the rate that was in force on
                # each day of that week, not just the rate today.
                "unit_rate_rows": _rate_rows(unit_results),
                "standing_charge_rows": _rate_rows(standing_results)}

    def _try(self, path: str, params: dict) -> list[dict]:
        """GET a rates endpoint, and come back empty-handed if it says no."""
        try:
            payload = self.get(path, params)
        except OctopusError:
            return []
        return payload.get("results") or []

    # -- working out the unit --------------------------------------------

    @staticmethod
    def detect_unit(readings: Iterable[dict]) -> dict:
        """Work out whether the meter reads m3 or kWh.

        Octopus does not say. The account payload carries no unit, the
        meter point carries no unit, and the consumption endpoint returns
        three fields - consumption, interval_start and interval_end - and
        nothing else. Octopus's own documentation puts it plainly: SMETS1
        meters have their readings pre-converted to kWh, SMETS2 meters
        report cubic metres, and there is no field that tells the two
        apart.

        So the only clue is the size of the numbers. A home with gas
        heating burns tens of kWh a day, which is a couple of cubic
        metres, and those two ranges barely overlap.

        The answer is therefore a guess, and it is returned with a
        confidence and the reasoning behind it rather than dressed up as
        a fact. An explicit setting always wins over the guess.

        The readings may come straight from the API ('consumption' plus
        interval times) or out of the history store. The history store
        calls it 'value', because the reading is in the meter's own unit
        and calling it 'm3' would be an assumption - the very thing
        this function exists to avoid.
        """
        total = 0.0
        count = 0
        stamps: list[float] = []

        for reading in readings:
            value = reading.get("consumption")
            if value is None:
                value = reading.get("value")
            if value is None:
                value = reading.get("m3")
            if value is None:
                continue
            try:
                total += float(value)
            except (TypeError, ValueError):
                continue
            count += 1
            stamp = reading.get("ts")
            if stamp is None and reading.get("interval_start"):
                parsed = _parse(reading["interval_start"])
                stamp = parsed.timestamp() if parsed else None
            if stamp:
                stamps.append(float(stamp))

        if count == 0 or total <= 0:
            return {"unit": "unknown", "calorific": None, "confidence": "no data"}

        # Work out the daily total from how much time the readings
        # actually cover, not from how many there happen to be.
        #
        # The span between the first and last timestamp is not the
        # covered duration: 48 half-hourly readings start at 00:00 and
        # the last one starts at 23:30, so the timestamps span 23.5
        # hours but the readings cover a full 24. The covered duration
        # is the number of intervals times the length of each one.
        daily = total
        span_note = ""
        if len(stamps) >= 2:
            span = max(stamps) - min(stamps)
            if span > 0:
                interval = span / (len(stamps) - 1)
                covered = span + interval
                span_days = covered / 86400.0
                if span_days >= 0.25:
                    daily = total / span_days
                    span_note = f" over {span_days:.1f} days of readings"
        elif count >= 24:
            daily = total / count * 48
        else:
            return {"unit": "unknown", "calorific": None,
                    "confidence": "not enough data",
                    "reason": f"only {count} readings, which is not enough to "
                              f"work out a daily total"}

        unit, why = _reason_about_the_unit(daily)
        why = why + span_note
        if unit == "kwh":
            return {"unit": "kwh", "calorific": 1.0, "confidence": "medium",
                    "reason": why, "daily_total": round(daily, 4)}
        return {"unit": "m3", "calorific": None, "confidence": "medium",
                "reason": why, "daily_total": round(daily, 4)}

#!/usr/bin/env python3
"""Ember - a self-hosted dashboard for Hive heating and Octopus gas.

    python3 run.py                 # the real thing
    python3 run.py --demo          # plausible data, no account needed

Standard library only. No pip install, anywhere.

The first run has nothing configured, so it will start in demo mode and
say so on every screen. To point it at the real accounts, run the two
probes once and then set the settings:

    cd probes
    HIVE_USER=... HIVE_PASS=... python3 probe_hive.py
    OCTOPUS_API_KEY=... OCTOPUS_ACCOUNT=A-... python3 probe_octopus.py

The Hive probe asks for the one-time SMS code a server cannot ask for,
and writes a session file that Ember then refreshes silently.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ember import __version__  # noqa: E402
from ember.config import DEFAULT_DATA_DIR, Settings  # noqa: E402
from ember.dashboard import Dashboard  # noqa: E402
from ember.demo import DemoProvider  # noqa: E402
from ember.history import History  # noqa: E402
from ember.server import serve  # noqa: E402


def _env_port(name: str, fallback: int) -> int:
    """A port from the environment, if it is one.

    The environment is how a container is told where to listen, so a
    typo in it should fall back to the default rather than stop the
    dashboard from starting.
    """
    try:
        return int(os.environ.get(name, "") or fallback)
    except (TypeError, ValueError):
        return fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ember")
    parser.add_argument("--host",
                        default=os.environ.get("EMBER_HOST", "127.0.0.1"),
                        help="what to bind to. 0.0.0.0 to reach it from "
                             "another machine - the settings page holds "
                             "your credentials, so only do that on a "
                             "network you trust")
    parser.add_argument("--port", type=int,
                        default=_env_port("EMBER_PORT", 8766))
    parser.add_argument("--demo", action="store_true",
                        help="use generated data instead of the real accounts")
    parser.add_argument("--data-dir",
                        default=os.environ.get("EMBER_DATA_DIR",
                                               str(DEFAULT_DATA_DIR)),
                        help="where the settings and the history live")
    parser.add_argument("--settings", default="",
                        help="path to the settings file")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="check the accounts and print what is wrong, "
                             "then exit without starting the server")
    parser.add_argument("--version", action="version",
                        version=f"Ember {__version__}")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser()
    settings_path = Path(args.settings).expanduser() if args.settings else (
        data_dir / "settings.json")

    settings = Settings(settings_path)
    settings.set("data_dir", str(data_dir))

    # A sensible default for the session file the probe writes.
    if not settings.get("hive_session_file"):
        settings.set("hive_session_file", str(data_dir / "hive-session.json"))

    demo = args.demo or settings.get_bool("demo")
    settings.set("demo", demo)

    history = History(data_dir / "history.db")
    provider = None
    if demo:
        provider = DemoProvider()
        provider.seed(history)

    dashboard = Dashboard(settings, history, provider)

    # The first poll is what finds out whether the accounts work. If it
    # fails, the server still starts: a wall panel that comes up saying
    # "the gas could not be read" is useful, and one that refuses to
    # start is not - it cannot even be told what went wrong.
    try:
        dashboard.poll()
    except Exception as exc:
        print(f"  warning: the first poll failed - {exc}")
        print("           the dashboard will start anyway; the problem is")
        print("           shown on the panel and in the settings page.")

    if args.check:
        return check(settings, dashboard, data_dir, demo)

    dashboard.start()

    print(f"Ember {__version__}")
    print(f"  data    {data_dir}")
    print(f"  mode    {'demo - generated data, nothing real' if demo else 'live'}")
    serve(settings, dashboard, host=args.host, port=args.port,
          verbose=args.verbose, version=__version__)
    return 0


def check(settings, dashboard, data_dir: Path, demo: bool) -> int:
    """Say what is working and what is not, and why.

    Rather than a debugging conversation, one command that tells the
    whole story: which account is missing, which file is not where it
    was expected, which token has gone stale.
    """
    from ember.hive import Hive
    from ember.octopus import Octopus

    print(f"Ember {__version__}")
    print(f"  data    {data_dir}")
    print(f"  mode    {'demo - generated data, nothing real' if demo else 'live'}")
    print()

    print("Hive")
    found = ""
    for path in dashboard._session_paths():
        mark = "found" if Path(path).is_file() else "missing"
        print(f"  {mark:8s} {path}")
        if not found and Path(path).is_file():
            found = path
    client = Hive(settings.get("hive_base", ""))
    signed_in = bool(found) and client.load_session(found)
    if signed_in:
        try:
            state = client.read_state()
            rooms = state.get("rooms", [])
            print(f"  signed in, {len(rooms)} rooms, "
                  f"house at {state.get('home_temp')}C")
        except Exception as exc:
            print(f"  signed in, but the API said: {exc}")
    else:
        print("  NOT SIGNED IN")
        print("  Sign in from the settings page:  http://127.0.0.1:8766/settings")
        print("  (or run probes/probe_hive.py once and copy the session across)")

    print()
    print("Octopus")
    key = settings.get("octopus_api_key", "")
    account = settings.get("octopus_account", "")
    if not key:
        print("  no API key - add it on the settings page, or run")
        print("  probes/probe_octopus.py")
    else:
        print(f"  key      {'set' if key else 'missing'}")
        print(f"  account  {account or 'missing'}")
        try:
            client = Octopus(key, account)
            meters = client.gas_agreements()
            if not meters:
                print("  no gas meter found on the account")
            for meter in meters:
                print(f"  meter    {meter['mprn']}  {meter['serial']}  "
                      f"({meter.get('meter_type') or 'unknown type'})")
            product, tariff = client.current_gas_product()
            print(f"  product  {product}")
            print(f"  tariff   {tariff}")
            rates = client.rates_now(product, tariff)
            print(f"  rate     {rates.get('unit_rate_p')}p/kWh")
            print(f"  standing {rates.get('standing_charge_p')}p/day")
        except Exception as exc:
            print(f"  could not read the account: {exc}")

    print()
    print("Stored history")
    try:
        stats = dashboard.history_stats()
    except Exception as exc:
        stats = {"error": str(exc)}
    if stats.get("error"):
        print(f"  could not read the history store: {stats['error']}")
    else:
        rows = stats.get("rows") or {}
        size = stats.get("bytes") or 0
        print(f"  file     {stats.get('path')}")
        print(f"  size     {size:,} bytes"
              + (f" ({size / 1024 / 1024:.1f} MB)" if size >= 1024 * 1024 else ""))
        print(f"  rows     gas {rows.get('gas', 0):,}   "
              f"polls {rows.get('polls', 0):,}   "
              f"room changes {rows.get('trv', 0):,}")
        print(f"  kept     {stats.get('history_days')} days of gas and polls, "
              f"{stats.get('trv_days')} days of room changes")
        per_day = stats.get("rows_per_day") or {}
        print(f"  growth   about {per_day.get('polls', 0)} polls and "
              f"{per_day.get('gas', 0)} gas rows a day "
              f"({per_day.get('trv_changes', 0)} room changes), "
              f"pruned once a day")
    print()
    print("Dashboard")
    payload = dashboard.payload()
    errors = payload.get("errors") or []
    if not errors:
        print("  everything is answering")
    for error in errors:
        print(f"  {error.get('source')}: {error.get('message')}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

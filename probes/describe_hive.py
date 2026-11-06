#!/usr/bin/env python3
"""Describe the saved Hive reply, so the parser can be written to fit.

The probe saved the raw reply to `hive-nodes.json`. This reads that file
and prints a compact structural summary - no schedules, no personal
details - so the shape of the data can be understood without pasting
half a megabyte of JSON.

    python3 describe_hive.py

It answers the questions the dashboard still needs answered:

  * which rooms are there, and in what order
  * what a TRV device actually reports, so "is the valve open" can be
    answered from the hardware rather than inferred
  * what the heating product reports about the boiler
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NODES = os.path.join(HERE, "hive-nodes.json")


def sample(value, depth=0):
    """A short, safe sample of any value."""
    if isinstance(value, dict):
        if depth:
            return "{...}"
        return "{" + ", ".join(list(value)[:8]) + ("..." if len(value) > 8 else "") + "}"
    if isinstance(value, list):
        if depth:
            return "[...]"
        return "[" + ", ".join(sample(item, depth + 1) for item in value[:3]) + (
            "..." if len(value) > 3 else "") + "]"
    if isinstance(value, str) and len(value) > 40:
        return value[:40] + "..."
    return value


def describe_dict(title, data, skip=()):
    print(f"\n{title}")
    if not isinstance(data, dict):
        print(f"  (not an object: {type(data).__name__})")
        return
    for key, value in data.items():
        if key in skip:
            continue
        print(f"  {key:28s} {sample(value, 1)}")


def main() -> int:
    if not os.path.isfile(NODES):
        print(f"No saved reply at {NODES}")
        print("Run probe_hive.py first.")
        return 1

    with open(NODES, encoding="utf-8") as handle:
        data = json.load(handle)

    print("=" * 68)
    print("TOP LEVEL")
    print("=" * 68)
    for key, value in data.items():
        size = len(value) if isinstance(value, (list, dict)) else 1
        print(f"  {key:24s} {type(value).__name__:6s} ({size})")

    products = data.get("products") or []
    devices = data.get("devices") or []

    # ---- the zones -----------------------------------------------------
    print("\n" + "=" * 68)
    print("THE ZONES (products of type trvcontrol)")
    print("=" * 68)
    zones = [p for p in products if isinstance(p, dict)
             and str(p.get("type", "")).lower() == "trvcontrol"]
    print(f"  {len(zones)} zone(s)")
    for index, zone in enumerate(zones):
        state = zone.get("state") or {}
        props = zone.get("props") or {}
        print(f"\n  [{index}] {state.get('name') or props.get('zoneName') or '?'}")
        print(f"      measured    {props.get('temperature')}")
        print(f"      target      {state.get('target')}")
        print(f"      frost       {state.get('frostProtection')}")
        print(f"      mode        {state.get('mode')}")
        print(f"      boost       {state.get('boost')}")
        print(f"      trvs        {props.get('trvs')}")
        print(f"      online      {props.get('online')}")
        extra = [key for key in props
                 if key not in ("temperature", "zoneName", "trvs", "online",
                                "scheduleOverride", "previous", "autoBoost",
                                "zone", "model", "modelVariant", "holidayMode",
                                "working", "schedule")]
        if extra:
            print(f"      other props {extra}")

    # ---- the heating product -------------------------------------------
    print("\n" + "=" * 68)
    print("THE HEATING PRODUCT")
    print("=" * 68)
    heating = [p for p in products if isinstance(p, dict)
               and str(p.get("type", "")).lower() in ("heating", "heatingcontroller")]
    for product in heating:
        describe_dict("  props", product.get("props") or {},
                      skip=("schedule", "holidayMode", "autoBoost"))
        describe_dict("  state", product.get("state") or {}, skip=("schedule",))

    # ---- the devices ----------------------------------------------------
    print("\n" + "=" * 68)
    print("THE DEVICES")
    print("=" * 68)
    if isinstance(devices, dict):
        device_list = list(devices.values())
    else:
        device_list = [d for d in devices if isinstance(d, dict)]

    by_type: dict[str, list] = {}
    for device in device_list:
        by_type.setdefault(str(device.get("type", "?")), []).append(device)
    for kind, group in sorted(by_type.items()):
        print(f"\n  {kind} ({len(group)})")
        describe_dict("    props of the first one", group[0].get("props") or {},
                      skip=("schedule",))
        describe_dict("    state of the first one", group[0].get("state") or {},
                      skip=("schedule",))

    # ---- the valve position, if there is one ---------------------------
    print("\n" + "=" * 68)
    print("ANY FIELD THAT LOOKS LIKE A VALVE POSITION")
    print("=" * 68)
    needles = ("position", "valve", "motor", "opening", "percent", "open")
    found = False
    for device in device_list:
        for section in ("props", "state"):
            block = device.get(section)
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if any(needle in key.lower() for needle in needles):
                    print(f"  {device.get('type')} [{section}].{key} = {sample(value, 1)}")
                    found = True
    if not found:
        print("  nothing found - the valve has to be inferred from the target")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

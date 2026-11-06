"""A demo provider - plausible data with no account behind it.

This exists so the dashboard can be run, looked at and developed against
before there are credentials, and so a fresh install shows something
instead of a wall of errors. Every screen it feeds is labelled "demo" in
the corner, because the whole point is that nobody mistakes it for the
real thing.

The data is generated, not random: gas follows a daily heating shape, the
outside temperature follows a cold November, and the gas is correlated
with the cold so the forecast has something real to fit.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ROOMS

#: Half-hourly weights for a combi boiler home in a cold month: a small
#: morning bump, a long evening peak, and a trickle of hot water all day.
PROFILE = (
    [0.15] * 12 + [0.80] * 4 + [0.25] * 16 + [0.90] * 2 + [2.20] * 8 + [0.30] * 6
)

#: The rooms, with the temperature each one sits at and how it relates to
#: the rest of the house.
ROOM_STATES = {
    "Dining Room":        (19.2, 20.0, True),
    "Downstairs Hallway": (18.6, 19.0, True),
    "Kitchen":            (20.3, 20.0, False),
    "Loft Room":          (18.4, 18.0, False),
    "Lounge":             (19.6, 19.0, False),
    "Master Bedroom":     (19.1, 19.5, True),
    "Matteo Bedroom":     (18.2, 18.0, False),
    "Sofia Bedroom":      (16.9, 17.5, True),
    "Upstairs Hallway":   (19.2, 19.0, False),
}

#: How many radiators are in each room, and how healthy their batteries
#: are. This matches the real kit: twelve valves across nine rooms, with
#: three rooms having a second radiator.
VALVE_COUNTS = {
    "Dining Room": 1,
    "Downstairs Hallway": 2,
    "Kitchen": 2,
    "Loft Room": 1,
    "Lounge": 2,
    "Master Bedroom": 1,
    "Matteo Bedroom": 1,
    "Sofia Bedroom": 1,
    "Upstairs Hallway": 1,
}

#: Lowest battery in the room, as a percentage. The Loft Room valve is
#: deliberately running low so the warning path gets exercised.
BATTERIES = {
    "Dining Room": 92,
    "Downstairs Hallway": 88,
    "Kitchen": 95,
    "Loft Room": 14,
    "Lounge": 78,
    "Master Bedroom": 91,
    "Matteo Bedroom": 84,
    "Sofia Bedroom": 96,
    "Upstairs Hallway": 87,
}

class DemoProvider:
    """Stands in for the live sources."""

    def __init__(self, seed: int = 20261104):
        self.random = random.Random(seed)
        self._seeded = False

    # -- seeding the history store ---------------------------------------

    def seed(self, history) -> None:
        """Fill the history store with 30 days of plausible readings."""
        if self._seeded:
            return
        self._seeded = True
        now = time.time()
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        # 30 complete days of gas, plus today so far.
        #
        # The daily total comes from the outside temperature, so the gas
        # and the cold actually correlate. Without that the forecast model
        # would find no slope at all, and the demo would be proving
        # nothing.
        gas_readings = []
        for offset in range(30, -1, -1):
            date = today - timedelta(days=offset)
            outside = self._outside_for(offset)
            total = self._daily_total(outside)
            scale = total / sum(PROFILE)
            last_half = 47
            if offset == 0:
                # Today is only half finished, and pretending otherwise
                # would make today's total look smaller than it is.
                last_half = min(47, int((now - date.timestamp()) / 1800))
            for index in range(last_half + 1):
                stamp = date + timedelta(minutes=30 * index)
                if stamp.timestamp() > now:
                    break
                value = PROFILE[index] * scale * self.random.uniform(0.9, 1.1)
                gas_readings.append({
                    "ts": stamp.timestamp(),
                    "value": round(value, 4),
                    "provisional": False,
                })
        history.record_gas(gas_readings)

        # Polls every two minutes - the interval the dashboard actually
        # polls at - so the seeded history looks like a dashboard that
        # has been running all along.
        #
        # The boiler runs in the morning, and in the evening for longer
        # the colder it is.
        #
        # All of them go in one transaction. A commit per poll is right
        # for a live reading, but thirty days of them is slow.
        poll_rows = []
        for offset in range(30, -1, -1):
            date = today - timedelta(days=offset)
            outside = self._outside_for(offset)
            cold = max(0.0, 12.0 - outside)
            evening_hours = min(6.0, max(2.0, 2.0 + cold * 0.6))
            for index in range(24 * 30):
                stamp = date + timedelta(minutes=2 * index)
                if stamp.timestamp() > now:
                    break
                hour = stamp.hour + stamp.minute / 60.0
                boiler = self._boiler_at(hour, evening_hours, outside)
                poll_rows.append((stamp.timestamp(), 1 if boiler else 0,
                                  19.5, outside))
        history.record_polls(poll_rows)
        history.record_trvs(now, [
            {"room": room, "target": target, "current": current,
             "valve_open": valve}
            for room, (current, target, valve) in ROOM_STATES.items()
        ])

    @staticmethod
    def _boiler_at(hour: float, evening_hours: float,
                   outside: float = 5.0) -> bool:
        """When the boiler runs.

        The schedule says when the heating is *allowed* on. The boiler
        runs until the house is warm, and that takes longer on a cold
        day - so the time it actually ran is not the time it was
        scheduled for. That difference is the whole point: Ember's
        heating figures are the time the boiler was on, not the hours
        the schedule allowed.

        The same rule is used for the seeded history and for the live
        reading, so the demo's "heating today" does not creep upwards
        every time the page is polled.
        """
        cold = max(0.0, 12.0 - outside)
        # How long the house takes to warm up, in hours.
        morning = min(2.0, 0.7 + cold * 0.14)
        evening = min(evening_hours, 1.0 + cold * 0.22)
        return (6 <= hour < 6 + morning) or (17 <= hour < 17 + evening)

    @staticmethod
    def _outside_for(days_ago: int) -> float:
        """A cold, wobbling November.

        The short wobble matters: a week of identical outside
        temperatures gives a week of identical heating times, and a
        dashboard whose heating column never changes looks like it is
        reading a schedule rather than a boiler.
        """
        base = (5.0 + 3.0 * math.sin(days_ago / 4.5)
                + 1.4 * math.cos(days_ago / 2.3)
                + 2.4 * math.sin(days_ago * 0.9))
        return round(base, 1)

    def _daily_total(self, outside: float) -> float:
        """Gas used in a day, given how cold it was.

        A combi home has a floor of gas that is used whatever the weather
        - hot water and cooking - and a heating part that grows as it
        gets colder. The floor is the intercept the forecast model
        looks for.
        """
        cold = max(0.0, 12.0 - outside)
        total = 0.78 + 0.128 * cold + self.random.uniform(-0.12, 0.12)
        return max(0.4, round(total, 3))

    # -- the sources ------------------------------------------------------

    def heating(self) -> dict:
        now = datetime.now()
        # One room's valve is being boosted, so the boost ring has
        # something to show - and shows *whose* boost it is, which is
        # the thing a bare "BOOST" never did.
        boosted_room = "Loft Room"
        room_boost_until = (now + timedelta(minutes=18)).isoformat()
        rooms = []
        for room in ROOMS:
            current, target, valve = ROOM_STATES.get(room, (19.0, 19.0, False))
            if room == boosted_room:
                # A boosted room is asked for more heat, and asks for it.
                target = 21.0
                valve = True
            rooms.append({
                "room": room,
                "current": current,
                "target": target,
                "valve_open": valve,
                "valve_count": VALVE_COUNTS.get(room, 1),
                "battery": BATTERIES.get(room),
                "boost_until": (room_boost_until if room == boosted_room
                                else None),
            })
        outside = self._outside_for(0)
        cold = max(0.0, 12.0 - outside)
        evening_hours = min(6.0, max(2.0, 2.0 + cold * 0.6))
        hour = now.hour + now.minute / 60.0
        boiler = self._boiler_at(hour, evening_hours, outside)
        boosts = [{"room": boosted_room, "until": room_boost_until,
                   "source": "room"}]
        if boiler:
            boosts.append({"room": "Boiler",
                           "until": (now + timedelta(minutes=30)).isoformat(),
                           "source": "boiler"})
        return {
            "boiler_on": boiler,
            "home_temp": 19.5,
            "target_temp": 20.0,
            "outside_temp": outside,
            # A boost only happens while the boiler is running.
            "boost_until": ((now + timedelta(minutes=30)).isoformat()
                            if boiler else None),
            "boost_minutes": 30 if boiler else 0,
            # Every boost in the house. The ring shows whichever one
            # ends soonest, and says which it is.
            "boosts": boosts,
            # No schedule block: Ember does not read Hive's schedule
            # yet, and a made-up time would be a confident lie.
            "next_block": None,
            "rooms": rooms,
        }

    def gas(self) -> dict:
        now = datetime.now()
        total = 1.84
        hourly = []
        for hour in range(24):
            weight = sum(PROFILE[hour * 2:hour * 2 + 2])
            hourly.append({"hour": hour, "m3": round(weight / sum(PROFILE) * total, 3)})
        return {
            "unit": "m3",
            "calorific": 11.16,
            "today_m3": total,
            "today_kwh": round(total * 11.16, 1),
            "cost_today": 1.44,
            "energy_p": 115.6,
            "standing_p": 28.8,
            "unit_rate_p": 5.63,
            "hourly": hourly,
            "days": [],
            "provisional": False,
        }

    def weather(self) -> dict:
        daily = []
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(-30, 7):
            date = base + timedelta(days=offset)
            # Past days look back, future days look forward: the
            # forecast for tomorrow must not be today's weather.
            mean = self._outside_for(abs(offset) if offset <= 0 else -offset)
            # A cold, damp November: cloud, and rain more often than not.
            code = 3 if mean < 8 else (2 if offset % 3 else 0)
            if mean < 4 and offset % 2:
                code = 63
            daily.append({"date": date.strftime("%Y-%m-%d"), "mean": mean,
                          "min": round(mean - 2.4, 1), "max": round(mean + 2.8, 1),
                          "code": code})
        return {"current": 4.2, "current_code": 3, "daily": daily,
                "timezone": "Europe/London"}

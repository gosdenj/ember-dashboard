"""The history store - Ember's own record of the house.

Hive's API gives the state right now. It does not give heating hours, a
heating history or a budget, because those are sums over time and Hive
computes them in their own cloud. So Ember keeps its own record: every
poll writes the boiler state, each valve's target and measured
temperature, and the outside temperature at that moment.

Everything the Insights-style panels show - hours today, hours this week,
the per-day heat figures - is read back out of this database.

SQLite, because it is in the standard library and it does the job.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, time, timedelta, timezone, tzinfo
import time as clock
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    ts           REAL PRIMARY KEY,
    boiler_on    INTEGER NOT NULL DEFAULT 0,
    home_temp    REAL,
    outside_temp REAL
);

CREATE TABLE IF NOT EXISTS trv (
    ts         REAL NOT NULL,
    room       TEXT NOT NULL,
    target     REAL,
    current    REAL,
    valve_open INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, room)
);

CREATE TABLE IF NOT EXISTS gas (
    ts          REAL PRIMARY KEY,
    value       REAL NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_trv_room_ts ON trv (room, ts);
"""


def _reading_value(item: dict) -> float | None:
    """The number on a gas reading, whatever it is called.

    The API calls it 'consumption', the history store used to call it
    'm3' and the demo calls it that too. Accepting all three means a
    shape surprise never stops the dashboard from starting.
    """
    if not isinstance(item, dict):
        return None
    for key in ("value", "m3", "consumption"):
        if item.get(key) is None:
            continue
        try:
            return float(item[key])
        except (TypeError, ValueError):
            continue
    return None


def midnight(tz: tzinfo) -> datetime:
    """The start of today, in a given timezone."""
    return datetime.now(timezone.utc).astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0)


def day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """The local midnight either side of a moment.

    Working in local time matters: a British Gas day is 23 hours on the
    spring clock change and 25 on the autumn one, and a day that has the
    wrong number of hours is a day whose total is wrong.

    The end is found by taking the *next date* at midnight rather than
    by adding 24 hours. Adding a timedelta to an aware datetime keeps
    the wall clock and the old UTC offset, which puts the end an hour
    out on the two clock-change days of the year.
    """
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = start.date() + timedelta(days=1)
    end = datetime.combine(tomorrow, time(0, 0), tzinfo=start.tzinfo)
    return start, end


def day_length(moment: datetime) -> float:
    """How many seconds a local day is, in real time."""
    start, end = day_bounds(moment)
    return (end.astimezone(timezone.utc)
            - start.astimezone(timezone.utc)).total_seconds()


class History:
    """Read and write access to the history database."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # What each room was last time we wrote it down, so the room
        # log only records changes. It is a cache of what is in the
        # database, not a second source of truth.
        self._trv_state: dict[str, tuple] = {}
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Bring an older database up to date.

        The gas column used to be called 'm3', which claimed the reading
        was in cubic metres - true for a SMETS2 meter, not for a SMETS1
        one. It is called 'value' now, and an existing database is
        renamed in place so the readings already collected are kept.
        """
        columns = [row["name"] for row in
                   self._db.execute("PRAGMA table_info(gas)").fetchall()]
        if not columns:
            return
        if "value" not in columns and "m3" in columns:
            self._db.execute("ALTER TABLE gas RENAME COLUMN m3 TO value")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writing ----------------------------------------------------------

    def record_poll(self, ts: float, boiler_on: bool, home_temp: float | None,
                    outside_temp: float | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO polls (ts, boiler_on, home_temp, outside_temp)"
                " VALUES (?,?,?,?)",
                (ts, 1 if boiler_on else 0, home_temp, outside_temp),
            )
            self._db.commit()

    def record_polls(self, rows: Sequence[tuple]) -> int:
        """Store several polls in one go.

        One commit per poll is the right thing for a live poll - the
        reading should survive a crash - but seeding thirty days of
        history that way is slow, and on a Raspberry Pi's SD card it is
        painfully slow. This writes them all in one transaction.
        """
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO polls (ts, boiler_on, home_temp, outside_temp)"
                " VALUES (?,?,?,?)", rows)
            self._db.commit()
        return len(rows)

    def record_trvs(self, ts: float, readings: Sequence[dict]) -> None:
        """Record what each room was asking for.

        A row is written when the room's state CHANGES - its target, or
        whether it is asking for heat - and not on every poll. Writing
        every poll meant 12 rows every two minutes, all but a handful of
        them saying the same thing: eight thousand rows a day to say
        "the Lounge is still off". The change log says the same thing in
        a few dozen rows a day, and it is the more useful record: it is
        the list of moments a room started or stopped asking for heat.

        The measured temperature is written with the change, so the row
        says what the room was when it turned over.
        """
        rows = []
        with self._lock:
            for item in readings:
                room = item.get("room")
                if room is None:
                    continue
                calling = 1 if item.get("valve_open") else 0
                target = item.get("target")
                state = (target, calling)
                if self._trv_state.get(room) == state:
                    continue
                self._trv_state[room] = state
                rows.append((ts, room, target, item.get("current"), calling))
            if rows:
                self._db.executemany(
                    "INSERT OR REPLACE INTO trv (ts, room, target, current, valve_open)"
                    " VALUES (?,?,?,?,?)", rows)
                self._db.commit()

    def record_gas(self, readings: Iterable[dict]) -> int:
        """Store gas consumption intervals. Returns how many rows were written.

        Readings are upserted, because Octopus revises a provisional
        reading into a confirmed one and the newer value must win.

        The number is the meter's own reading, in the meter's own unit -
        cubic metres on a SMETS2 meter, kWh on a SMETS1 one - so the
        column is called 'value' rather than 'm3'. The reading can arrive
        under any of three names depending on where it came from, and a
        row that makes no sense is skipped rather than taking the whole
        poll down with it.
        """
        rows = []
        for item in readings or []:
            value = _reading_value(item)
            if value is None:
                continue
            try:
                stamp = float(item["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append((stamp, value,
                         1 if item.get("provisional") else 0))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO gas (ts, value, provisional) VALUES (?,?,?)",
                rows)
            self._db.commit()
        return len(rows)

    def delete_gas_since(self, ts: float) -> int:
        """Forget the gas readings from a moment on.

        Used when seeding or re-seeding the store: the readings that
        are already there would be counted twice.
        """
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM gas WHERE ts >= ?", (ts,)).fetchone()[0]
            self._db.execute("DELETE FROM gas WHERE ts >= ?", (ts,))
            self._db.commit()
        return count

    def delete_gas_between(self, start: float, end: float) -> int:
        """Forget the gas readings between two moments.

        Used to take a day out of the store - a meter exchange leaves a
        hole, and a hole is what the charts have to be able to show.
        """
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM gas WHERE ts >= ? AND ts < ?",
                (start, end)).fetchone()[0]
            self._db.execute("DELETE FROM gas WHERE ts >= ? AND ts < ?",
                             (start, end))
            self._db.commit()
        return count

    def prune(self, days: int, trv_days: int | None = None) -> int:
        """Drop anything older than the retention window.

        Every table has one. The gas readings and the polls are the
        history the dashboard is built on, so they are kept for as long
        as the user asked. The room readings change only when a room
        asks for heat, so they are kept for much less time - they are a
        log, not a dataset.

        Returns how many rows were dropped.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).timestamp()
        trv_cutoff = (now - timedelta(days=trv_days if trv_days is not None else days)).timestamp()
        with self._lock:
            total = 0
            for table, limit in (("polls", cutoff), ("gas", cutoff), ("trv", trv_cutoff)):
                count = self._db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ts < ?", (limit,)
                ).fetchone()[0]
                if count:
                    self._db.execute(f"DELETE FROM {table} WHERE ts < ?", (limit,))
                    total += count
            self._db.commit()
            # The change log is now missing the rows we just dropped, so
            # what we remember about each room is no longer true of the
            # database. Forget it and let the next poll write the
            # state again.
            if total:
                self._trv_state.clear()
        return total

    def stats(self) -> dict:
        """What is actually in the store, and how big it is on disk.

        The question "is this thing going to fill my card?" deserves a
        number, not a guess.
        """
        with self._lock:
            counts = {}
            for table in ("polls", "trv", "gas"):
                counts[table] = self._db.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        size = 0
        path = Path(self.path) if self.path else None
        if path is not None:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        return {
            "rows": counts,
            "bytes": size,
            "path": str(path) if path else "",
        }

    # -- reading ----------------------------------------------------------

    def polls_between(self, start: float, end: float) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(
                "SELECT ts, boiler_on, home_temp, outside_temp FROM polls"
                " WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end)))

    def latest_poll(self) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT ts, boiler_on, home_temp, outside_temp FROM polls"
                " ORDER BY ts DESC LIMIT 1").fetchone()

    def latest_trvs(self) -> dict[str, dict]:
        """The most recent reading for each room."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.room, t.target, t.current, t.valve_open, t.ts FROM trv t"
                " JOIN (SELECT room, MAX(ts) AS ts FROM trv GROUP BY room) latest"
                " ON t.room = latest.room AND t.ts = latest.ts"
            ).fetchall()
        return {row["room"]: dict(row) for row in rows}

    def heating_minutes(self, start: float, end: float,
                        max_gap: float | None = None) -> float:
        """Minutes the boiler was on between two moments.

        The boiler's state is only known at the moments we polled it, so
        the answer is an estimate: each poll is assumed to describe the
        period until the next one.

        But only for the periods Ember was actually watching. A gap
        longer than the poll ceiling is a period Ember was not running,
        and assuming the boiler stayed on for the whole of it is not an
        estimate - it is an invention. A six-hour gap with the boiler
        on at the start would otherwise be counted as six hours of
        heating.
        """
        return self._heating_from_rows(self.polls_between(start, end),
                                       start, end, max_gap)

    # The floor for the gap ceiling. A gap shorter than this is always
    # treated as a normal poll interval, whatever the data says.
    MIN_POLL_GAP = 900.0

    def _gap_ceiling(self, rows) -> float:
        """How long a gap between polls still counts as Ember watching.

        **Not a constant.** The poll interval is not a constant either:
        a dashboard whose polls are slow - a session that needs
        renewing takes minutes, a network that is having a bad day -
        would have every single interval judged "not watching" if the
        ceiling were a fixed number, and its heating hours would be
        zero. That is exactly the bug this replaces.

        So the ceiling comes from the data: the **typical** gap (the
        median, which ignores both the odd fast poll and the one long
        break) times a generous factor, with a floor under it.

        A gap far longer than the typical one is a period Ember was
        not running: the boiler's state through it is unknown, and
        assuming it held would be an invention rather than a
        measurement.
        """
        gaps = sorted(rows[index + 1]["ts"] - rows[index]["ts"]
                     for index in range(len(rows) - 1))
        gaps = [gap for gap in gaps if gap > 0]
        if not gaps:
            return self.MIN_POLL_GAP
        median = gaps[len(gaps) // 2]
        return max(self.MIN_POLL_GAP, median * 10)

    def _watched_minutes(self, rows, start: float, end: float,
                         max_gap: float | None = None) -> float:
        """How much of the window Ember was actually watching.

        The heating minutes are a sum over the intervals between polls,
        so a period when Ember was not running is simply missing from
        the total - and the total looks like a whole day's heating.

        This counts the time that was genuinely covered: the intervals
        between polls that are no longer than the gap ceiling. The
        difference between this and the length of the window is what
        Ember did not see.
        """
        if not rows:
            return 0.0
        ceiling = max(self._gap_ceiling(rows),
                      max_gap if max_gap is not None else 0.0)
        covered = 0.0
        for index in range(len(rows) - 1):
            gap = rows[index + 1]["ts"] - rows[index]["ts"]
            if gap <= 0 or gap > ceiling:
                continue
            segment_start = max(start, rows[index]["ts"])
            segment_end = min(end, rows[index + 1]["ts"])
            if segment_end > segment_start:
                covered += segment_end - segment_start
        # The last poll has no successor, so it cannot be paired. A
        # single poll interval is assumed - the same assumption the
        # heating sum makes.
        if len(rows) > 1:
            gap = rows[-1]["ts"] - rows[-2]["ts"]
            if 0 < gap <= ceiling:
                covered += min(gap, end - max(start, rows[-1]["ts"]))
        return covered / 60.0

    def _heating_from_rows(self, rows, start: float, end: float,
                           max_gap: float | None = None) -> float:
        """The same sum, from rows that have already been fetched."""
        if not rows:
            return 0.0
        ceiling = max(self._gap_ceiling(rows),
                      max_gap if max_gap is not None else 0.0)
        total = 0.0
        for index in range(len(rows) - 1):
            gap = rows[index + 1]["ts"] - rows[index]["ts"]
            if gap <= 0 or gap > ceiling:
                # Ember was not running for this stretch, so it does
                # not know what the boiler was doing.
                continue
            if not rows[index]["boiler_on"]:
                continue
            segment_start = max(start, rows[index]["ts"])
            segment_end = min(end, rows[index + 1]["ts"])
            if segment_end > segment_start:
                total += segment_end - segment_start
        # The last poll inside the window has no successor to pair with.
        # If it says the boiler is on, count a single poll interval.
        if len(rows) > 1 and rows[-1]["boiler_on"]:
            gap = rows[-1]["ts"] - rows[-2]["ts"]
            if 0 < gap <= ceiling:
                total += min(gap, end - max(start, rows[-1]["ts"]))
        return total / 60.0

    def heating_minutes_by_day(self, days: int, tz: tzinfo | None = None,
                                now: float | None = None,
                                max_gap: float | None = None) -> list[dict]:
        """Heating minutes for each of the last N days, oldest first.

        The current day is cut off at now. Without that, the last poll
        is assumed to describe the rest of the day - so today's total
        would include heating that has not happened yet, and it would
        not match the "heating today" figure shown beside it.
        """
        tz = tz or timezone.utc
        anchor = midnight(tz)
        cutoff = clock.time() if now is None else now
        result = []
        for offset in range(days - 1, -1, -1):
            start, end = day_bounds(anchor - timedelta(days=offset))
            start_ts = start.timestamp()
            end_ts = end.timestamp()
            if end_ts > cutoff:
                end_ts = cutoff
            if end_ts < start_ts:
                end_ts = start_ts
            rows = self.polls_between(start_ts, end_ts)
            minutes = self._heating_from_rows(rows, start_ts, end_ts, max_gap)
            watched = self._watched_minutes(rows, start_ts, end_ts, max_gap)
            length = max(0.0, (end_ts - start_ts) / 60.0)
            result.append({
                "date": start.strftime("%Y-%m-%d"),
                "label": start.strftime("%a"),
                "minutes": round(minutes, 1),
                # How much of the day Ember was watching, and whether
                # that was all of it. A day it only watched half of
                # holds a *lower bound* on the heating, not the day's
                # heating.
                "watched_minutes": round(watched, 1),
                "complete": bool(rows) and watched >= length - 10.0,
                # Whether anything was recorded for that day at all.
                #
                # A day with no polls is not a day with no heating - it
                # is a day Ember was not running. Saying "0h 00m" would
                # be a claim, and a false one.
                "recorded": bool(rows),
            })
        return result

    def gas_by_day(self, days: int, tz: tzinfo | None = None) -> list[dict]:
        """Gas used per day, oldest first.

        The readings are increments, so a day is the sum of the readings
        that fall inside it.

        Each day also says whether it is **complete**. Octopus delivers
        readings with a lag, so a day can be missing altogether - and a
        missing day is not a day with no gas in it. The fit has to know
        the difference.

        A day is complete when the meter has reported readings that
        **cover** it - not merely when later data has arrived. That
        distinction matters after a meter exchange: the old meter stops,
        the new one starts reporting days later, and every day in
        between looks like "the data has reached past the end of this
        day" while holding almost nothing. Those days are gaps, not days
        of no gas, and the forecast fit must not learn from them.

        Octopus returns a row for every interval, zeros included, so a
        day that should hold one row per hour and holds two is a day
        the meter did not report - not a day of almost no gas. Two
        readings are allowed to be missing, because an odd half-hour
        does go missing now and then.
        """
        tz = tz or timezone.utc
        anchor = midnight(tz)
        newest = self.newest_gas_reading()
        buckets: list[dict] = []
        for offset in range(days - 1, -1, -1):
            start, end = day_bounds(anchor - timedelta(days=offset))
            buckets.append({
                "date": start.strftime("%Y-%m-%d"),
                "label": start.strftime("%a"),
                # The day of the month, and where the day falls in the
                # week, so a calendar can be drawn without the browser
                # having to work it out from a date string.
                "day": start.day,
                "weekday": start.weekday(),
                "value": 0.0,
                "count": 0,
                "first": None,
                "last": None,
                # The local day's own bounds, in seconds. They come from
                # day_bounds, so a clock-change day is 23 or 25 hours
                # long rather than 24.
                "start_ts": start.timestamp(),
                "end_ts": end.timestamp(),
                # Whether the data has reached past the end of this day.
                # The readings are stored with the interval's start time,
                # so the last reading of a day starts an hour before
                # midnight.
                "passed": newest is not None and newest >= end.timestamp() - 3600,
                # How many readings this day should hold: one per hour,
                # except on the two clock-change days of the year.
                "expected": max(1, round((end - start).total_seconds() / 3600)),
                # Whether the meter sent anything at all for this day.
                # "reported" and "complete" are different questions: today
                # is reported but not over, and a day after a meter
                # exchange is neither.
                "reported": False,
                "complete": False,
            })
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, value FROM gas WHERE ts >= ?", ((anchor - timedelta(days=days)).timestamp(),)
            ).fetchall()
        for row in rows:
            local = datetime.fromtimestamp(row["ts"], timezone.utc).astimezone(tz)
            index = (local.date() - (anchor - timedelta(days=days - 1)).date()).days
            if 0 <= index < len(buckets):
                bucket = buckets[index]
                bucket["value"] += row["value"]
                bucket["count"] += 1
                if bucket["first"] is None or row["ts"] < bucket["first"]:
                    bucket["first"] = row["ts"]
                if bucket["last"] is None or row["ts"] > bucket["last"]:
                    bucket["last"] = row["ts"]
        for bucket in buckets:
            bucket["value"] = round(bucket["value"], 3)
            bucket["reported"] = bucket["count"] > 0
            bucket["complete"] = self._day_is_covered(bucket)
        return buckets

    @staticmethod
    def _day_is_covered(bucket: dict) -> bool:
        """Does the day's own set of readings cover the whole day?

        The count is what matters. Octopus returns a row for every
        interval, zeros included, so a day holding a row per hour is a
        day the meter reported and a day holding two rows is a day it
        did not. Two rows are allowed to be missing, because an odd
        half-hour does go missing now and then.
        """
        if not bucket.get("passed"):
            return False
        expected = bucket.get("expected") or 24
        return bucket.get("count", 0) >= max(1, expected - 2)

    def gas_hourly_profile(self, days: int = 7, tz: tzinfo | None = None) -> dict:
        """The shape of a day: the average use in each hour.

        Octopus delivers gas a day or two late, so "the last 24 hours" is
        usually empty - and the hour-by-hour shape of *one* day is not
        what a house does every day. This averages each hour across the
        last few days instead, which answers "when does this house use
        gas" and is never empty while there is any history at all.

        Each hour says how many days it was averaged over, because an
        hour with one day behind it is not the same claim as an hour
        with seven.
        """
        tz = tz or timezone.utc
        anchor = midnight(tz)
        first = (anchor - timedelta(days=days - 1)).timestamp()
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, value FROM gas WHERE ts >= ?", (first,)).fetchall()

        # Sum each hour within each day, so one day cannot count twice.
        per_day: dict[tuple[str, int], float] = {}
        for row in rows:
            local = datetime.fromtimestamp(row["ts"], timezone.utc).astimezone(tz)
            key = (local.strftime("%Y-%m-%d"), local.hour)
            per_day[key] = per_day.get(key, 0.0) + row["value"]

        days_seen = {date for date, _ in per_day}
        hours: list[dict] = []
        for hour in range(24):
            totals = [value for (date, when), value in per_day.items()
                      if when == hour]
            hours.append({
                "hour": hour,
                "avg": round(sum(totals) / len(totals), 4) if totals else 0.0,
                "days": len(totals),
            })
        return {"hours": hours, "days": len(days_seen)}

    def gas_total_between(self, start: float, end: float) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(value), 0) AS total FROM gas WHERE ts >= ? AND ts < ?",
                (start, end)).fetchone()
        return float(row["total"])

    def gas_partial_by_day(self, days: int, tz: tzinfo | None = None,
                            now: datetime | None = None) -> list[dict]:
        """Gas used in each of the last N days, up to the same time of day.

        This is what makes "today against the average" a fair comparison:
        at two o'clock in the afternoon, today is compared with two
        o'clock in the afternoon on the other days, not with their whole
        selves.

        ``now`` says which time of day the comparison is made at. It
        defaults to the actual time, but it can be stated outright, which
        is the difference between a test that proves the behaviour and
        one that only passes at a particular time of day.
        """
        tz = tz or timezone.utc
        moment = now or datetime.now(timezone.utc)
        anchor = moment.astimezone(tz).replace(
            hour=0, minute=0, second=0, microsecond=0)
        local = moment.astimezone(tz)
        cutoff = local.hour + local.minute / 60.0
        result = []
        for offset in range(days - 1, -1, -1):
            start, _ = day_bounds(anchor - timedelta(days=offset))
            end = start + timedelta(hours=cutoff)
            if end > moment:
                end = moment
            total = self.gas_total_between(start.timestamp(), end.timestamp())
            result.append({
                "date": start.strftime("%Y-%m-%d"),
                "value": round(total, 3),
                "partial": True,
            })
        return result

    def oldest_gas_reading(self) -> float | None:
        """The oldest gas reading we hold, as a timestamp.

        It is how Ember knows how much history it has, and a chart of
        the week needs a week's worth.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(ts) FROM gas").fetchone()
        return row[0] if row and row[0] is not None else None

    def newest_gas_reading(self) -> float | None:
        """The timestamp of the most recent reading, or None.

        Octopus does not deliver readings as they happen, so this is
        how far behind the meter is - and a day with no readings is a
        day that has not been reported, not a day with no gas in it.
        """
        with self._lock:
            row = self._db.execute("SELECT MAX(ts) FROM gas").fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    def gas_readings_between(self, start: float, end: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, value, provisional FROM gas WHERE ts >= ? AND ts < ? ORDER BY ts",
                (start, end)).fetchall()
        return [dict(row) for row in rows]

    def outside_temperatures_by_day(self, days: int,
                                     tz: tzinfo | None = None) -> list[dict]:
        """Mean outside temperature per day, from the polls."""
        tz = tz or timezone.utc
        anchor = midnight(tz)
        totals: dict[str, list[float]] = {}
        for offset in range(days):
            start, end = day_bounds(anchor - timedelta(days=offset))
            for row in self.polls_between(start.timestamp(), end.timestamp()):
                if row["outside_temp"] is None:
                    continue
                key = start.strftime("%Y-%m-%d")
                totals.setdefault(key, []).append(row["outside_temp"])
        result = []
        for offset in range(days - 1, -1, -1):
            start, _ = day_bounds(anchor - timedelta(days=offset))
            key = start.strftime("%Y-%m-%d")
            values = totals.get(key, [])
            result.append({
                "date": key,
                "mean_outside": round(sum(values) / len(values), 2) if values else None,
            })
        return result

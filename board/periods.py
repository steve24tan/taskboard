"""Server-day / server-week maths.

Game dailies reset at a fixed hour in the game's server timezone (CN servers: 04:00 UTC+8),
weeklies at that hour on Monday. A fixed UTC offset is used on purpose: zoneinfo needs the
tzdata package on Windows, and the CN servers have no DST.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CADENCES = ("daily", "weekly", "monthly")


def tz_of(offset_hours: float) -> timezone:
    return timezone(timedelta(hours=offset_hours))


def period_start(ts: float, cadence: str, reset_hour: int = 4, tz_hours: float = 8) -> float:
    """Unix time at which the period containing `ts` started."""
    tz = tz_of(tz_hours)
    t = datetime.fromtimestamp(ts, tz)
    day0 = t.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if t < day0:
        day0 -= timedelta(days=1)
    if cadence == "daily":
        start = day0
    elif cadence == "weekly":
        start = day0 - timedelta(days=day0.weekday())        # Monday
    elif cadence == "monthly":
        start = day0.replace(day=1)
        if start > t:   # 1st of the month before the reset hour belongs to last month
            prev = (start - timedelta(days=1)).replace(day=1)
            start = prev
    else:
        raise ValueError("unknown cadence: %r" % cadence)
    return start.timestamp()


def next_start(start: float, cadence: str, reset_hour: int = 4, tz_hours: float = 8) -> float:
    tz = tz_of(tz_hours)
    s = datetime.fromtimestamp(start, tz)
    if cadence == "daily":
        n = s + timedelta(days=1)
    elif cadence == "weekly":
        n = s + timedelta(days=7)
    else:
        n = (s.replace(day=28) + timedelta(days=4)).replace(day=1)
    return n.timestamp()


def recent_periods(now: float, cadence: str, count: int, reset_hour: int = 4, tz_hours: float = 8):
    """[(start, end), ...] oldest first, the last one being the current period."""
    cur = period_start(now, cadence, reset_hour, tz_hours)
    out = [(cur, next_start(cur, cadence, reset_hour, tz_hours))]
    while len(out) < count:
        prev_end = out[0][0]
        prev = period_start(prev_end - 1, cadence, reset_hour, tz_hours)
        out.insert(0, (prev, prev_end))
    return out


def label(start: float, cadence: str, tz_hours: float = 8) -> str:
    d = datetime.fromtimestamp(start, tz_of(tz_hours))
    if cadence == "monthly":
        return d.strftime("%Y-%m")
    return d.strftime("%m-%d")

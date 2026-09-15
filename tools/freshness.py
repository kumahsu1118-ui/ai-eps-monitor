#!/usr/bin/env python3
"""Schedule-aware freshness for weekday 08:00 Taipei collections.

Weekdays 08:00 Taipei are the expected collection times. Friday success
keeps Saturday/Sunday fresh. Monday after grace without a new success is stale.
Same rule applies per-ticker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
SCHEDULE_HOUR = 8
GRACE_HOURS_DEFAULT = 2.0


def to_taipei(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI)


def last_scheduled_run_due(now_local: datetime, grace_hours: float = GRACE_HOURS_DEFAULT) -> datetime:
    """Latest weekday 08:00 Taipei that is already due (grace elapsed)."""
    now_local = to_taipei(now_local)
    weekday = now_local.weekday()  # Mon=0 ... Sun=6

    def at_schedule(d: datetime) -> datetime:
        return d.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)

    if weekday >= 5:
        # Weekend: last due run is Friday 08:00 (no Sat/Sun schedule).
        days_since_friday = weekday - 4
        friday = now_local - timedelta(days=days_since_friday)
        return at_schedule(friday)

    today_run = at_schedule(now_local)
    due_cutoff = today_run + timedelta(hours=float(grace_hours))
    if now_local < due_cutoff:
        # Previous weekday 08:00
        if weekday == 0:
            return at_schedule(now_local - timedelta(days=3))  # Friday
        return at_schedule(now_local - timedelta(days=1))
    return today_run


def is_schedule_stale(
    last_success: datetime | None,
    now: datetime | None = None,
    grace_hours: float = GRACE_HOURS_DEFAULT,
) -> bool:
    """True when last success is before the last due weekday 08:00 Taipei run."""
    if last_success is None:
        return True
    now_dt = now if now is not None else datetime.now(timezone.utc)
    now_local = to_taipei(now_dt)
    success_local = to_taipei(last_success)
    due = last_scheduled_run_due(now_local, grace_hours=grace_hours)
    return success_local < due


def parse_iso_to_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    s = str(iso).strip()
    if not s:
        return None
    raw = s
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

#!/usr/bin/env python3
"""Schedule-aware collection freshness (weekday 08:00 Taipei + grace).

Single implementation used by ``export_web_data`` (persisted meta) and
``health_check`` (recompute vs a supplied ``now``). Do not fork this rule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
COLLECTION_HOUR_TAIPEI = 8
COLLECTION_GRACE_HOURS = 6


def parse_iso_dt(iso_utc: str) -> datetime | None:
    s = (iso_utc or "").strip()
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


def most_recent_due_collection_start(now_tp: datetime, grace_hours: int = COLLECTION_GRACE_HOURS) -> datetime | None:
    """Most recent weekday 08:00 Taipei whose (start+grace) is <= now_tp."""
    d = now_tp.date()
    for i in range(0, 12):
        day = d - timedelta(days=i)
        if day.weekday() >= 5:  # Sat/Sun
            continue
        start = datetime(day.year, day.month, day.day, COLLECTION_HOUR_TAIPEI, 0, 0, tzinfo=TAIPEI)
        if now_tp >= start + timedelta(hours=grace_hours):
            return start
    return None


def next_expected_collection(now_tp: datetime) -> datetime:
    """Next weekday 08:00 Taipei strictly after now_tp (or today if still before 08:00)."""
    d = now_tp.date()
    for i in range(0, 10):
        day = d + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        start = datetime(day.year, day.month, day.day, COLLECTION_HOUR_TAIPEI, 0, 0, tzinfo=TAIPEI)
        if start > now_tp:
            return start
    day = d + timedelta(days=1)
    while day.weekday() >= 5:
        day = day + timedelta(days=1)
    return datetime(day.year, day.month, day.day, COLLECTION_HOUR_TAIPEI, 0, 0, tzinfo=TAIPEI)


def compute_freshness(last_success_iso: str | None, now: datetime | None = None, grace_hours: int = COLLECTION_GRACE_HOURS) -> dict:
    """Schedule-aware freshness shared by exporter meta and the health check.

    Friday success → Sat/Sun NOT stale.
    Monday expected run + grace without success → stale.
    """
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_tp = now_utc.astimezone(TAIPEI)
    last_dt = parse_iso_dt(last_success_iso) if last_success_iso else None
    last_tp = last_dt.astimezone(TAIPEI) if last_dt else None

    due = most_recent_due_collection_start(now_tp, grace_hours=grace_hours)
    nxt = next_expected_collection(now_tp)
    next_expected = nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_after = (nxt + timedelta(hours=grace_hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_due_grace = None
    if due is not None:
        last_due_grace = (due + timedelta(hours=grace_hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if last_tp is None:
        return {
            "dataStale": True,
            "nextExpected": next_expected,
            "staleAfter": stale_after,
            "lastDueGraceDeadline": last_due_grace,
            "freshnessRule": "weekday_0800_taipei_plus_grace",
            "graceHours": grace_hours,
        }
    if due is None:
        stale = False
    else:
        stale = last_tp < due
    return {
        "dataStale": stale,
        "nextExpected": next_expected,
        "staleAfter": stale_after,
        "lastDueGraceDeadline": last_due_grace,
        "freshnessRule": "weekday_0800_taipei_plus_grace",
        "graceHours": grace_hours,
        "lastDueCollectionStart": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if due else None,
    }


def is_schedule_stale(last_success_iso: str | None, now: datetime | None = None, grace_hours: int = COLLECTION_GRACE_HOURS) -> bool:
    return bool(compute_freshness(last_success_iso, now=now, grace_hours=grace_hours).get("dataStale"))

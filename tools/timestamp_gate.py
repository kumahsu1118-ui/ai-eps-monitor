#!/usr/bin/env python3
"""Snapshot timestamp gate.

Invalid timestamps always fail. Future timestamps (beyond a small clock skew)
always fail. Historical dates older than MAX_AGE_WITHOUT_BACKFILL require --backfill.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

TAIPEI = timezone(timedelta(hours=8))
FUTURE_SKEW = timedelta(minutes=5)
MAX_AGE_WITHOUT_BACKFILL = timedelta(days=2)

_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?)?$"
)


class TimestampGateError(ValueError):
    """snapshot_utc failed the timestamp gate."""


def parse_snapshot_utc(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _ISO.match(s)
    if not m:
        # try fromisoformat (Python 3.11+ handles Z in 3.11)
        try:
            ss = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ss)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    day = m.group(1)
    if m.group(2) is None:
        try:
            dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    try:
        dt = datetime(
            int(day[0:4]), int(day[5:7]), int(day[8:10]),
            int(m.group(2)), int(m.group(3)), int(m.group(4)),
            tzinfo=timezone.utc,
        )
        return dt
    except Exception:
        return None


def validate_snapshot_timestamp(
    snapshot: dict | None,
    *,
    now: datetime | None = None,
    backfill: bool = False,
) -> dict:
    """Return {ok, reason, parsed}. reason in {ok, invalid_timestamp, future_timestamp, historical_without_backfill}."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    raw = None
    if isinstance(snapshot, dict):
        raw = snapshot.get("snapshot_utc") or snapshot.get("snapshotUtc")
    parsed = parse_snapshot_utc(raw)
    if parsed is None:
        return {
            "ok": False,
            "reason": "invalid_timestamp",
            "message": f"invalid snapshot_utc={raw!r}",
            "parsed": None,
        }
    if parsed > now + FUTURE_SKEW:
        return {
            "ok": False,
            "reason": "future_timestamp",
            "message": f"snapshot_utc {parsed.isoformat()} is in the future",
            "parsed": parsed,
        }
    age = now - parsed
    if (not backfill) and age > MAX_AGE_WITHOUT_BACKFILL:
        return {
            "ok": False,
            "reason": "historical_without_backfill",
            "message": (
                f"snapshot_utc {parsed.isoformat()} is older than "
                f"{MAX_AGE_WITHOUT_BACKFILL.days}d; pass --backfill to ingest"
            ),
            "parsed": parsed,
        }
    return {"ok": True, "reason": "ok", "message": "ok", "parsed": parsed}


def assert_snapshot_timestamp(snapshot: dict | None, *, now=None, backfill: bool = False) -> datetime:
    result = validate_snapshot_timestamp(snapshot, now=now, backfill=backfill)
    if not result["ok"]:
        raise TimestampGateError(result["message"])
    return result["parsed"]

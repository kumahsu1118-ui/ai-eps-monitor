#!/usr/bin/env python3
"""Shared Internal revision-window math (export + alert engine).

Single writer of this formula: both ``export_web_data.py`` (EPS Revision
Momentum 30/60/90D) and ``build_alerts.py`` (Internal 30D cumulative) MUST
call these helpers so fiscal identity, latest-observation anchor, baseline
selection, same-day collapse, and EPS percent math cannot drift apart.

Collection schedule
-------------------
Daily pipeline is weekdays ``0 8 * * 1-5`` Taipei. Saturday and Sunday are
not collection days. A Friday observation followed by a Monday observation
is a normal weekend gap, not missing history.

Window identity and anchor
--------------------------
* Identity = ``(ticker, normalized reportedFiscalPeriodEnding)``.
  Mapped year / slot is display-only and is never the grouping key.
* Anchor = latest valid observation at or before ``as_of`` for that identity.
  Snapshot / ``as_of`` is only a cutoff; it is not the window origin when it
  sits ahead of the latest usable daily row (weekend, stale/partial day).
* ``targetDate = anchorDate − N calendar days``.

Same-day normalization
----------------------
For ``(ticker, reportedFiscalPeriodEnding, date)`` keep one row:
1. Prefer the observation with the latest ``updateTime``.
2. If ``updateTime`` is missing or tied, last append order wins.

Schedule-aware / bounded-gap baseline (fail-closed)
---------------------------------------------------
Replace the old fixed ``INTERNAL_WINDOW_START_SLACK_DAYS = 2`` calendar slack.

Candidate baseline = last same-day-normalized observation with
``date ≤ targetDate``. It is accepted iff the **weekday gap** from that
observation date to ``targetDate`` is ``≤ MAX_BASELINE_WEEKDAY_GAP`` (1).

Weekday gap = number of Monday–Friday dates in the half-open interval
``(observation_date, targetDate]``. Weekends never count.

This means:

* Friday baseline, Monday ``targetDate``: weekdays in (Fri, Mon] = {Mon} = 1
  → **valid** (normal Friday→Monday). Saturday/Sunday targets from a Friday
  baseline have gap 0 and are also valid.
* An observation many collection days before ``targetDate`` (e.g. a 90-day-old
  row used as a 30D baseline): weekday gap ≫ 1 → **unavailable**. Ancient
  points must not masquerade as a valid N-day baseline. Never substitute
  "oldest in series".

Zero baseline (start EPS == 0) → unavailable (no division by zero).
Insufficient history → unavailable. Each of 30/60/90D is independent.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

INTERNAL_REVISION_WINDOWS = (30, 60, 90)

# Bounded number of Mon–Fri dates allowed in (baseline_date, targetDate].
# 1 covers Friday→Monday (Monday counts) and a single weekday holiday.
# Larger gaps fail closed. See module docstring.
MAX_BASELINE_WEEKDAY_GAP = 1


def to_num(x: Any) -> float | None:
    """Parse number; reject non-finite (NaN/Infinity)."""
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {
        "",
        "n/a",
        "na",
        "n/a (baseline)",
        "data unavailable",
        "null",
        "none",
        "nan",
        "inf",
        "-inf",
        "+inf",
        "infinity",
        "-infinity",
    }:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def parse_obs_datetime(value: Any) -> datetime | None:
    """Parse a daily-snapshot timestamp or YYYY-MM-DD date to UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    # Timestamp formats MUST be tried before YYYY-MM-DD. Parsing "2026-09-15T18:00:00Z"
    # with the date-only format would collapse every same-day updateTime to midnight
    # and last-append would incorrectly win over a later clock time.
    if "T" in s:
        raw = s
        try:
            iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw[:19] + ("Z" if fmt.endswith("Z") else ""), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def fiscal_identity_key(ticker, fiscal_label) -> tuple[str, str] | None:
    """Cross-time identity: ticker + normalized reportedFiscalPeriodEnding.

    Mapped year / slot is display-only and must not be used as identity.
    """
    import sa_parser as _sa

    t = str(ticker or "").strip().upper()
    lab = _sa.normalize_fiscal_period_label(fiscal_label)
    if not t or not lab:
        return None
    return (t, lab)


def observation_calendar_date(dt: datetime, row: dict | None = None) -> date:
    """UTC calendar date for same-day identity.

    Prefer the explicit daily ``date`` field when present so identity matches
    ``(ticker, reportedFiscalPeriodEnding, date)``.
    """
    if isinstance(row, dict):
        raw = row.get("date") or row.get("Date")
        parsed = parse_obs_datetime(raw) if raw else None
        if parsed is not None:
            return parsed.astimezone(timezone.utc).date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date()


def weekday_gap(obs_date: date, target_date: date) -> int:
    """Count Monday–Friday dates in ``(obs_date, target_date]``.

    Weekends never count. ``obs_date >= target_date`` → 0.
    """
    if obs_date >= target_date:
        return 0
    n = 0
    d = obs_date + timedelta(days=1)
    while d <= target_date:
        if d.weekday() < 5:  # Mon=0 .. Fri=4
            n += 1
        d += timedelta(days=1)
    return n


def baseline_gap_allowed(obs_date: date, target_date: date) -> bool:
    """Schedule-aware bounded-gap rule (fail-closed).

    Valid when weekday_gap(obs, target) ≤ MAX_BASELINE_WEEKDAY_GAP.
    Friday→Monday is valid; an ancient observation is not.
    """
    if obs_date > target_date:
        return False
    return weekday_gap(obs_date, target_date) <= MAX_BASELINE_WEEKDAY_GAP


def _row_update_time(row: Any) -> datetime | None:
    if not isinstance(row, dict):
        return None
    return parse_obs_datetime(row.get("updateTime") or row.get("Update Time"))


def _coerce_point(item) -> tuple[datetime, float | None, Any] | None:
    if item is None:
        return None
    if isinstance(item, dict):
        dt = parse_obs_datetime(
            item.get("date") or item.get("Date") or item.get("updateTime") or item.get("Update Time")
        )
        cons = to_num(item.get("consensus") if "consensus" in item else item.get("Current EPS"))
        if dt is None:
            return None
        return (dt, cons, item)
    try:
        dt = item[0]
        cons = item[1]
        row = item[2] if len(item) > 2 else {}
    except Exception:
        return None
    if not isinstance(dt, datetime):
        dt = parse_obs_datetime(dt)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if cons is not None and not isinstance(cons, (int, float)):
        cons = to_num(cons)
    return (dt, cons, row)


def normalize_same_day_observations(points: list | None) -> list[tuple[datetime, float | None, Any]]:
    """Collapse ``(identity already grouped, date)`` to one observation.

    Input order is append order. Winner: latest updateTime, else last append.
    Output is sorted by observation datetime.
    """
    coerced: list[tuple[int, datetime, float | None, Any]] = []
    for idx, raw in enumerate(points or []):
        pt = _coerce_point(raw)
        if pt is None:
            continue
        coerced.append((idx, pt[0], pt[1], pt[2]))
    by_day: dict[date, list[tuple[int, datetime, float | None, Any]]] = {}
    for item in coerced:
        d = observation_calendar_date(item[1], item[3] if isinstance(item[3], dict) else None)
        by_day.setdefault(d, []).append(item)
    collapsed: list[tuple[datetime, float | None, Any]] = []
    for _day, items in by_day.items():
        timed = [( _row_update_time(it[3]), it) for it in items]
        with_time = [(ts, it) for ts, it in timed if ts is not None]
        if with_time:
            # Latest updateTime; append index breaks ties.
            with_time.sort(key=lambda x: (x[0], x[1][0]))
            winner = with_time[-1][1]
        else:
            winner = items[-1]  # last append order
        collapsed.append((winner[1], winner[2], winner[3]))
    collapsed.sort(key=lambda x: x[0])
    return collapsed


def group_daily_by_fiscal_identity(daily_rows: list[dict] | None) -> dict[tuple[str, str], list]:
    """Group daily.jsonl observations by (ticker, reportedFiscalPeriodEnding).

    Preserves append order until same-day collapse, then one row per calendar
    date, sorted by datetime.
    """
    groups: dict[tuple[str, str], list] = {}
    for row in daily_rows or []:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker") or row.get("Ticker")
        fiscal = (
            row.get("reportedFiscalPeriodEnding")
            or row.get("reportedFiscalLabel")
            or row.get("fiscalKey")
            or row.get("Fiscal Year")
        )
        key = fiscal_identity_key(ticker, fiscal)
        if not key:
            continue
        cons = to_num(row.get("consensus") if "consensus" in row else row.get("Current EPS"))
        if cons is None:
            continue
        dt = parse_obs_datetime(
            row.get("date") or row.get("Date") or row.get("updateTime") or row.get("Update Time")
        )
        if dt is None:
            continue
        groups.setdefault(key, []).append((dt, cons, row))
    return {key: normalize_same_day_observations(pts) for key, pts in groups.items()}


def _unavailable_window(window_days: int, extra: dict | None = None, *, reason: str = "insufficient_history") -> dict:
    label = f"Internal {int(window_days)}D"
    out = {
        "windowDays": int(window_days),
        "windowLabel": label,
        "status": "unavailable",
        "reason": reason,
        "revisionPct": None,
        "startEps": None,
        "endEps": None,
        "startDate": None,
        "endDate": None,
        "anchorDate": None,
        "targetDate": None,
    }
    if extra:
        out.update(extra)
        # status/reason/pct stay unavailable unless extra explicitly overrides
        if "status" not in extra:
            out["status"] = "unavailable"
        if "revisionPct" not in extra:
            out["revisionPct"] = None
        if "reason" not in extra:
            out["reason"] = reason
    return out


def evaluate_internal_window(
    points: list | None,
    *,
    as_of: datetime,
    window_days: int,
) -> dict:
    """Evaluate one Internal N-day window.

    Returns ``{"window": <public dict>, "start": point|None, "latest": point|None}``.
    Point tuples are ``(datetime, eps, row)``.
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    pts = normalize_same_day_observations(list(points or []))
    empty = {
        "window": _unavailable_window(window_days),
        "start": None,
        "latest": None,
    }
    if not pts:
        return empty
    latest_candidates = [p for p in pts if p[0] <= as_of]
    if not latest_candidates:
        return empty
    latest = latest_candidates[-1]
    latest_cons = to_num(latest[1]) if not isinstance(latest[1], (int, float)) else (
        float(latest[1]) if math.isfinite(float(latest[1])) else None
    )
    anchor_date = observation_calendar_date(latest[0], latest[2] if isinstance(latest[2], dict) else None)
    target_date = anchor_date - timedelta(days=int(window_days))
    end_meta = {
        "endEps": latest_cons,
        "endDate": anchor_date.strftime("%Y-%m-%d"),
        "anchorDate": anchor_date.strftime("%Y-%m-%d"),
        "targetDate": target_date.strftime("%Y-%m-%d"),
    }

    before = []
    for p in pts:
        d = observation_calendar_date(p[0], p[2] if isinstance(p[2], dict) else None)
        if d <= target_date:
            before.append(p)
    start_pt = before[-1] if before else None
    if start_pt is not None:
        start_date = observation_calendar_date(
            start_pt[0], start_pt[2] if isinstance(start_pt[2], dict) else None
        )
        if not baseline_gap_allowed(start_date, target_date):
            start_pt = None

    if start_pt is None:
        return {
            "window": _unavailable_window(window_days, end_meta),
            "start": None,
            "latest": latest,
        }
    start_date = observation_calendar_date(
        start_pt[0], start_pt[2] if isinstance(start_pt[2], dict) else None
    )
    if anchor_date <= start_date:
        return {
            "window": _unavailable_window(window_days, end_meta),
            "start": start_pt,
            "latest": latest,
        }
    first = start_pt[1]
    last = latest_cons
    if first is not None and not isinstance(first, (int, float)):
        first = to_num(first)
    start_meta = {
        "startEps": first,
        "startDate": start_date.strftime("%Y-%m-%d"),
        **end_meta,
    }
    if first is None or last is None:
        return {
            "window": _unavailable_window(window_days, start_meta),
            "start": start_pt,
            "latest": latest,
        }
    if first == 0:
        return {
            "window": _unavailable_window(window_days, start_meta, reason="zero_baseline"),
            "start": start_pt,
            "latest": latest,
        }
    pct = (last - first) / abs(first) * 100.0
    if not math.isfinite(pct):
        return {
            "window": _unavailable_window(window_days, start_meta, reason="zero_baseline"),
            "start": start_pt,
            "latest": latest,
        }
    return {
        "window": {
            "windowDays": int(window_days),
            "windowLabel": f"Internal {int(window_days)}D",
            "status": "ok",
            "reason": None,
            "revisionPct": pct,
            **start_meta,
        },
        "start": start_pt,
        "latest": latest,
    }


def compute_internal_window(
    points: list | None,
    *,
    as_of: datetime,
    window_days: int,
    max_start_slack_days: int | None = None,  # noqa: ARG001 — removed; kept so old call sites do not explode
) -> dict:
    """Internal N-day consensus revision from real daily history.

    ``max_start_slack_days`` is ignored. Baseline admission is the
    schedule-aware weekday-gap rule documented in this module
    (``MAX_BASELINE_WEEKDAY_GAP``), not a fixed 2-day calendar slack.
    """
    return evaluate_internal_window(points, as_of=as_of, window_days=window_days)["window"]

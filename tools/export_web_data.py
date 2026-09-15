#!/usr/bin/env python3
"""Export snapshot + revisions + drivers into clean JSON for the web dashboard.

Never invents EPS. Missing numerics become null (frontend shows "—").
Preserves all history.jsonl rows including baselines.
Earnings digests persist under data/earnings/{T}.json across exports.
Daily consensus snapshots are append-only under data/daily_eps_snapshots/.
"""
from __future__ import annotations

import hashlib
import os
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from atomic_io import (
        JsonlAtomicError,
        PipelineLockedError,
        acquire_global_pipeline_lock,
        atomic_append_jsonl,
        atomic_write_text,
    )
except ImportError:
    import sys as _aio_sys
    _aio_sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import (
        JsonlAtomicError,
        PipelineLockedError,
        acquire_global_pipeline_lock,
        atomic_append_jsonl,
        atomic_write_text,
    )

try:
    from snapshot_quality import (
        MixedBuildError,
        check_build_generation_consistency,
        snapshot_quality_gate,
    )
except ImportError:
    import sys as _sq_sys
    _sq_sys.path.insert(0, str(Path(__file__).resolve().parent))
    from snapshot_quality import (
        MixedBuildError,
        check_build_generation_consistency,
        snapshot_quality_gate,
    )

# Resolve project root whether invoked as tools/ or web/tools/ (symlink)
_here = Path(__file__).resolve().parent
ROOT = _here.parent if (_here / "export_web_data.py").exists() and (_here.parent / "data" / "snapshots").exists() else _here
if not (ROOT / "data" / "snapshots").exists():
    cand = Path(__file__).resolve().parent
    for _ in range(5):
        if (cand / "data" / "snapshots").exists():
            ROOT = cand
            break
        cand = cand.parent
if not (ROOT / "data" / "snapshots").exists():
    raise SystemExit("Cannot locate project root with data/snapshots")

import sys as _sys
if str(_here) not in _sys.path:
    _sys.path.insert(0, str(_here))

SNAP_DIR = ROOT / "data" / "snapshots"
REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
DRIVERS_DIR = ROOT / "data" / "drivers"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
UNIVERSE_PATH = ROOT / "data" / "universe.json"
EARNINGS_DIR = ROOT / "data" / "earnings"
DAILY_SNAP_DIR = ROOT / "data" / "daily_eps_snapshots"
WEB_DATA = ROOT / "web" / "data"
DASH = ROOT / "dashboard"
CHECKPOINT_PATH = ROOT / "data" / "comparison_checkpoint.json"

TAIPEI = timezone(timedelta(hours=8))

TICKERS_DEFAULT = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]
# Fallback only — prefer display_mapped_years(taipei_year)
YEAR_KEYS = ["2026E", "2027E", "2028E", "2029E"]
CHART_YEARS = ["2026E", "2027E", "2028E"]
CY_TO_SLOT = {
    "CY2026": "2026E",
    "CY2027": "2027E",
    "CY2028": "2028E",
    "CY2029": "2029E",
}


def display_mapped_years(taipei_year: int | None = None, include_y3: bool = True) -> list[str]:
    """Taipei calendar year Y → Mapped Y, Y+1, Y+2 (and optional Y+3)."""
    if taipei_year is None:
        taipei_year = now_taipei().year
    years = [f"{taipei_year}E", f"{taipei_year + 1}E", f"{taipei_year + 2}E"]
    if include_y3:
        years.append(f"{taipei_year + 3}E")
    return years


def chart_years_from(display_years: list[str]) -> list[str]:
    """Chart series uses first three display years."""
    return list(display_years[:3])


def cy_to_slot_dynamic(align: str | None, display_years: list[str] | None = None) -> str | None:
    if not align:
        return None
    slot = CY_TO_SLOT.get(str(align))
    if slot:
        return slot
    m = re.match(r"CY(\d{4})", str(align))
    if m:
        return f"{m.group(1)}E"
    return None

MAPPING_RULE_NOTE = (
    "Slot mapping only (not true calendar-year EPS): Fiscal Period Ending Jan–Mar → "
    "prior calendar year slot; otherwise ending year slot. Reported Fiscal Period Ending "
    "labels are always preserved. True calendar-year EPS requires summing Q1+Q2+Q3+Q4 "
    "consensus with all four present (never interpolated)."
)

TRUE_CY_STATUS = "unavailable — need quarterly consensus"


def unavailable(x) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and math.isnan(x):
        return True
    if isinstance(x, str):
        s = x.strip()
        if s == "" or s.lower() in {
            "data unavailable",
            "n/a",
            "n/a (baseline)",
            "na",
            "null",
            "none",
        }:
            return True
    return False


def to_num(x):
    """Parse numbers; treat 'Data unavailable' / n/a as None. Keep 0.0."""
    if unavailable(x):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    if s.lower() in {"n/a", "na"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def to_display_str(x):
    """Keep human strings; map Data unavailable → None for JSON cleanliness."""
    if unavailable(x):
        return None
    return str(x).strip() if not isinstance(x, (int, float, bool)) else x


def load_latest_snapshot():
    dated = sorted(
        p
        for p in SNAP_DIR.glob("20*.json")
        if not p.name.startswith("raw_") and re.match(r"^\d{4}-\d{2}-\d{2}", p.name)
    )
    if not dated:
        raise SystemExit(f"No snapshot found in {SNAP_DIR}")
    path = dated[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def load_watchlist():
    if UNIVERSE_PATH.exists():
        u = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        tickers = u.get("tickers") or TICKERS_DEFAULT
        return list(tickers)
    return list(TICKERS_DEFAULT)


def load_history():
    rows = []
    if not REV_PATH.exists():
        return rows
    for line in REV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_drivers(ticker: str):
    p = DRIVERS_DIR / f"{ticker}.json"
    if not p.exists():
        return {"ticker": ticker, "updated": None, "status": None, "drivers": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ticker": ticker,
            "updated": None,
            "status": None,
            "drivers": [],
            "exportError": f"Parse error in data/drivers/{ticker}.json ({type(exc).__name__}); original kept",
            "corrupt": True,
        }


def load_alerts():
    if ALERTS_PATH.exists():
        data = json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "alerts" in data:
            return data
        if isinstance(data, list):
            return {"alerts": data}
    return {"alerts": []}


def pe(price, eps):
    """Forward PE. EPS <= 0 → N/M (None)."""
    p, e = to_num(price), to_num(eps)
    if p is None or e is None or e <= 0:
        return None
    return p / e


def eps_same(a, b) -> bool:
    """True if consensus values are equal for same-day snapshot dedupe."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return a == b


def compute_dispersion(high, low, consensus):
    """Dispersion (high-low)/consensus. Non-positive consensus → N/M (never negative ratio from sign flip)."""
    h, l, c = to_num(high), to_num(low), to_num(consensus)
    if h is None or l is None or c is None or c <= 0:
        return None
    return (h - l) / c


def parse_earnings_date_parts(raw) -> tuple[int, int, int] | None:
    if unavailable(raw):
        return None
    s = str(raw).strip()
    # Strip trailing session hints
    s = re.split(r"\s*\(", s, maxsplit=1)[0].strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return int(m.group(3)), int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def format_next_earnings_display(raw, status: str | None) -> str | None:
    if unavailable(raw):
        return None
    parts = parse_earnings_date_parts(raw)
    months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    if parts:
        y, mo, d = parts
        date_s = f"{months[mo - 1]} {d}, {y}"
    else:
        date_s = re.split(r"\s*\(", str(raw).strip(), maxsplit=1)[0].strip()
    st = (status or "estimated").lower()
    label = "Confirmed" if st == "confirmed" else "Estimated"
    return f"{date_s} · {label}"


def infer_next_earnings_status(next_raw, digest: dict | None = None) -> tuple[str | None, str | None]:
    """Return (status, source). Confirmed only with company IR evidence; SA dates → estimated."""
    if unavailable(next_raw):
        return None, None
    text = str(next_raw)
    # Digest may note IR confirmation
    if isinstance(digest, dict):
        blob = json.dumps(digest).lower()
        if (
            ("company ir" in blob or "investor relations" in blob)
            and ("confirmed" in blob or "announced" in blob)
        ) or digest.get("nextEarningsStatus") == "confirmed":
            src = digest.get("nextEarningsSource") or "Company IR"
            return "confirmed", src
        if digest.get("nextEarningsStatus") in ("confirmed", "estimated"):
            return digest["nextEarningsStatus"], digest.get("nextEarningsSource") or "Seeking Alpha"
    if "estimated" in text.lower():
        return "estimated", "Seeking Alpha"
    # No IR scrape yet → SA / third-party calendar dates are estimates
    return "estimated", "Seeking Alpha"


def ensure_driver_files(tickers: list[str]) -> list[dict]:
    """Ensure data/drivers/{T}.json exist from templates without wiping existing content.

    On corrupt JSON: preserve original bytes, write .corrupt-backup, surface error;
    NEVER overwrite with baseline template.
    Returns list of error dicts (empty when healthy).
    """
    DRIVERS_DIR.mkdir(parents=True, exist_ok=True)
    templates = {}
    tpl_path = DRIVERS_DIR / "templates.json"
    if tpl_path.exists():
        try:
            templates = json.loads(tpl_path.read_text(encoding="utf-8"))
        except Exception:
            templates = {}
    errors: list[dict] = []
    for t in tickers:
        p = DRIVERS_DIR / f"{t}.json"
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8")
                existing = json.loads(raw)
                if isinstance(existing, dict):
                    # Keep any valid JSON driver file (even empty drivers list)
                    continue
            except Exception as exc:
                backup = Path(str(p) + ".corrupt-backup")
                try:
                    if not backup.exists():
                        backup.write_bytes(p.read_bytes())
                except Exception:
                    pass
                err_msg = f"Parse error in data/drivers/{t}.json ({type(exc).__name__}); original kept"
                errors.append({"ticker": t, "error": err_msg, "path": str(p)})
                # NEVER overwrite with baseline template
                continue
        names = templates.get(t) or []
        payload = {
            "ticker": t,
            "updated": now_utc_iso(),
            "status": "Baseline — not yet scored vs prior earnings",
            "drivers": [
                {"name": n, "status": "unchanged", "note": "Awaiting first post-setup earnings refresh"}
                for n in names
            ],
        }
        write_json(p, payload)
    return errors


def growth_pct(a, b):
    """EPS growth ratio, or sentinel strings for zero-crossing.

    Crossing zero → "Turn profitable" / "Turn loss".
    Non-computable (None / start==0) → None (UI shows N/M).
    """
    a, b = to_num(a), to_num(b)
    if a is None or b is None:
        return None
    if a == 0:
        return None  # N/M
    if a < 0 < b:
        return "Turn profitable"
    if a > 0 > b:
        return "Turn loss"
    return (b - a) / abs(a)


def cagr_n_years(e_start, e_end, n_years: float = 2.0):
    """n-year CAGR. Start or end EPS <= 0 → N/M (None)."""
    a, b = to_num(e_start), to_num(e_end)
    if a is None or b is None or a <= 0 or b <= 0 or n_years <= 0:
        return None
    return (b / a) ** (1.0 / n_years) - 1.0


def cagr_2628(e26, e28):
    """Backward-compat alias (2-year CAGR). Prefer cagr_n_years."""
    return cagr_n_years(e26, e28, 2.0)


def taipei_display_safe(iso_utc: str) -> str | None:
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
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TAIPEI)
    months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    return f"{months[local.month - 1]} {local.day}, {local.year} {local.hour:02d}:{local.minute:02d} Taipei Time"


def now_taipei() -> datetime:
    """Current Taipei time. Tests may set AI_EPS_TAIPEI_YEAR to force the calendar year."""
    dt = datetime.now(TAIPEI)
    override = os.environ.get("AI_EPS_TAIPEI_YEAR") or os.environ.get("TAIPEI_YEAR_OVERRIDE")
    if override:
        try:
            y = int(override)
            dt = dt.replace(year=y)
        except Exception:
            pass
    return dt


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_dt(iso_utc: str) -> datetime | None:
    s = (iso_utc or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.strptime(iso_utc[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt



# --- Schedule-aware freshness (weekday 08:00 Taipei + grace) ---
COLLECTION_HOUR_TAIPEI = 8
COLLECTION_GRACE_HOURS = 6


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
    """Schedule-aware freshness shared by exporter meta and (mirrored) client.

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
    stale_after = None
    if due is not None:
        stale_after = (due + timedelta(hours=grace_hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    next_expected = nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if last_tp is None:
        return {
            "dataStale": True,
            "nextExpected": next_expected,
            "staleAfter": stale_after,
            "freshnessRule": "weekday_0800_taipei_plus_grace",
            "graceHours": grace_hours,
        }
    if due is None:
        # No due collection yet (e.g. Monday morning before 08:00+grace) → not stale if we have any success
        stale = False
    else:
        stale = last_tp < due
    return {
        "dataStale": stale,
        "nextExpected": next_expected,
        "staleAfter": stale_after,
        "freshnessRule": "weekday_0800_taipei_plus_grace",
        "graceHours": grace_hours,
        "lastDueCollectionStart": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if due else None,
    }


def is_schedule_stale(last_success_iso: str | None, now: datetime | None = None, grace_hours: int = COLLECTION_GRACE_HOURS) -> bool:
    return bool(compute_freshness(last_success_iso, now=now, grace_hours=grace_hours).get("dataStale"))


def parse_price_fields(price_raw, price_as_of) -> tuple:
    """Split regular-session lastClose from afterHours when price_as_of mentions post-market."""
    last_close = to_num(price_raw)
    after_hours = None
    as_of = to_display_str(price_as_of) or ""
    m = re.search(
        r"(?:post[- ]market|after[- ]hours|extended)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        as_of,
        re.IGNORECASE,
    )
    if m:
        after_hours = to_num(m.group(1))
    return last_close, after_hours


def fiscal_equals_calendar(reported_label: str | None) -> bool | None:
    """TSM Dec FY (and similar Dec endings) equal calendar year."""
    if not reported_label:
        return None
    return bool(re.match(r"^Dec\s+\d{4}$", reported_label.strip(), re.IGNORECASE))


def pack_eps_year(raw_year: dict | None, mapped_year: str | None = None) -> dict:
    raw_year = raw_year or {}
    cons = to_num(raw_year.get("consensus"))
    high = to_num(raw_year.get("high"))
    low = to_num(raw_year.get("low"))
    reported = to_display_str(raw_year.get("reported_fiscal_label"))
    # True CY EPS only if built from Q1+Q2+Q3+Q4 with all four present — not in snapshots.
    true_cy = None
    out = {
        "consensus": cons,
        "high": high,
        "low": low,
        "analysts": to_num(raw_year.get("analysts") if raw_year.get("analysts") is not None else raw_year.get("analystCount")),
        "dispersion": compute_dispersion(high, low, cons),
        "rev1M": to_num(raw_year.get("rev_1M_pct")),
        "rev3M": to_num(raw_year.get("rev_3M_pct")),
        "rev6M": to_num(raw_year.get("rev_6M_pct")),
        "reportedFiscalLabel": reported,
        "calendarAlignment": to_display_str(raw_year.get("calendar_alignment")),
        "mappedYear": mapped_year,
        "trueCalendarYearEps": true_cy,
        "slotMappingOnly": True,
    }
    analysts = out.get("analysts")
    out["analystCount"] = analysts
    if analysts == 0:
        out["coverageStatus"] = "warning"
        out["coverageWarning"] = "analystCount=0"
    elif analysts is None:
        out["coverageStatus"] = "unknown"
    else:
        out["coverageStatus"] = "ok"
    fec = fiscal_equals_calendar(reported)
    if fec is True:
        out["fiscalEqualsCalendar"] = True
    return out



MOMENTUM_FORMULA_DOC = (
    "Deterministic EPS momentum from mapped Y+1 and Y+2 SA 1M revision % only "
    "(no management guidance / drivers): "
    "both >= +3 → Strong Positive; both >= +1 → Positive; "
    "both <= -3 → Strong Negative; both <= -1 → Negative; else Neutral. "
    "Missing either leg → Neutral."
)


def compute_momentum_from_revisions(rev_y1, rev_y2) -> str:
    """Fixed formula from Y+1 / Y+2 1M revisions. No guidance/drivers."""
    a, b = to_num(rev_y1), to_num(rev_y2)
    if a is None or b is None:
        return "Neutral"
    if a >= 3.0 and b >= 3.0:
        return "Strong Positive"
    if a >= 1.0 and b >= 1.0:
        return "Positive"
    if a <= -3.0 and b <= -3.0:
        return "Strong Negative"
    if a <= -1.0 and b <= -1.0:
        return "Negative"
    return "Neutral"


def build_companies(snap: dict, tickers: list[str], year_keys: list[str] | None = None) -> dict:
    year_keys = year_keys or display_mapped_years()
    snap_utc = snap.get("snapshot_utc")
    out = {}
    for t in tickers:
        d = (snap.get("tickers") or {}).get(t) or {}
        eps_in = d.get("eps") or {}
        eps_out = {yk: pack_eps_year(eps_in.get(yk), mapped_year=yk) for yk in year_keys}
        # Primary fiscal identity map: reportedFiscalLabel → packed eps
        eps_by_fiscal: dict = {}
        for yk, packed in eps_out.items():
            lab = packed.get("reportedFiscalLabel")
            if lab:
                entry = dict(packed)
                entry["mappedYear"] = yk
                eps_by_fiscal[lab] = entry
        # Optionally index all_reported_rows only when they map into display years
        # (avoid polluting epsByFiscal with long SA period tables).
        for row in d.get("all_reported_rows") or []:
            if not isinstance(row, dict):
                continue
            lab = to_display_str(row.get("reported_fiscal_label") or row.get("Fiscal Year"))
            if not lab or lab in eps_by_fiscal:
                continue
            align = to_display_str(row.get("calendar_alignment") or row.get("Calendar Alignment"))
            mapped = cy_to_slot_dynamic(align, year_keys) if align else None
            if mapped not in year_keys:
                continue
            packed = pack_eps_year(row, mapped_year=mapped)
            eps_by_fiscal[lab] = packed

        last_close, after_hours = parse_price_fields(d.get("price"), d.get("price_as_of"))
        next_raw = to_display_str(d.get("next_earnings"))
        digest_hint = None
        earn_path = EARNINGS_DIR / f"{t}.json"
        if earn_path.exists():
            try:
                digest_hint = json.loads(earn_path.read_text(encoding="utf-8"))
            except Exception:
                digest_hint = None
        nestatus, nesource = infer_next_earnings_status(next_raw, digest_hint)
        next_display = format_next_earnings_display(next_raw, nestatus) if next_raw else None

        data_gaps = d.get("data_gaps") or []
        # Optional per-ticker failure: gaps that imply pull failure, or explicit flag
        collection_failed = bool(d.get("collection_failed")) or any(
            "pull fail" in str(g).lower() or "collection fail" in str(g).lower() or "fetch fail" in str(g).lower()
            for g in data_gaps
        )
        collection_as_of = to_display_str(d.get("collection_as_of")) or (
            None if collection_failed and d.get("last_successful_collection") else snap_utc
        )
        if collection_failed and d.get("last_successful_collection"):
            collection_as_of = to_display_str(d.get("last_successful_collection"))

        out[t] = {
            "ticker": t,
            "price": last_close,
            "lastClose": last_close,
            "afterHours": after_hours,
            "priceAsOf": to_display_str(d.get("price_as_of")),
            "lastEarnings": to_display_str(d.get("last_earnings")),
            "nextEarnings": next_display or next_raw,
            "nextEarningsRaw": next_raw,
            "nextEarningsStatus": nestatus,
            "nextEarningsSource": nesource,
            "fyNote": to_display_str(d.get("fy_note")),
            "momentum": (
                compute_momentum_from_revisions(
                    (eps_out.get(year_keys[1]) or {}).get("rev1M") if len(year_keys) > 1 else None,
                    (eps_out.get(year_keys[2]) or {}).get("rev1M") if len(year_keys) > 2 else None,
                )
                if len(year_keys) > 2
                else (to_display_str(d.get("eps_momentum")) or "Neutral")
            ),
            "momentumFormula": MOMENTUM_FORMULA_DOC,
            "eps": eps_out,
            "epsByFiscal": eps_by_fiscal,
            "sourceUrl": to_display_str(d.get("source_url")),
            "updateTime": to_display_str(d.get("update_time")) or snap_utc,
            "source": to_display_str(d.get("source")) or snap.get("source"),
            "dataGaps": data_gaps,
            "collectionAsOf": collection_as_of,
            "dataAsOf": collection_as_of,
            "lastSuccessfulCollection": collection_as_of,
            "collectionFailed": collection_failed,
            "usedLastKnownGood": bool(
                d.get("last_known_good")
                or d.get("used_last_known_good")
                or d.get("lkg")
                or d.get("usedLkg")
            )
            or (collection_failed and bool(d.get("last_successful_collection"))),
            "drivers": load_drivers(t),
        }
    return out


def build_valuation(
    companies: dict,
    tickers: list[str],
    snap_utc: str,
    year_keys: list[str] | None = None,
) -> list:
    """Period-based valuation rows.

    Primary schema:
      periods["YYYYE"] = {eps, pe, rev1M, reportedFiscalLabel, growthFromPrior, ...}
    Backward-compat flat keys (eps26/pe27/…) kept during transition when years match.
    """
    year_keys = year_keys or display_mapped_years()
    # Use first three display years for primary table (Y, Y+1, Y+2)
    display = list(year_keys[:3])
    rows = []
    for t in tickers:
        c = companies[t]
        price = c.get("lastClose") if c.get("lastClose") is not None else c.get("price")
        periods: dict = {}
        prev_cons = None
        for i, yk in enumerate(display):
            e = (c.get("eps") or {}).get(yk) or {}
            cons = e.get("consensus")
            entry = {
                "eps": cons,
                "pe": pe(price, cons),
                "rev1M": e.get("rev1M"),
                "reportedFiscalLabel": e.get("reportedFiscalLabel"),
                "trueCalendarYearEps": e.get("trueCalendarYearEps"),
                "growthFromPrior": growth_pct(prev_cons, cons) if i > 0 else None,
            }
            periods[yk] = entry
            prev_cons = cons

        cagr = None
        if len(display) >= 3:
            e0 = (periods.get(display[0]) or {}).get("eps")
            e2 = (periods.get(display[2]) or {}).get("eps")
            cagr = cagr_n_years(e0, e2, 2.0)

        row = {
            "ticker": t,
            "price": price,
            "lastClose": price,
            "afterHours": c.get("afterHours"),
            "periods": periods,
            "displayYears": display,
            "cagrY0Y2": cagr,
            "momentum": c["momentum"],
            "lastUpdated": snap_utc,
        }

        # Backward-compat flat keys for older UI / markdown during transition
        # Only emit when display years happen to be 2026E/2027E/2028E; otherwise
        # still emit index-based aliases so tests can migrate gradually.
        aliases = [
            ("eps26", "pe26", "reportedFy26", "trueCalendarYearEps26", "growth27", 0),
            ("eps27", "pe27", "reportedFy27", "trueCalendarYearEps27", "growth28", 1),
            ("eps28", "pe28", "reportedFy28", "trueCalendarYearEps28", None, 2),
        ]
        # Prefer year-suffix aliases from actual year numbers when possible
        for yk in display:
            m = re.match(r"(\d{4})E$", yk)
            if not m:
                continue
            yy = m.group(1)[2:]  # "26", "27", …
            p = periods[yk]
            row[f"eps{yy}"] = p.get("eps")
            row[f"pe{yy}"] = p.get("pe")
            row[f"reportedFy{yy}"] = p.get("reportedFiscalLabel")
            row[f"trueCalendarYearEps{yy}"] = p.get("trueCalendarYearEps")

        if len(display) >= 1:
            row["rev1M"] = (periods.get(display[1]) or periods.get(display[0]) or {}).get("rev1M")
            # Primary 1M rev on Y+1 (legacy behaviour used 2027E)
            if len(display) >= 2:
                row["rev1M"] = (periods[display[1]]).get("rev1M")
        if len(display) >= 3:
            row["rev1M28"] = (periods[display[2]]).get("rev1M")  # legacy name; value is Y+2 rev
            row[f"rev1M{display[2][:4]}"] = (periods[display[2]]).get("rev1M")
        if len(display) >= 2:
            row["growth27"] = (periods[display[1]]).get("growthFromPrior")
        if len(display) >= 3:
            row["growth28"] = (periods[display[2]]).get("growthFromPrior")
        row["cagr2628"] = cagr  # legacy alias for cagrY0Y2

        # Index-stable flat keys used by older summary cards (eps26=Y0 etc.)
        if len(display) >= 1:
            row["eps26"] = (periods[display[0]]).get("eps")
            row["pe26"] = (periods[display[0]]).get("pe")
            row["reportedFy26"] = (periods[display[0]]).get("reportedFiscalLabel")
        if len(display) >= 2:
            row["eps27"] = (periods[display[1]]).get("eps")
            row["pe27"] = (periods[display[1]]).get("pe")
            row["reportedFy27"] = (periods[display[1]]).get("reportedFiscalLabel")
        if len(display) >= 3:
            row["eps28"] = (periods[display[2]]).get("eps")
            row["pe28"] = (periods[display[2]]).get("pe")
            row["reportedFy28"] = (periods[display[2]]).get("reportedFiscalLabel")

        rows.append(row)
    return rows



def map_history_row(raw: dict) -> dict:
    prev = raw.get("Previous EPS")
    cur = raw.get("Current EPS")
    chg = raw.get("Change")
    rev = raw.get("Revision %")
    previous_eps = None if unavailable(prev) else to_num(prev)
    current_eps = to_num(cur)
    absolute_change = None if unavailable(chg) else to_num(chg)
    revision_pct = None if unavailable(rev) else to_num(rev)
    fiscal = raw.get("Fiscal Year")
    align = raw.get("Calendar Alignment")
    mapped = _slot_from_alignment(align) or cy_to_slot_dynamic(align)
    return {
        "date": raw.get("Date") or (raw.get("Update Time") or "")[:10],
        "ticker": raw.get("Ticker"),
        "fiscalYear": fiscal,  # primary identity with ticker
        "reportedFiscalLabel": fiscal,
        "fiscalKey": fiscal,
        "mappedYear": mapped,  # display mapping only
        "calendarAlignment": align,
        "previousEps": previous_eps,
        "currentEps": current_eps,
        "absoluteChange": absolute_change,
        "revisionPct": revision_pct,
        "reason": raw.get("Reason"),
        "source": raw.get("Source"),
        "updateTime": raw.get("Update Time"),
    }


def _slot_from_alignment(align: str | None) -> str | None:
    if not align:
        return None
    slot = CY_TO_SLOT.get(align)
    if slot:
        return slot
    m = re.match(r"CY(\d{4})", str(align))
    if m:
        return f"{m.group(1)}E"
    return None


def ticker_blocks_daily_observation(ticker: str, companies: dict, snap: dict) -> bool:
    """LKG / collection-failed tickers must not append a fake daily EPS observation."""
    c = companies.get(ticker) or {}
    d = ((snap.get("tickers") or {}).get(ticker) or {}) if isinstance(snap, dict) else {}
    if c.get("collectionFailed") or d.get("collection_failed"):
        return True
    flags = (
        c.get("usedLastKnownGood"),
        c.get("lastKnownGood"),
        d.get("last_known_good"),
        d.get("used_last_known_good"),
        d.get("lkg"),
        d.get("usedLkg"),
        d.get("from_lkg"),
    )
    if any(bool(x) for x in flags):
        return True
    return False


def seed_and_append_daily_snapshots(
    companies: dict,
    tickers: list[str],
    snap: dict,
    snap_path: Path,
    year_keys: list[str] | None = None,
) -> list[dict]:
    """Append-only daily consensus store.

    Primary identity: (date, ticker, reportedFiscalLabel).
    Stores ALL displayMappedYears (not just chart years).
    Same-day rules:
      - EPS unchanged vs last same-day record → do not append
      - EPS changed → append new record (keep prior same-day records)
    LKG / collection-failed tickers are skipped (no fake observation).
    JSONL writes are atomic fail-closed (no open('a') fallback).
    """
    year_keys = year_keys or display_mapped_years()
    DAILY_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = DAILY_SNAP_DIR / "daily.jsonl"

    existing: list[dict] = []
    last_consensus: dict[tuple, object] = {}
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            existing.append(row)
            fiscal = row.get("reportedFiscalLabel") or row.get("fiscalKey")
            key = (row.get("date"), row.get("ticker"), fiscal or row.get("slot"))
            last_consensus[key] = row.get("consensus")
            if row.get("slot"):
                last_consensus[(row.get("date"), row.get("ticker"), "slot:" + str(row.get("slot")))] = row.get("consensus")

    history_raw = load_history()
    new_rows: list[dict] = []
    for raw in history_raw:
        date = raw.get("Date") or (raw.get("Update Time") or "")[:10]
        ticker = raw.get("Ticker")
        fiscal = raw.get("Fiscal Year")
        slot = _slot_from_alignment(raw.get("Calendar Alignment")) or cy_to_slot_dynamic(
            raw.get("Calendar Alignment"), year_keys
        )
        if not date or not ticker:
            continue
        if not fiscal and (not slot or slot not in year_keys):
            continue
        identity = fiscal or slot
        key = (date, ticker, identity)
        if key in last_consensus:
            continue
        eps = to_num(raw.get("Current EPS"))
        row = {
            "date": date,
            "ticker": ticker,
            "slot": slot,
            "mappedYear": slot,
            "fiscalKey": fiscal,
            "consensus": eps,
            "reportedFiscalLabel": fiscal,
            "calendarAlignment": raw.get("Calendar Alignment"),
            "source": "seed_from_revision_history",
            "updateTime": raw.get("Update Time"),
        }
        new_rows.append(row)
        last_consensus[key] = eps

    snap_date = (snap.get("snapshot_utc") or snap_path.name)[:10]
    snap_utc = snap.get("snapshot_utc")
    for t in tickers:
        if ticker_blocks_daily_observation(t, companies, snap):
            continue
        c = companies.get(t) or {}
        for slot in year_keys:
            e = (c.get("eps") or {}).get(slot) or {}
            fiscal = e.get("reportedFiscalLabel")
            identity = fiscal or slot
            key = (snap_date, t, identity)
            new_eps = e.get("consensus")
            if key in last_consensus:
                if eps_same(last_consensus[key], new_eps):
                    continue
            analysts = e.get("analystCount") if e.get("analystCount") is not None else e.get("analysts")
            coverage = e.get("coverageStatus")
            if analysts == 0 and not coverage:
                coverage = "warning"
            row = {
                "date": snap_date,
                "ticker": t,
                "slot": slot,
                "mappedYear": slot,
                "fiscalKey": fiscal,
                "consensus": new_eps,
                "reportedFiscalLabel": fiscal,
                "calendarAlignment": e.get("calendarAlignment"),
                "analystCount": analysts,
                "coverageStatus": coverage,
                "source": "daily_export",
                "updateTime": snap_utc,
            }
            if analysts == 0:
                row["coverageWarning"] = e.get("coverageWarning") or "analystCount=0"
            new_rows.append(row)
            last_consensus[key] = new_eps

    if new_rows:
        atomic_append_jsonl(jsonl_path, new_rows)
        existing.extend(new_rows)

    day_payload = {
        "date": snap_date,
        "snapshotUtc": snap_utc,
        "tickers": {},
    }
    for t in tickers:
        if ticker_blocks_daily_observation(t, companies, snap):
            continue
        c = companies.get(t) or {}
        day_payload["tickers"][t] = {
            slot: {
                "consensus": ((c.get("eps") or {}).get(slot) or {}).get("consensus"),
                "reportedFiscalLabel": ((c.get("eps") or {}).get(slot) or {}).get("reportedFiscalLabel"),
                "calendarAlignment": ((c.get("eps") or {}).get(slot) or {}).get("calendarAlignment"),
                "mappedYear": slot,
                "analystCount": ((c.get("eps") or {}).get(slot) or {}).get("analystCount")
                or ((c.get("eps") or {}).get(slot) or {}).get("analysts"),
                "coverageStatus": ((c.get("eps") or {}).get(slot) or {}).get("coverageStatus"),
            }
            for slot in year_keys
        }
        by_fiscal = {}
        for slot in year_keys:
            e = (c.get("eps") or {}).get(slot) or {}
            lab = e.get("reportedFiscalLabel")
            if lab:
                by_fiscal[lab] = {
                    "consensus": e.get("consensus"),
                    "reportedFiscalLabel": lab,
                    "calendarAlignment": e.get("calendarAlignment"),
                    "mappedYear": slot,
                    "analystCount": e.get("analystCount") if e.get("analystCount") is not None else e.get("analysts"),
                    "coverageStatus": e.get("coverageStatus"),
                }
        day_payload["tickers"][t]["_byFiscal"] = by_fiscal
    write_json(DAILY_SNAP_DIR / f"{snap_date}.json", day_payload)

    return existing



def build_eps_history_from_daily(daily_rows: list[dict]) -> dict:
    """Build chart series from daily_eps_snapshots (not only revision events)."""
    hist: dict = {}
    # Deduplicate by (ticker, slot, date) keeping last write order
    by_key: dict[tuple, dict] = {}
    for r in daily_rows:
        t = r.get("ticker")
        slot = r.get("slot")
        date = r.get("date")
        if not t or not slot or not date:
            continue
        by_key[(t, slot, date)] = r
    for (t, slot, date), r in sorted(by_key.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        hist.setdefault(t, {}).setdefault(slot, []).append(
            {
                "date": date,
                "eps": r.get("consensus"),
                "reportedFiscalLabel": r.get("reportedFiscalLabel"),
            }
        )
    return hist


def _has_digest_content(obj: dict) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("hasDigest") is True:
        return True
    for key in ("positives", "negatives", "uncertainties", "qa", "results", "revenue", "eps", "margins", "sources"):
        val = obj.get(key)
        if isinstance(val, list) and len(val) > 0:
            return True
    for key in ("commentary", "guidance", "comparison"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return True
        if val is not None and not isinstance(val, (str, list)) and val != "":
            return True
    return False


def minimal_earnings_stub(companies: dict, ticker: str) -> dict:
    c = companies.get(ticker) or {}
    return {
        "ticker": ticker,
        "hasDigest": False,
        "lastEarnings": c.get("lastEarnings"),
        "nextEarnings": c.get("nextEarnings"),
        "nextEarningsRaw": c.get("nextEarningsRaw"),
        "nextEarningsStatus": c.get("nextEarningsStatus"),
        "nextEarningsSource": c.get("nextEarningsSource"),
        "revenue": [],
        "eps": [],
        "margins": [],
        "guidance": None,
        "comparison": None,
        "positives": [],
        "negatives": [],
        "uncertainties": [],
        "commentary": None,
        "qa": [],
        "results": [],
        "sources": [],
    }


def _try_last_known_good_earnings(ticker: str) -> dict | None:
    """Recover digest from prior web export if present."""
    web_earn = WEB_DATA / "earnings.json"
    if not web_earn.exists():
        return None
    try:
        prev = json.loads(web_earn.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(prev, dict) and isinstance(prev.get(ticker), dict):
        return prev[ticker]
    return None


def load_or_init_earnings(companies: dict, tickers: list[str]) -> dict:
    """Load persistent earnings files; write minimal stub once if missing/empty of digest fields.

    Never unconditionally overwrite files that already have digest content.
    On JSON parse error: NEVER write an empty stub over the file — keep original bytes,
    copy to {T}.json.corrupt-backup, and fall back to last-known-good web export if available.
    """
    EARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for t in tickers:
        path = EARNINGS_DIR / f"{t}.json"
        stub = minimal_earnings_stub(companies, t)
        if not path.exists():
            write_json(path, stub)
            out[t] = stub
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            raw_stripped = raw.strip()
            if not raw_stripped:
                write_json(path, stub)
                out[t] = stub
                continue
            data = json.loads(raw_stripped)
        except Exception as exc:
            # NEVER overwrite corrupt file with stub
            backup = path.with_name(path.name + ".corrupt-backup")
            try:
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
            except Exception:
                pass
            err_msg = f"Parse error in data/earnings/{t}.json ({type(exc).__name__}); original kept"
            fallback = _try_last_known_good_earnings(t)
            if fallback and _has_digest_content(fallback):
                data = dict(fallback)
                data["ticker"] = t
                data["hasDigest"] = True
                data["exportError"] = err_msg + "; using last-known-good from web/data/earnings.json"
                # Refresh next-earnings metadata from companies when missing
                for k in ("nextEarnings", "nextEarningsRaw", "nextEarningsStatus", "nextEarningsSource", "lastEarnings"):
                    if not data.get(k) and stub.get(k):
                        data[k] = stub[k]
                out[t] = data
            else:
                stub_err = dict(stub)
                stub_err["hasDigest"] = False
                stub_err["exportError"] = err_msg + "; no last-known-good digest"
                out[t] = stub_err
            continue

        # Attach / refresh next-earnings status fields from companies snapshot
        for k in ("nextEarnings", "nextEarningsRaw", "nextEarningsStatus", "nextEarningsSource"):
            if stub.get(k):
                # Prefer companies-derived status unless digest explicitly confirmed IR
                if k == "nextEarningsStatus" and data.get("nextEarningsStatus") == "confirmed":
                    continue
                if k in ("nextEarningsSource",) and data.get("nextEarningsStatus") == "confirmed":
                    continue
                data[k] = stub[k]

        if _has_digest_content(data):
            if not data.get("lastEarnings") and stub.get("lastEarnings"):
                data["lastEarnings"] = stub["lastEarnings"]
            data["ticker"] = t
            data["hasDigest"] = True
            # Ensure status fields present
            if not data.get("nextEarningsStatus") and stub.get("nextEarningsStatus"):
                data["nextEarningsStatus"] = stub["nextEarningsStatus"]
                data["nextEarningsSource"] = stub.get("nextEarningsSource")
            out[t] = data
            # Do NOT rewrite the file (preserve markers / user edits)
            continue

        # Empty stub: refresh last/next from companies but preserve any extra keys
        merged = dict(stub)
        for k, v in data.items():
            if k in ("lastEarnings", "nextEarnings", "nextEarningsRaw", "nextEarningsStatus", "nextEarningsSource") and stub.get(k):
                continue
            if k not in merged or merged[k] in (None, [], "", False):
                if v not in (None, [], ""):
                    merged[k] = v
        merged["lastEarnings"] = stub.get("lastEarnings")
        merged["nextEarnings"] = stub.get("nextEarnings")
        merged["nextEarningsRaw"] = stub.get("nextEarningsRaw")
        merged["nextEarningsStatus"] = stub.get("nextEarningsStatus")
        merged["nextEarningsSource"] = stub.get("nextEarningsSource")
        merged["hasDigest"] = False
        write_json(path, merged)
        out[t] = merged
    return out



def fmt_md_num(x, digits=2):
    v = to_num(x)
    if v is None:
        return "Data unavailable"
    return f"{v:.{digits}f}"



def regenerate_markdown_backups(companies: dict, valuation: list, snap: dict, tickers: list[str], year_keys: list[str] | None = None):
    """Thin optional backup regeneration of HOME.md / VALUATION.md.

    Uses displayMappedYears / valuation.periods — never hard-coded 2026E/2027E/2028E schema keys.
    """
    DASH.mkdir(parents=True, exist_ok=True)
    snap_utc = snap.get("snapshot_utc")
    source = snap.get("source", "Seeking Alpha")
    year_keys = list(year_keys or display_mapped_years())
    display = list(year_keys[:3])
    while len(display) < 3:
        display.append(None)
    y0, y1, y2 = display[0], display[1], display[2]

    def period_of(row: dict, yk: str | None) -> dict:
        if not yk:
            return {}
        periods = row.get("periods") or {}
        if yk in periods:
            return periods[yk] or {}
        return {}

    def row_eps(row, yk):
        p = period_of(row, yk)
        if p.get("eps") is not None:
            return p.get("eps")
        # legacy flat fallback only when years happen to match
        return None

    def row_pe(row, yk):
        p = period_of(row, yk)
        return p.get("pe")

    def row_fy(row, yk):
        return period_of(row, yk).get("reportedFiscalLabel")

    def row_growth(row, yk):
        return period_of(row, yk).get("growthFromPrior")

    vlines = [
        "# Valuation Dashboard",
        "",
        f"Source: {source} | Snapshot: {snap_utc}",
        "",
        "> Forward PE = Last Close / Consensus EPS (mapped FY slots). SA revision windows are **1M/3M/6M** (not 7D/30D/90D). "
        "Mapped columns are FY-mapped calendar slots only — not true calendar-year EPS. See per-ticker Reported FY labels.",
        "",
        f"| Ticker | Last Close | Mapped {y0} EPS | Mapped {y1} EPS | Mapped {y2} EPS | {y0} PE | {y1} PE | {y2} PE | {y1} Growth | {y2} Growth | 1M EPS Rev (SA) | EPS Momentum | Last Updated |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in valuation:
        g1 = row_growth(r, y1)
        g2 = row_growth(r, y2)
        if isinstance(g1, str):
            g27 = g1
        else:
            g27 = "Data unavailable" if g1 is None else f"{g1 * 100:+.2f}%"
        if isinstance(g2, str):
            g28 = g2
        else:
            g28 = "Data unavailable" if g2 is None else f"{g2 * 100:+.2f}%"
        rev_v = r.get("rev1M")
        if rev_v is None and y1:
            rev_v = period_of(r, y1).get("rev1M")
        rev = "Data unavailable" if rev_v is None else f"{rev_v:.2f}%"
        e0 = fmt_md_num(row_eps(r, y0))
        e1 = fmt_md_num(row_eps(r, y1))
        e2 = fmt_md_num(row_eps(r, y2))
        fy0, fy1, fy2 = row_fy(r, y0), row_fy(r, y1), row_fy(r, y2)
        if fy0:
            e0 = f"{e0} ({fy0})"
        if fy1:
            e1 = f"{e1} ({fy1})"
        if fy2:
            e2 = f"{e2} ({fy2})"
        price = r.get("lastClose") if r.get("lastClose") is not None else r.get("price")
        vlines.append(
            f"| {r['ticker']} | {fmt_md_num(price)} | {e0} | {e1} | {e2} | "
            f"{fmt_md_num(row_pe(r, y0))} | {fmt_md_num(row_pe(r, y1))} | {fmt_md_num(row_pe(r, y2))} | {g27} | {g28} | {rev} | {r.get('momentum')} | {snap_utc} |"
        )
    (DASH / "VALUATION.md").write_text("\n".join(vlines) + "\n", encoding="utf-8")

    ranked = []
    for r in valuation:
        v = r.get("rev1M")
        if v is None and y1:
            v = period_of(r, y1).get("rev1M")
        if v is not None:
            ranked.append((r["ticker"], v))
    ups = sorted([(t, v) for t, v in ranked if v > 0], key=lambda x: x[1], reverse=True)
    downs = sorted([(t, v) for t, v in ranked if v < 0], key=lambda x: x[1])
    ranked_y2 = []
    for r in valuation:
        v = period_of(r, y2).get("rev1M") if y2 else r.get("rev1M28")
        if v is not None:
            ranked_y2.append((r["ticker"], v))
    ups_y2 = sorted([(t, v) for t, v in ranked_y2 if v > 0], key=lambda x: x[1], reverse=True)

    hlines = [
        "# AI Investment EPS & Earnings Monitor — Home",
        "",
        f"Last updated: {snap_utc}  ",
        f"Primary estimate source this run: {source}",
        "",
        "## 1. Portfolio Monitor",
        "",
        f"| Ticker | Last Close | Mapped {y1} EPS | Reported FY label | {y1} PE | 1M EPS Rev (SA) | EPS Momentum | Last Earnings | Next Earnings |",
        "|---|---:|---:|---|---:|---:|---|---|---|",
    ]
    for t in tickers:
        c = companies[t]
        e1 = (c.get("eps") or {}).get(y1) or {}
        price = c.get("lastClose") if c.get("lastClose") is not None else c.get("price")
        pe1 = pe(price, e1.get("consensus"))
        rev = e1.get("rev1M")
        rev_s = "Data unavailable" if rev is None else f"{rev:.2f}%"
        hlines.append(
            f"| {t} | {fmt_md_num(price)} | {fmt_md_num(e1.get('consensus'))} | "
            f"{e1.get('reportedFiscalLabel') or 'Data unavailable'} | {fmt_md_num(pe1)} | {rev_s} | "
            f"{c.get('momentum')} | {c.get('lastEarnings') or 'Data unavailable'} | {c.get('nextEarnings') or 'Data unavailable'} |"
        )

    hlines += [
        "",
        "### FY-mapped calendar slots (Reported Fiscal Period Ending preserved; not true CY EPS)",
        "",
        f"| Ticker | Mapped {y0} (reported) | Mapped {y1} (reported) | Mapped {y2} (reported) |",
        "|---|---|---|---|",
    ]
    for t in tickers:
        c = companies[t]
        cells = []
        for yk in (y0, y1, y2):
            e = (c.get("eps") or {}).get(yk) or {}
            cons = e.get("consensus")
            lab = e.get("reportedFiscalLabel") or "Data unavailable"
            cons_s = "Data unavailable" if cons is None else f"{cons:.2f}"
            cells.append(f"{cons_s} ({lab})")
        hlines.append(f"| {t} | {cells[0]} | {cells[1]} | {cells[2]} |")

    hlines += ["", f"## 2. Largest {y1} EPS Upgrades (1M)", ""]
    if ups:
        for t, v in ups[:5]:
            hlines.append(f"- **{t}**: {v:.2f}% (1M)")
    else:
        hlines.append("- No positive 1M revisions in this snapshot.")

    hlines += ["", f"## 3. Largest {y1} EPS Downgrades (1M)", ""]
    if downs:
        for t, v in downs[:5]:
            hlines.append(f"- **{t}**: {v:.2f}% (1M)")
    else:
        hlines.append("- No negative 1M revisions in this snapshot.")

    hlines += ["", f"## 3b. Largest {y2} EPS Upgrades (1M)", ""]
    if ups_y2:
        for t, v in ups_y2[:5]:
            hlines.append(f"- **{t}**: {v:.2f}% (1M)")
    else:
        hlines.append(f"- No positive {y2} 1M revisions in this snapshot.")

    hlines += [
        "",
        "## 4. Latest Earnings Insights",
        "",
        "- Digests persist in `data/earnings/{TICKER}.json`. Investor-style fills populate after each report.",
        "",
        "## 5. Important Alerts",
        "",
        "- **None** — alerts only after material revision events (baselines preserved separately).",
        "",
        "## 6. EPS Revision History & Daily Snapshots",
        "",
        "- `data/revisions/history.jsonl` = revision_events (real changes + baselines).",
        "- `data/daily_eps_snapshots/` = append-only daily consensus for charts (even if unchanged).",
        "",
        "## Source discipline notes",
        "",
        "- SA does **not** expose 7D/30D/90D on these pages; recorded as Data unavailable; visible **1M/3M/6M %** stored separately.",
        "- Missing years are Data unavailable. Never backfilled from adjacent years.",
        f"- Mapping rule: {MAPPING_RULE_NOTE}",
        f"- True CY EPS status: {TRUE_CY_STATUS}",
        f"- Momentum: {MOMENTUM_FORMULA_DOC}",
        "- Fiscal vs calendar: see `sources/fiscal_year_map.md`.",
        "",
    ]
    (DASH / "HOME.md").write_text("\n".join(hlines), encoding="utf-8")


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def run_build_alerts() -> dict:
    """Alert Engine — only after a publishable persist. Never a pre-gate step."""
    try:
        import build_alerts as ba

        payload = ba.evaluate_alerts()
        ba.write_alerts(payload)
        return payload
    except Exception as exc:
        err = {
            "alerts": [],
            "activeAlerts": [],
            "alertHistory": [],
            "alertEngineLastEvaluated": now_utc_iso(),
            "alertEngineStatus": "error",
            "alertEngineError": f"{type(exc).__name__}: {exc}",
        }
        try:
            ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_json(ALERTS_PATH, err)
        except Exception:
            pass
        return err


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_data_version(payload_parts: dict) -> str:
    """sha256 of canonical JSON payload (sorted keys) across public data files."""
    h = hashlib.sha256()
    for key in sorted(payload_parts.keys()):
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        h.update(canonical_json_bytes(payload_parts[key]))
        h.update(b"\0")
    return h.hexdigest()


def load_prior_meta() -> dict:
    p = WEB_DATA / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_comparison_checkpoint() -> dict:
    """Previous successful-export baseline for What Changed. Not advanced on failure."""
    if not CHECKPOINT_PATH.exists():
        return {}
    try:
        obj = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def advance_comparison_checkpoint(
    companies: dict,
    *,
    data_version: str,
    build_id: str,
    snap_utc: str | None = None,
) -> dict:
    """Advance ONLY after a successful export write."""
    payload = {
        "dataVersion": data_version,
        "buildId": build_id,
        "snapshotUtc": snap_utc,
        "advancedAt": now_utc_iso(),
        "companies": companies,
    }
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(CHECKPOINT_PATH, payload)
    return payload


def compute_what_changed(
    prior_companies: dict | None,
    companies: dict,
    prior_alert_ids: set | None = None,
    new_alerts: list | None = None,
) -> list[dict]:
    """WHAT CHANGED vs the previous comparisonCheckpoint (not the in-flight export)."""
    changes: list[dict] = []
    prior_companies = prior_companies or {}
    prior_alert_ids = prior_alert_ids or set()
    for t, c in (companies or {}).items():
        if not isinstance(c, dict):
            continue
        pc = prior_companies.get(t) or {}
        if not isinstance(pc, dict):
            pc = {}
        for yk, e in (c.get("eps") or {}).items():
            if not isinstance(e, dict):
                continue
            pe = ((pc.get("eps") or {}).get(yk) or {}) if isinstance(pc.get("eps"), dict) else {}
            old_c, new_c = pe.get("consensus"), e.get("consensus")
            if old_c is not None and new_c is not None:
                try:
                    if abs(float(old_c) - float(new_c)) > 1e-9:
                        changes.append(
                            {
                                "ticker": t,
                                "kind": "eps",
                                "period": yk,
                                "previous": old_c,
                                "current": new_c,
                                "summary": f"{t} {yk} consensus {old_c} → {new_c}",
                            }
                        )
                except (TypeError, ValueError):
                    pass
            old_r = pe.get("rev1M")
            new_r = e.get("rev1M")
            if old_r is not None and new_r is not None:
                try:
                    if abs(float(old_r) - float(new_r)) > 1e-9:
                        changes.append(
                            {
                                "ticker": t,
                                "kind": "rev1M",
                                "period": yk,
                                "previous": old_r,
                                "current": new_r,
                                "summary": f"{t} {yk} SA 1M {old_r} → {new_r}",
                            }
                        )
                except (TypeError, ValueError):
                    pass
        old_n = pc.get("nextEarningsRaw") or pc.get("nextEarnings")
        new_n = c.get("nextEarningsRaw") or c.get("nextEarnings")
        if old_n and new_n and str(old_n) != str(new_n):
            changes.append(
                {
                    "ticker": t,
                    "kind": "nextEarnings",
                    "previous": old_n,
                    "current": new_n,
                    "summary": f"{t} next earnings {old_n} → {new_n}",
                }
            )
    for a in new_alerts or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if a.get("id") not in prior_alert_ids:
            changes.append(
                {
                    "ticker": a.get("ticker"),
                    "kind": "alert",
                    "id": a.get("id"),
                    "summary": a.get("message") or a.get("title"),
                }
            )
    return changes


def strip_operational_alert_fields(alerts_list: list) -> list:
    """Drop ageDays / other operational fields so dataVersion stays stable across calendar ticks."""
    out = []
    for a in alerts_list or []:
        if not isinstance(a, dict):
            out.append(a)
            continue
        b = {k: v for k, v in a.items() if k not in {"ageDays"}}
        out.append(b)
    return out


def collection_completeness(companies: dict, tickers: list[str]) -> dict:
    successful, failed = [], []
    for t in tickers:
        c = companies.get(t) or {}
        if c.get("collectionFailed"):
            failed.append(t)
        else:
            successful.append(t)
    total = len(tickers)
    ok_n = len(successful)
    if total == 0:
        status = "failed"
    elif ok_n == total:
        status = "complete"
    elif ok_n == 0:
        status = "failed"
    else:
        status = "partial"
    return {
        "collectionStatus": status,
        "successfulTickers": successful,
        "failedTickers": failed,
        "successfulCount": ok_n,
        "totalCount": total,
        "collectionStatusLabel": (
            f"{status.upper()} · {ok_n}/{total}" if status == "partial" else status.upper()
        ),
    }



def main(*, acquire_lock: bool = True):
    """Export after a publishable persist.

    Order inside a publishable run:
      persist daily (fresh tickers only) → Alert Engine → web JSON →
      dashboard.json/buildId → comparisonCheckpoint advance.

    Mixed-build or quality-gate reject aborts WITHOUT mutating Alert DB and
    WITHOUT advancing comparisonCheckpoint.
    """
    lock = None
    if acquire_lock:
        try:
            lock = acquire_global_pipeline_lock(ROOT)
        except PipelineLockedError as exc:
            raise SystemExit(f"PIPELINE LOCKED: {exc}") from exc

    try:
        # Reject mixed public generations before any persist / Alert Engine.
        try:
            check_build_generation_consistency(WEB_DATA)
        except MixedBuildError as exc:
            raise SystemExit(f"MIXED BUILD REJECTED: {exc}") from exc

        snap_path, snap = load_latest_snapshot()
        tickers = load_watchlist()

        gate = snapshot_quality_gate(snap, None)
        if not gate.get("ok") or not gate.get("publishable"):
            raise SystemExit(
                f"QUALITY GATE REJECT ({gate.get('failReason')}): "
                "abort export; Alert DB untouched; comparisonCheckpoint not advanced"
            )

        driver_errors = ensure_driver_files(tickers)
        snap_utc = snap.get("snapshot_utc") or ""
        source = snap.get("source") or "Seeking Alpha"
        display = taipei_display_safe(snap_utc)

        year_keys = display_mapped_years(now_taipei().year, include_y3=True)
        chart_years = chart_years_from(year_keys)

        prior_meta = load_prior_meta()
        checkpoint = load_comparison_checkpoint()
        prior_companies = checkpoint.get("companies") if isinstance(checkpoint.get("companies"), dict) else {}
        if not prior_companies:
            try:
                pc_path = WEB_DATA / "companies.json"
                if pc_path.exists():
                    prior_companies = json.loads(pc_path.read_text(encoding="utf-8"))
            except Exception:
                prior_companies = {}
        prior_alert_ids: set = set()
        try:
            pa_path = WEB_DATA / "alerts.json"
            if pa_path.exists():
                prev_alerts = json.loads(pa_path.read_text(encoding="utf-8"))
                prior_alert_ids = {
                    a.get("id")
                    for a in (prev_alerts.get("activeAlerts") or prev_alerts.get("alerts") or [])
                    if isinstance(a, dict) and a.get("id")
                }
        except Exception:
            prior_alert_ids = set()

        companies = build_companies(snap, tickers, year_keys=year_keys)
        valuation = build_valuation(companies, tickers, snap_utc, year_keys=year_keys)
        history_raw = load_history()
        revisions = [map_history_row(r) for r in history_raw]

        daily_rows = seed_and_append_daily_snapshots(
            companies, tickers, snap, snap_path, year_keys=year_keys
        )
        eps_history = build_eps_history_from_daily(daily_rows)

        earnings = load_or_init_earnings(companies, tickers)

        # Alert Engine — after persist, never pre-gate
        alerts_payload = run_build_alerts()
        active = alerts_payload.get("activeAlerts") or alerts_payload.get("alerts") or []
        history_alerts = alerts_payload.get("alertHistory") or active
        what_changed = compute_what_changed(prior_companies, companies, prior_alert_ids, active)
        alerts = {
            "activeAlerts": active,
            "alertHistory": history_alerts,
            "alertDiagnostics": alerts_payload.get("alertDiagnostics") or [],
            "whatChangedSinceLastCollection": what_changed,
            "changedSinceLastCollection": what_changed,
            "comparisonCheckpoint": {
                "dataVersion": checkpoint.get("dataVersion"),
                "buildId": checkpoint.get("buildId"),
                "advancedAt": checkpoint.get("advancedAt"),
            },
            "alerts": active,
            "alertEngineLastEvaluated": alerts_payload.get("alertEngineLastEvaluated"),
            "alertEngineStatus": alerts_payload.get("alertEngineStatus") or "error",
            "alertEngineError": alerts_payload.get("alertEngineError"),
            "oneShotActiveDays": alerts_payload.get("oneShotActiveDays"),
        }

        freshness = compute_freshness(snap_utc)
        data_stale = bool(freshness.get("dataStale"))

        site_published = prior_meta.get("sitePublished")
        site_published_display = prior_meta.get("sitePublishedDisplay")
        if not site_published:
            published_dt = now_taipei()
            site_published = published_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            site_published_display = taipei_display_safe(site_published)

        coll = collection_completeness(companies, tickers)
        refresh_version = hashlib.sha256(
            canonical_json_bytes(
                {
                    "lastSuccessfulCollection": snap_utc,
                    "alertEngineLastEvaluated": alerts.get("alertEngineLastEvaluated"),
                    "sitePublished": site_published,
                    "collectionStatus": coll.get("collectionStatus"),
                }
            )
        ).hexdigest()

        payload_parts = {
            "companies": companies,
            "valuation": {"rows": valuation},
            "revisions": {"revisions": revisions},
            "eps_history": eps_history,
            "earnings": earnings,
            "alerts": {
                "activeAlerts": strip_operational_alert_fields(active),
                "alertEngineStatus": alerts.get("alertEngineStatus"),
            },
            "watchlist": {"tickers": tickers},
        }
        data_version = compute_data_version(payload_parts)
        build_id = data_version

        quality_meta = {
            "status": gate.get("status") or "pass",
            "publishable": True,
            "reason": gate.get("failReason"),
            "extremeChanges": [
                q for q in (gate.get("quarantined") or []) if q.get("reason") == "extreme_eps_change"
            ],
            "quarantined": gate.get("quarantined") or [],
        }

        meta = {
            "lastUpdated": snap_utc,
            "lastUpdatedDisplay": display,
            "primarySource": source,
            "snapshotFile": snap_path.name,
            "revisionWindowNote": snap.get("revision_window_note"),
            "calendarAlignmentRule": snap.get("calendar_alignment_rule"),
            "mappingRule": MAPPING_RULE_NOTE,
            "trueCyStatus": TRUE_CY_STATUS,
            "momentumFormula": MOMENTUM_FORMULA_DOC,
            "consensusDataAsOf": snap_utc,
            "consensusDataAsOfDisplay": display,
            "lastSuccessfulCollection": snap_utc,
            "lastSuccessfulCollectionDisplay": display,
            "sitePublished": site_published,
            "sitePublishedDisplay": site_published_display,
            "dataStale": data_stale,
            "nextExpected": freshness.get("nextExpected"),
            "staleAfter": freshness.get("staleAfter"),
            "freshnessRule": freshness.get("freshnessRule"),
            "graceHours": freshness.get("graceHours"),
            "displayMappedYears": year_keys,
            "chartYears": chart_years,
            "dataVersion": data_version,
            "refreshVersion": refresh_version,
            "buildId": build_id,
            "alertEngineLastEvaluated": alerts.get("alertEngineLastEvaluated"),
            "alertEngineStatus": alerts.get("alertEngineStatus"),
            "alertEngineError": alerts.get("alertEngineError"),
            "oneShotActiveDays": alerts.get("oneShotActiveDays"),
            "collectionStatus": coll["collectionStatus"],
            "successfulTickers": coll["successfulTickers"],
            "failedTickers": coll["failedTickers"],
            "successfulCount": coll["successfulCount"],
            "totalCount": coll["totalCount"],
            "collectionStatusLabel": coll["collectionStatusLabel"],
            "driverExportErrors": driver_errors or None,
            "qualityGate": quality_meta,
            "comparisonCheckpoint": {
                "dataVersion": checkpoint.get("dataVersion"),
                "buildId": checkpoint.get("buildId"),
                "advancedAt": checkpoint.get("advancedAt"),
                "usedForWhatChanged": True,
            },
        }

        dashboard = {
            "buildId": build_id,
            "dataVersion": data_version,
            "snapshotFile": snap_path.name,
            "generatedAt": now_utc_iso(),
        }

        WEB_DATA.mkdir(parents=True, exist_ok=True)
        write_json(WEB_DATA / "watchlist.json", {"tickers": tickers})
        write_json(WEB_DATA / "companies.json", companies)
        write_json(WEB_DATA / "valuation.json", {"rows": valuation})
        write_json(WEB_DATA / "revisions.json", {"revisions": revisions})
        write_json(WEB_DATA / "eps_history.json", eps_history)
        write_json(WEB_DATA / "earnings.json", earnings)
        write_json(WEB_DATA / "alerts.json", alerts)
        write_json(WEB_DATA / "meta.json", meta)
        write_json(WEB_DATA / "dashboard.json", dashboard)

        regenerate_markdown_backups(companies, valuation, snap, tickers, year_keys=year_keys)

        # comparisonCheckpoint advances ONLY after a successful export.
        advance_comparison_checkpoint(
            companies, data_version=data_version, build_id=build_id, snap_utc=snap_utc
        )

        print(f"Exported web data → {WEB_DATA}")
        print(f"  snapshot: {snap_path.name}")
        print(f"  tickers: {', '.join(tickers)}")
        print(f"  displayMappedYears: {year_keys}")
        print(f"  consensusDataAsOfDisplay: {display}")
        print(f"  sitePublishedDisplay: {site_published_display}")
        print(f"  dataStale: {data_stale}")
        print(f"  dataVersion: {data_version[:12]}…")
        print(f"  refreshVersion: {refresh_version[:12]}…")
        print(f"  collectionStatus: {coll.get('collectionStatusLabel')}")
        print(f"  alertEngineStatus: {alerts.get('alertEngineStatus')} ({len(alerts.get('alerts') or [])} alerts)")
        print(f"  whatChanged: {len(what_changed)} vs previous checkpoint")
    except JsonlAtomicError:
        raise
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()

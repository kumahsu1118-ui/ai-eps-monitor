#!/usr/bin/env python3
"""Export snapshot + revisions + drivers into clean JSON for the web dashboard.

Never invents EPS. Missing numerics become null (frontend shows "—").
Preserves all history.jsonl rows including baselines.
Earnings digests persist under data/earnings/{T}.json across exports.
Daily consensus snapshots are append-only under data/daily_eps_snapshots/.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

from sa_parser import first_defined

SNAP_DIR = ROOT / "data" / "snapshots"
REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
DRIVERS_DIR = ROOT / "data" / "drivers"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
UNIVERSE_PATH = ROOT / "data" / "universe.json"
EARNINGS_DIR = ROOT / "data" / "earnings"
DAILY_SNAP_DIR = ROOT / "data" / "daily_eps_snapshots"
WEB_DATA = ROOT / "web" / "data"
DASH = ROOT / "dashboard"

TAIPEI = timezone(timedelta(hours=8))

TICKERS_DEFAULT = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]


def configured_taipei_year() -> int:
    """Taipei calendar year for display mapping. Override with AI_EPS_TAIPEI_YEAR."""
    env = os.environ.get("AI_EPS_TAIPEI_YEAR")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return now_taipei().year


def display_mapped_years(taipei_year: int | None = None, include_y3: bool = True) -> list[str]:
    """Taipei calendar year Y → Mapped Y, Y+1, Y+2 (and optional Y+3)."""
    if taipei_year is None:
        taipei_year = configured_taipei_year()
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
    m = re.match(r"CY(\d{4})", str(align))
    if m:
        return f"{m.group(1)}E"
    m2 = re.match(r"(\d{4})E$", str(align).strip())
    if m2:
        return f"{m2.group(1)}E"
    return None

MAPPING_RULE_NOTE = (
    "Slot mapping only (not true calendar-year EPS): Fiscal Period Ending Jan–Mar → "
    "prior calendar year slot; otherwise ending year slot. Reported Fiscal Period Ending "
    "labels are always preserved. True calendar-year EPS requires summing Q1+Q2+Q3+Q4 "
    "consensus with all four present (never interpolated)."
)

TRUE_CY_STATUS = "unavailable — need quarterly consensus"

MOMENTUM_FORMULA = (
    "Deterministic from Y+1 and Y+2 1M EPS revisions only: "
    "both >1% → Strong Positive; Y+1>0 and Y+2>=0 → Positive; "
    "both <-1% → Strong Negative; Y+1<0 and Y+2<=0 → Negative; else Neutral. "
    "No model, guidance, or driver mix-in."
)
NM = "N/M"
TURN_PROFITABLE = "Turn profitable"
TURN_LOSS = "Turn loss"


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
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "drivers" not in data:
            raise ValueError("invalid driver schema")
        return data
    except Exception as exc:
        bak = p.with_name(p.name + ".corrupt-backup")
        try:
            if not bak.exists():
                bak.write_bytes(p.read_bytes())
        except Exception:
            pass
        return {
            "ticker": ticker,
            "drivers": [],
            "exportError": f"Parse error in data/drivers/{ticker}.json ({type(exc).__name__}); original kept",
            "originalPreserved": True,
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
    p, e = to_num(price), to_num(eps)
    if p is None or e is None:
        return None
    if e <= 0:
        return NM
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
    h, l, c = to_num(high), to_num(low), to_num(consensus)
    if h is None or l is None or c is None:
        return None
    if c <= 0:
        return NM
    disp = (h - l) / c
    if disp < 0:
        return 0.0
    return disp


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

    On parse error: preserve original, write .corrupt-backup, return export error.
    NEVER silently reset to a baseline stub.
    """
    errors: list[dict] = []
    DRIVERS_DIR.mkdir(parents=True, exist_ok=True)
    templates = {}
    tpl_path = DRIVERS_DIR / "templates.json"
    if tpl_path.exists():
        try:
            templates = json.loads(tpl_path.read_text(encoding="utf-8"))
        except Exception:
            templates = {}
    for t in tickers:
        p = DRIVERS_DIR / f"{t}.json"
        if p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(existing, dict) or "drivers" not in existing:
                    raise ValueError("invalid driver schema")
                continue  # keep current content (including empty drivers list)
            except Exception as exc:
                bak = p.with_name(p.name + ".corrupt-backup")
                try:
                    if not bak.exists():
                        bak.write_bytes(p.read_bytes())
                except Exception:
                    pass
                errors.append(
                    {
                        "ticker": t,
                        "error": f"Parse error in data/drivers/{t}.json ({type(exc).__name__}); original kept",
                        "backup": bak.name,
                        "originalPreserved": True,
                    }
                )
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
    a, b = to_num(a), to_num(b)
    if a is None or b is None:
        return None
    if a <= 0 and b > 0:
        return TURN_PROFITABLE
    if a > 0 and b <= 0:
        return TURN_LOSS
    if a <= 0 and b <= 0:
        return NM
    return (b - a) / abs(a)


def cagr_over(first_eps, last_eps, years: int = 2):
    a, b = to_num(first_eps), to_num(last_eps)
    if a is None or b is None or years <= 0:
        return None
    if a <= 0 or b <= 0:
        return NM
    return (b / a) ** (1.0 / float(years)) - 1.0


def momentum_from_revisions(y1_1m, y2_1m) -> str:
    """Fixed formula from Y+1 / Y+2 1M revisions only."""
    y1 = to_num(y1_1m)
    y2 = to_num(y2_1m)
    if y1 is None:
        y1 = 0.0
    if y2 is None:
        y2 = 0.0
    if y1 > 1.0 and y2 > 1.0:
        return "Strong Positive"
    if y1 > 0 and y2 >= 0:
        return "Positive"
    if y1 < -1.0 and y2 < -1.0:
        return "Strong Negative"
    if y1 < 0 and y2 <= 0:
        return "Negative"
    return "Neutral"


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
    return datetime.now(TAIPEI)


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
        "analysts": first_defined(to_num(raw_year.get("analysts")), to_num(raw_year.get("nEst"))),
        "dispersion": compute_dispersion(high, low, cons),
        "rev1M": first_defined(
            to_num(raw_year.get("rev_1M_pct")),
            to_num(raw_year.get("rev1M")),
            to_num(raw_year.get("revision_1m")),
        ),
        "rev3M": first_defined(
            to_num(raw_year.get("rev_3M_pct")),
            to_num(raw_year.get("rev3M")),
            to_num(raw_year.get("revision_3m")),
        ),
        "rev6M": first_defined(
            to_num(raw_year.get("rev_6M_pct")),
            to_num(raw_year.get("rev6M")),
            to_num(raw_year.get("revision_6m")),
        ),
        "reportedFiscalLabel": reported,
        "calendarAlignment": to_display_str(raw_year.get("calendar_alignment")),
        "mappedYear": mapped_year,
        "trueCalendarYearEps": true_cy,
        "slotMappingOnly": True,
    }
    fec = fiscal_equals_calendar(reported)
    if fec is True:
        out["fiscalEqualsCalendar"] = True
    return out


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
        env_failed = {
            x.strip().upper()
            for x in (os.environ.get("AI_EPS_FAILED_TICKERS") or "").split(",")
            if x.strip()
        }
        collection_failed = bool(d.get("collection_failed")) or t in env_failed or any(
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
            "momentum": momentum_from_revisions(
                (eps_out.get(year_keys[1]) or {}).get("rev1M") if len(year_keys) > 1 else None,
                (eps_out.get(year_keys[2]) or {}).get("rev1M") if len(year_keys) > 2 else None,
            ),
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
            "drivers": load_drivers(t),
        }
    return out


def build_valuation(companies: dict, tickers: list[str], snap_utc: str, year_keys: list[str] | None = None) -> list:
    """Period-based valuation: periods[YYYYE].eps|.pe|.rev1M|.reportedFiscalLabel."""
    year_keys = year_keys or display_mapped_years()
    display3 = list(year_keys[:3])
    rows = []
    for t in tickers:
        c = companies[t]
        price = c.get("lastClose") if c.get("lastClose") is not None else c.get("price")
        periods = {}
        for yk in display3:
            e = (c.get("eps") or {}).get(yk) or {}
            periods[yk] = {
                "eps": e.get("consensus"),
                "pe": pe(price, e.get("consensus")),
                "rev1M": e.get("rev1M"),
                "reportedFiscalLabel": e.get("reportedFiscalLabel"),
                "trueCalendarYearEps": e.get("trueCalendarYearEps"),
            }
        growth = {}
        for i in range(1, len(display3)):
            prev_y, yk = display3[i - 1], display3[i]
            growth[yk] = growth_pct(periods[prev_y].get("eps"), periods[yk].get("eps"))
        cagr = None
        if len(display3) >= 3:
            cagr = cagr_over(periods[display3[0]].get("eps"), periods[display3[2]].get("eps"), years=2)
        y0, y1, y2 = (display3 + [None, None, None])[:3]
        rows.append(
            {
                "ticker": t,
                "price": price,
                "lastClose": price,
                "afterHours": c.get("afterHours"),
                "periods": periods,
                "growth": growth,
                "cagr": cagr,
                "cagrFrom": y0,
                "cagrTo": y2,
                "rev1M": (periods.get(y1) or {}).get("rev1M") if y1 else None,
                "momentum": c["momentum"],
                "lastUpdated": snap_utc,
                "displayYears": display3,
            }
        )
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
    return cy_to_slot_dynamic(align)


def seed_and_append_daily_snapshots(
    companies: dict,
    tickers: list[str],
    snap: dict,
    snap_path: Path,
    year_keys: list[str] | None = None,
) -> list[dict]:
    """Append-only daily consensus store.

    Primary identity: (date, ticker, reportedFiscalLabel).
    `slot` / mappedYear kept for display mapping only.
    Same-day rules:
      - EPS unchanged vs last same-day record → do not append
      - EPS changed → append new record (keep prior same-day records)
    Charts use the last chronologically per (ticker, slot, date).
    """
    year_keys = year_keys or display_mapped_years()
    chart_years = chart_years_from(year_keys)
    DAILY_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = DAILY_SNAP_DIR / "daily.jsonl"

    existing: list[dict] = []
    # Last consensus seen per key in file order (for same-day change detection)
    last_consensus: dict[tuple, object] = {}
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            existing.append(row)
            # Prefer fiscal identity; fall back to slot for legacy rows
            fiscal = row.get("reportedFiscalLabel") or row.get("fiscalKey")
            key = (row.get("date"), row.get("ticker"), fiscal or row.get("slot"))
            last_consensus[key] = row.get("consensus")
            if row.get("slot"):
                last_consensus[(row.get("date"), row.get("ticker"), "slot:" + str(row.get("slot")))] = row.get("consensus")

    # Seed from revision history dates (baselines / events) if key never seen
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
        if not fiscal and (not slot or slot not in chart_years):
            continue
        identity = fiscal or slot
        key = (date, ticker, identity)
        if key in last_consensus:
            continue
        eps = to_num(raw.get("Current EPS"))
        row = {
            "date": date,
            "ticker": ticker,
            "slot": slot,  # display mapping only
            "mappedYear": slot,
            "fiscalKey": fiscal,
            "consensus": eps,
            "epsMean": eps,
            "reportedFiscalLabel": fiscal,
            "reportedFiscalPeriodEnding": fiscal,
            "calendarAlignment": raw.get("Calendar Alignment"),
            "source": "seed_from_revision_history",
            "updateTime": raw.get("Update Time"),
        }
        new_rows.append(row)
        last_consensus[key] = eps

    # Snapshot-day consensus: append on first sight OR when EPS changed same day
    snap_date = (snap.get("snapshot_utc") or snap_path.name)[:10]
    snap_utc = snap.get("snapshot_utc")
    for t in tickers:
        c = companies.get(t) or {}
        for slot in chart_years:
            e = (c.get("eps") or {}).get(slot) or {}
            fiscal = e.get("reportedFiscalLabel")
            identity = fiscal or slot
            key = (snap_date, t, identity)
            new_eps = e.get("consensus")
            if key in last_consensus:
                if eps_same(last_consensus[key], new_eps):
                    continue  # unchanged → do not append
            row = {
                "date": snap_date,
                "ticker": t,
                "slot": slot,  # display mapping only
                "mappedYear": slot,
                "fiscalKey": fiscal,
                "consensus": new_eps,
                "epsMean": new_eps,
                "reportedFiscalLabel": fiscal,
                "reportedFiscalPeriodEnding": fiscal,
                "calendarAlignment": e.get("calendarAlignment"),
                "source": "daily_export",
                "updateTime": snap_utc,
            }
            new_rows.append(row)
            last_consensus[key] = new_eps

    if new_rows:
        with jsonl_path.open("a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                existing.append(row)

    # Dated JSON for the day reflecting latest values (overwrite that day file only)
    day_payload = {
        "date": snap_date,
        "snapshotUtc": snap_utc,
        "tickers": {},
    }
    for t in tickers:
        c = companies.get(t) or {}
        day_payload["tickers"][t] = {
            slot: {
                "consensus": ((c.get("eps") or {}).get(slot) or {}).get("consensus"),
                "reportedFiscalLabel": ((c.get("eps") or {}).get(slot) or {}).get("reportedFiscalLabel"),
                "calendarAlignment": ((c.get("eps") or {}).get(slot) or {}).get("calendarAlignment"),
                "mappedYear": slot,
            }
            for slot in chart_years
        }
        # Also expose by fiscal label for identity stability
        by_fiscal = {}
        for slot in chart_years:
            e = (c.get("eps") or {}).get(slot) or {}
            lab = e.get("reportedFiscalLabel")
            if lab:
                by_fiscal[lab] = {
                    "consensus": e.get("consensus"),
                    "reportedFiscalLabel": lab,
                    "calendarAlignment": e.get("calendarAlignment"),
                    "mappedYear": slot,
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
    for t, dig in list(out.items()):
        out[t] = normalize_consensus_comparisons(dig)
    return out



def normalize_consensus_comparisons(digest: dict) -> dict:
    """Split actuals vs consensusComparison provenance. Do not mix them."""
    if not isinstance(digest, dict):
        return digest
    results = digest.get("results") if isinstance(digest.get("results"), dict) else {}
    gd = digest.get("guidanceDetail") if isinstance(digest.get("guidanceDetail"), dict) else {}
    results_vs = results.get("vsConsensus") or digest.get("resultsVsConsensus")
    cc = digest.get("consensusComparison") if isinstance(digest.get("consensusComparison"), dict) else {}
    if cc.get("vsConsensus"):
        results_vs = cc.get("vsConsensus") or results_vs
    guidance_vs = gd.get("vsConsensus") or digest.get("guidanceVsConsensus")
    digest["resultsVsConsensus"] = results_vs
    digest["guidanceVsConsensus"] = guidance_vs

    if not isinstance(digest.get("actuals"), dict):
        digest["actuals"] = {
            "sourceUrl": results.get("sourceUrl") if results.get("sourceTier") in (1, 2, 3) else None,
            "sourceTier": results.get("sourceTier") if results.get("sourceTier") in (1, 2, 3) else None,
            "source": results.get("source") or "Company IR",
            "epsActual": results.get("eps"),
            "revenue": results.get("revenue"),
        }
    if not isinstance(digest.get("consensusComparison"), dict) or not digest["consensusComparison"].get("sourceUrl"):
        sa_url = cc.get("sourceUrl")
        sa_tier = cc.get("sourceTier")
        if not sa_url:
            for item in (digest.get("positives") or []):
                if isinstance(item, dict) and item.get("url") and (item.get("sourceTier") is None or int(item.get("sourceTier") or 4) >= 4):
                    sa_url = item.get("url")
                    sa_tier = item.get("sourceTier") or 4
                    break
        digest["consensusComparison"] = {
            "sourceUrl": sa_url,
            "sourceTier": int(sa_tier) if sa_tier is not None else 4,
            "source": cc.get("source") or "Seeking Alpha",
            "vsConsensus": results_vs,
            "notes": cc.get("notes") or results.get("notes"),
        }
    return digest


def fmt_md_num(x, digits=2):
    if isinstance(x, str) and x in {NM, TURN_PROFITABLE, TURN_LOSS}:
        return x
    v = to_num(x)
    if v is None:
        return "Data unavailable"
    return f"{v:.{digits}f}"


def fmt_md_growth(g):
    if g is None:
        return "Data unavailable"
    if isinstance(g, str):
        return g
    try:
        return f"{g * 100:+.2f}%"
    except TypeError:
        return str(g)


def regenerate_markdown_backups(companies: dict, valuation: list, snap: dict, tickers: list[str], year_keys: list[str] | None = None):
    """Thin optional backup regeneration of HOME.md / VALUATION.md."""
    DASH.mkdir(parents=True, exist_ok=True)
    snap_utc = snap.get("snapshot_utc")
    source = snap.get("source", "Seeking Alpha")
    years = list((year_keys or display_mapped_years())[:3])
    y0, y1, y2 = (years + ["", "", ""])[:3]

    def period_of(row: dict, yk: str) -> dict:
        return ((row.get("periods") or {}).get(yk)) or {}

    eps_hdr = " | ".join(f"Mapped {y} EPS" for y in years)
    pe_hdr = " | ".join(f"{y} PE" for y in years)
    growth_hdr = " | ".join(f"{y} Growth" for y in years[1:])
    vlines = [
        "# Valuation Dashboard",
        "",
        f"Source: {source} | Snapshot: {snap_utc}",
        "",
        "> Forward PE = Last Close / Consensus EPS (mapped FY slots). SA revision windows are **1M/3M/6M** (not 7D/30D/90D). "
        "Mapped columns are FY-mapped calendar slots only — not true calendar-year EPS. See per-ticker Reported FY labels.",
        "",
        f"| Ticker | Last Close | {eps_hdr} | {pe_hdr} | {growth_hdr} | 1M EPS Rev (SA) | EPS Momentum | Last Updated |",
        "|" + "|".join(["---", "---:"] + ["---:"] * (len(years) * 2 + max(0, len(years) - 1) + 2) + ["---|---"]) + "|",
    ]
    for r in valuation:
        periods = r.get("periods") or {}
        growth = r.get("growth") or {}
        eps_cells = []
        pe_cells = []
        for yk in years:
            p = periods.get(yk) or {}
            e = fmt_md_num(p.get("eps"))
            if p.get("reportedFiscalLabel"):
                e = f"{e} ({p['reportedFiscalLabel']})"
            eps_cells.append(e)
            pe_cells.append(fmt_md_num(p.get("pe")))
        g_cells = []
        for yk in years[1:]:
            g = growth.get(yk)
            g_cells.append(fmt_md_growth(g))
        rev = r.get("rev1M")
        rev_s = "Data unavailable" if rev is None else f"{rev:.2f}%"
        price = r.get("lastClose") if r.get("lastClose") is not None else r.get("price")
        vlines.append(
            f"| {r['ticker']} | {fmt_md_num(price)} | "
            + " | ".join(eps_cells + pe_cells + g_cells)
            + f" | {rev_s} | {r['momentum']} | {snap_utc} |"
        )
    (DASH / "VALUATION.md").write_text("\n".join(vlines) + "\n", encoding="utf-8")

    def y1_rev(row):
        return (period_of(row, y1) or {}).get("rev1M") if y1 else row.get("rev1M")

    ranked = [(r["ticker"], y1_rev(r)) for r in valuation if y1_rev(r) is not None]
    ups = sorted([(t, v) for t, v in ranked if v > 0], key=lambda x: x[1], reverse=True)
    downs = sorted([(t, v) for t, v in ranked if v < 0], key=lambda x: x[1])

    def y2_rev(row):
        return (period_of(row, y2) or {}).get("rev1M") if y2 else None

    ranked2 = [(r["ticker"], y2_rev(r)) for r in valuation if y2_rev(r) is not None]
    ups2 = sorted([(t, v) for t, v in ranked2 if v > 0], key=lambda x: x[1], reverse=True)

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
            f"{c['momentum']} | {c.get('lastEarnings') or 'Data unavailable'} | {c.get('nextEarnings') or 'Data unavailable'} |"
        )

    mapped_hdr = " | ".join(f"Mapped {yk} (reported)" for yk in years)
    hlines += [
        "",
        "### FY-mapped calendar slots (Reported Fiscal Period Ending preserved; not true CY EPS)",
        "",
        f"| Ticker | {mapped_hdr} |",
        "|" + "|".join(["---"] * (1 + len(years))) + "|",
    ]
    for t in tickers:
        c = companies[t]
        cells = []
        for yk in years:
            e = (c.get("eps") or {}).get(yk) or {}
            cons = e.get("consensus")
            lab = e.get("reportedFiscalLabel") or "Data unavailable"
            cons_s = "Data unavailable" if cons is None else f"{cons:.2f}"
            cells.append(f"{cons_s} ({lab})")
        hlines.append(f"| {t} | " + " | ".join(cells) + " |")

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
    if ups2:
        for t, v in ups2[:5]:
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
        "- See `data/alerts/index.json` (activeAlerts + alertHistory).",
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
        "- Fiscal vs calendar: see `sources/fiscal_year_map.md`.",
        "",
    ]
    (DASH / "HOME.md").write_text("\n".join(hlines), encoding="utf-8")


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_build_alerts() -> dict:
    """Call deterministic alert engine before writing web/data/alerts.json."""
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
            "alertDiagnostics": [],
            "driverLoadErrors": [],
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


def alerts_for_data_version(alerts: dict) -> dict:
    """Substantive alert payload: ids/eventAt/kind — not ageDays or evaluation ticks."""

    def strip_one(a):
        if not isinstance(a, dict):
            return a
        return {k: v for k, v in a.items() if k not in {"ageDays", "lastEvaluatedAt", "alertEngineLastEvaluated"}}

    return {
        "alerts": [strip_one(a) for a in (alerts.get("alerts") or [])],
        "activeAlerts": [strip_one(a) for a in (alerts.get("activeAlerts") or [])],
        "alertEngineStatus": alerts.get("alertEngineStatus"),
    }


def compute_refresh_version(generated_at, last_success, collection_status) -> str:
    blob = json.dumps(
        {
            "generatedAt": generated_at,
            "lastSuccessfulCollection": last_success,
            "collectionStatus": collection_status,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_prior_meta() -> dict:
    p = WEB_DATA / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    snap_path, snap = load_latest_snapshot()
    tickers = load_watchlist()
    driver_errors = ensure_driver_files(tickers)
    snap_utc = snap.get("snapshot_utc") or ""
    source = snap.get("source") or "Seeking Alpha"
    display = taipei_display_safe(snap_utc)

    year_keys = display_mapped_years(configured_taipei_year(), include_y3=True)
    chart_years = chart_years_from(year_keys)

    prior_meta = load_prior_meta()

    companies = build_companies(snap, tickers, year_keys=year_keys)
    valuation = build_valuation(companies, tickers, snap_utc, year_keys=year_keys)
    history_raw = load_history()
    revisions = [map_history_row(r) for r in history_raw]

    daily_rows = seed_and_append_daily_snapshots(
        companies, tickers, snap, snap_path, year_keys=year_keys
    )
    eps_history = build_eps_history_from_daily(daily_rows)

    earnings = load_or_init_earnings(companies, tickers)

    # Deterministic alert engine — must run before writing alerts.json
    alerts_payload = run_build_alerts()
    active = alerts_payload.get("activeAlerts") or alerts_payload.get("alerts") or []
    driver_errors = list(driver_errors or []) + list(alerts_payload.get("driverLoadErrors") or [])
    alerts = {
        "alerts": active,
        "activeAlerts": active,
        "alertHistory": alerts_payload.get("alertHistory") or [],
        "alertDiagnostics": alerts_payload.get("alertDiagnostics") or [],
        "driverLoadErrors": driver_errors,
        "alertEngineLastEvaluated": alerts_payload.get("alertEngineLastEvaluated"),
        "alertEngineStatus": alerts_payload.get("alertEngineStatus") or "error",
        "alertEngineError": alerts_payload.get("alertEngineError"),
    }
    if driver_errors and alerts.get("alertEngineStatus") == "ok":
        alerts["alertEngineStatus"] = "error"
        alerts["alertEngineError"] = "; ".join(
            e.get("error") or str(e) for e in driver_errors
        )

    from freshness import is_schedule_stale as _is_schedule_stale

    consensus_dt = parse_iso_dt(snap_utc)
    now_dt = datetime.now(timezone.utc)
    data_stale = _is_schedule_stale(consensus_dt, now=now_dt)

    # Per-ticker schedule freshness (same weekday 08:00 Taipei rule)
    for t, c in companies.items():
        last = parse_iso_dt(c.get("lastSuccessfulCollection") or c.get("collectionAsOf") or snap_utc)
        ticker_stale = bool(c.get("collectionFailed")) or _is_schedule_stale(last, now=now_dt)
        c["scheduleStale"] = ticker_stale
        c["dataStale"] = ticker_stale

    failed_tickers = [t for t in tickers if (companies.get(t) or {}).get("collectionFailed")]
    successful_tickers = [t for t in tickers if t not in failed_tickers]
    if not successful_tickers:
        collection_status = "failed"
    elif failed_tickers:
        collection_status = "partial"
    else:
        collection_status = "complete"

    # Preserve prior sitePublished until publish actually ships a new payload
    site_published = prior_meta.get("sitePublished")
    site_published_display = prior_meta.get("sitePublishedDisplay")
    if not site_published:
        # First export: stamp once; publish script may refresh when hash changes
        published_dt = now_taipei()
        site_published = published_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        site_published_display = taipei_display_safe(site_published)

    generated_at = now_utc_iso()
    last_success = snap_utc
    refresh_version = compute_refresh_version(generated_at, last_success, collection_status)

    # dataVersion = substantive EPS/price/earnings/active-alert payload.
    # ageDays / lastEvaluated ticks / refresh clocks must NOT bump it.
    payload_parts = {
        "companies": companies,
        "valuation": {"rows": valuation},
        "revisions": {"revisions": revisions},
        "eps_history": eps_history,
        "earnings": earnings,
        "alerts": alerts_for_data_version(alerts),
        "watchlist": {"tickers": tickers},
    }
    data_version = compute_data_version(payload_parts)
    build_id = data_version

    meta = {
        "lastUpdated": snap_utc,
        "lastUpdatedDisplay": display,
        "primarySource": source,
        "snapshotFile": snap_path.name,
        "revisionWindowNote": snap.get("revision_window_note"),
        "calendarAlignmentRule": snap.get("calendar_alignment_rule"),
        "mappingRule": MAPPING_RULE_NOTE,
        "trueCyStatus": TRUE_CY_STATUS,
        "consensusDataAsOf": snap_utc,
        "consensusDataAsOfDisplay": display,
        "lastSuccessfulCollection": last_success,
        "lastSuccessfulCollectionDisplay": display,
        "sitePublished": site_published,
        "sitePublishedDisplay": site_published_display,
        "dataStale": data_stale,
        "freshnessPolicy": "weekday_0800_taipei_grace_2h",
        "displayMappedYears": year_keys,
        "chartYears": chart_years,
        "dataVersion": data_version,
        "refreshVersion": refresh_version,
        "buildId": build_id,
        "generatedAt": generated_at,
        "momentumFormula": MOMENTUM_FORMULA,
        "collectionStatus": collection_status,
        "successfulTickers": successful_tickers,
        "failedTickers": failed_tickers,
        "successfulCount": len(successful_tickers),
        "totalCount": len(tickers),
        "driverLoadErrors": driver_errors,
        "alertEngineLastEvaluated": alerts.get("alertEngineLastEvaluated"),
        "alertEngineStatus": alerts.get("alertEngineStatus"),
        "alertEngineError": alerts.get("alertEngineError"),
    }

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    write_json(WEB_DATA / "watchlist.json", {"tickers": tickers})
    write_json(WEB_DATA / "meta.json", meta)
    write_json(WEB_DATA / "companies.json", companies)
    write_json(WEB_DATA / "valuation.json", {"rows": valuation})
    write_json(WEB_DATA / "revisions.json", {"revisions": revisions})
    write_json(WEB_DATA / "eps_history.json", eps_history)
    write_json(WEB_DATA / "earnings.json", earnings)
    write_json(WEB_DATA / "alerts.json", alerts)

    regenerate_markdown_backups(companies, valuation, snap, tickers, year_keys=year_keys)

    print(f"Exported web data → {WEB_DATA}")
    print(f"  snapshot: {snap_path.name}")
    print(f"  tickers: {', '.join(tickers)}")
    print(f"  displayMappedYears: {year_keys}")
    print(f"  consensusDataAsOfDisplay: {display}")
    print(f"  sitePublishedDisplay: {site_published_display}")
    print(f"  dataStale: {data_stale}")
    print(f"  collectionStatus: {collection_status} · {len(successful_tickers)}/{len(tickers)}")
    print(f"  dataVersion: {data_version[:12]}…")
    print(f"  refreshVersion: {refresh_version[:12]}…")
    print(f"  alertEngineStatus: {alerts.get('alertEngineStatus')} ({len(alerts.get('alerts') or [])} alerts)")



if __name__ == "__main__":
    main()

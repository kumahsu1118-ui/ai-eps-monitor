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
SCHEMA_VERSION = "1"
# Pure-build mode (set by CLI / ingest): no formal persistent DB mutation
NO_PERSISTENT_MUTATION = False
STAGE_DIR: Path | None = None

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
    """Parse numbers; treat 'Data unavailable' / n/a as None. Keep 0.0.

    Reject non-finite (NaN/Infinity) — math.isfinite only.
    """
    if unavailable(x):
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    s = str(x).strip().replace(",", "").replace("$", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    if s.lower() in {"n/a", "na", "nan", "inf", "-inf", "+inf", "infinity", "-infinity"}:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def to_display_str(x):
    """Keep human strings; map Data unavailable → None for JSON cleanliness."""
    if unavailable(x):
        return None
    return str(x).strip() if not isinstance(x, (int, float, bool)) else x


def snapshot_sort_key(path: Path) -> str:
    """Sort key for snapshot files: prefer full UTC timestamp identity."""
    name = path.stem  # e.g. 2026-09-15T013600Z or 2026-09-15
    return name


def list_full_snapshots() -> list[Path]:
    """Validated snapshots only (same-day timestamped OK).

    Excludes raw_/quarantine/latest/manifest and non-publishable stamped files.
    load_latest_snapshot() and history comparison read ONLY these.
    """
    try:
        import snapshot_quality as sq
        return sq.list_validated_snapshots(SNAP_DIR)
    except Exception:
        out = []
        for p in SNAP_DIR.glob("20*.json"):
            if p.name.startswith("raw_"):
                continue
            if p.name in {"latest.json", "manifest.json"}:
                continue
            if "quarantine" in p.parts:
                continue
            if re.match(r"^\d{4}-\d{2}-\d{2}(T\d{6}Z)?(_[A-Za-z0-9]+)?(_\d+)?\.json$", p.name):
                out.append(p)
        return sorted(out, key=snapshot_sort_key)


def persist_full_snapshot(snap: dict, snap_utc: str | None = None) -> Path:
    """Write full snapshot as UTC timestamp file; never overwrite first same-day snapshot.

    Also updates latest.json + manifest.json for convenience.
    """
    from atomic_io import atomic_write_json
    utc = snap_utc or snap.get("snapshot_utc") or ""
    # Normalize to 2026-09-15T013600Z
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})Z$", str(utc))
    if m:
        fname = f"{m.group(1)}T{m.group(2)}{m.group(3)}{m.group(4)}Z.json"
    else:
        day = str(utc)[:10] if utc else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fname = f"{day}.json"
    path = SNAP_DIR / fname
    SNAP_DIR.mkdir(parents=True, exist_ok=True)

    def _content_hash(obj: dict) -> str:
        payload = {k: v for k, v in (obj or {}).items() if k not in {"qualityGate", "runId", "runStatus"}}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]

    if path.exists():
        # Immutable: never overwrite. Same content → reuse; different → timestamp+contentHash.
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if _content_hash(existing) == _content_hash(snap):
                # identical — update pointers only
                try:
                    atomic_write_json(SNAP_DIR / "latest.json", snap)
                    manifest = {
                        "latest": path.name,
                        "snapshot_utc": snap.get("snapshot_utc"),
                        "files": [x.name for x in list_full_snapshots()],
                        "immutable": True,
                    }
                    atomic_write_json(SNAP_DIR / "manifest.json", manifest)
                except Exception:
                    pass
                return path
        except Exception:
            existing = None
        if m:
            ch = _content_hash(snap)
            stem = fname.replace(".json", "")
            path = SNAP_DIR / f"{stem}_{ch}.json"
            n = 0
            while path.exists():
                try:
                    ex2 = json.loads(path.read_text(encoding="utf-8"))
                    if _content_hash(ex2) == _content_hash(snap):
                        return path
                except Exception:
                    pass
                n += 1
                path = SNAP_DIR / f"{stem}_{ch}_{n}.json"
        else:
            # legacy day file exists — write a timestamped unique name
            now = datetime.now(timezone.utc)
            fname = now.strftime("%Y-%m-%dT%H%M%SZ.json")
            path = SNAP_DIR / fname
            n = 0
            while path.exists():
                n += 1
                fname = now.strftime("%Y-%m-%dT%H%M%SZ") + f"_{n}.json"
                path = SNAP_DIR / fname
    atomic_write_json(path, snap)
    # Convenience pointers
    try:
        atomic_write_json(SNAP_DIR / "latest.json", snap)
        manifest = {"latest": path.name, "snapshot_utc": snap.get("snapshot_utc"), "files": [x.name for x in list_full_snapshots()]}
        atomic_write_json(SNAP_DIR / "manifest.json", manifest)
    except Exception:
        pass
    return path


def load_latest_snapshot():
    """Load newest VALIDATED snapshot only.

    Never treats data/incoming/ or quarantine as validated history.
    Prefer INGEST_GATED_SNAPSHOT env when set (this-run gated path).
    """
    gated = os.environ.get("INGEST_GATED_SNAPSHOT")
    if gated:
        gp = Path(gated)
        if gp.exists():
            return gp, json.loads(gp.read_text(encoding="utf-8"))
    dated = list_full_snapshots()
    if dated:
        path = dated[-1]
        return path, json.loads(path.read_text(encoding="utf-8"))
    # latest.json only if it looks publishable
    latest_ptr = SNAP_DIR / "latest.json"
    if latest_ptr.exists():
        data = json.loads(latest_ptr.read_text(encoding="utf-8"))
        qg = (data or {}).get("qualityGate") or {}
        if qg.get("publishable") is not False and qg.get("status") not in {
            "reject", "needs_verification"
        }:
            return latest_ptr, data
    raise SystemExit(f"No validated snapshot found in {SNAP_DIR}")


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


def extract_earnings_session(raw) -> str | None:
    """Keep Pre-Market / Post-Market session hints from raw next-earnings strings."""
    if unavailable(raw):
        return None
    s = str(raw)
    m = re.search(r"\((\s*(?:Pre|Post)[\s-]*Market)\s*\)", s, re.I)
    if m:
        tok = re.sub(r"\s+", "-", m.group(1).strip(), count=0)
        tok = re.sub(r"(?i)pre[\s-]*market", "Pre-Market", tok)
        tok = re.sub(r"(?i)post[\s-]*market", "Post-Market", tok)
        if re.search(r"(?i)pre", m.group(1)):
            return "Pre-Market"
        if re.search(r"(?i)post", m.group(1)):
            return "Post-Market"
    if re.search(r"(?i)pre[\s-]*market", s):
        return "Pre-Market"
    if re.search(r"(?i)post[\s-]*market", s):
        return "Post-Market"
    return None


def format_next_earnings_display(
    raw,
    status: str | None,
    session: str | None = None,
) -> str | None:
    """UI e.g. 'Nov 25, 2026 · Post-Market · Estimated'."""
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
    sess = session or extract_earnings_session(raw)
    st = (status or "estimated").lower()
    label = "Confirmed" if st == "confirmed" else "Estimated"
    bits = [date_s]
    if sess:
        bits.append(sess)
    bits.append(label)
    return " · ".join(bits)


def _is_company_ir_url(url: str | None) -> bool:
    if not url:
        return False
    u = str(url).lower()
    if "seekingalpha.com" in u:
        return False
    return bool(
        re.search(r"investor\.|investors\.|/ir/|investor-relations|newsroom|press-release", u)
        or "company ir" in u
    )


def infer_next_earnings_status(next_raw, digest: dict | None = None) -> tuple[str | None, str | None, str | None]:
    """Return (status, source, sourceUrl).

    Confirmed ONLY if structured fields prove company IR:
      nextEarningsStatus == 'confirmed'
      AND nextEarningsSourceUrl present
      AND URL/evidence is company IR (not SA digest heuristic scan).
    Else Estimated. Heuristic scanning digest text for 'Company IR'+'announced' is REMOVED.
    """
    if unavailable(next_raw):
        return None, None, None
    text = str(next_raw)
    if isinstance(digest, dict):
        st = digest.get("nextEarningsStatus")
        url = digest.get("nextEarningsSourceUrl") or digest.get("nextEarningsSourceURL")
        src = digest.get("nextEarningsSource")
        # Structured confirmation only
        if (
            str(st or "").lower() == "confirmed"
            and url
            and (_is_company_ir_url(str(url)) or str(src or "").lower() in {"company ir", "investor relations"})
        ):
            return "confirmed", (src or "Company IR"), str(url)
        if str(st or "").lower() == "confirmed" and not (url and _is_company_ir_url(str(url))):
            # Explicitly NOT confirmed without structured IR URL evidence
            return "estimated", (src or "Seeking Alpha"), url
        if str(st or "").lower() == "estimated":
            return "estimated", (src or "Seeking Alpha"), url
    if "estimated" in text.lower():
        return "estimated", "Seeking Alpha", None
    return "estimated", "Seeking Alpha", None


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
    next_expected = nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # staleAfter = nextExpected + grace (client/server shared definition)
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
        # No due collection yet (e.g. Monday morning before 08:00+grace) → not stale if we have any success
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
        "analysts": to_num(raw_year.get("analysts")),
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

REVISION_REGIME_DOC = (
    "Revision regime from mapped Y+1 (nearTerm) and Y+2 (longTerm) SA 1M %: "
    "Y+1<0 and Y+2>=+5 → Back-end Loaded / Divergent; "
    "Y+1>=+5 and Y+2<0 → Front-loaded / Divergent; "
    "both >= +3 → Broad Upward Revision; both <= -3 → Broad Downward Revision; "
    "both near 0 (|x|<1) → Stable; else Mixed / Neutral-adjacent. "
    "Complements Momentum (which can be Neutral when legs diverge)."
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


def compute_revision_regime(rev_y1, rev_y2) -> dict:
    """nearTerm/longTerm revision % + regime label (for divergent cases Momentum misses)."""
    a, b = to_num(rev_y1), to_num(rev_y2)
    out = {
        "nearTermRevision": a,
        "longTermRevision": b,
        "revisionRegime": None,
        "revisionRegimeDoc": REVISION_REGIME_DOC,
    }
    if a is None or b is None:
        out["revisionRegime"] = "Insufficient Data"
        return out
    if a < 0 and b >= 5.0:
        out["revisionRegime"] = "Back-end Loaded / Divergent"
    elif a >= 5.0 and b < 0:
        out["revisionRegime"] = "Front-loaded / Divergent"
    elif a >= 3.0 and b >= 3.0:
        out["revisionRegime"] = "Broad Upward Revision"
    elif a <= -3.0 and b <= -3.0:
        out["revisionRegime"] = "Broad Downward Revision"
    elif abs(a) < 1.0 and abs(b) < 1.0:
        out["revisionRegime"] = "Stable"
    elif a > 0 and b > 0:
        out["revisionRegime"] = "Mild Upward"
    elif a < 0 and b < 0:
        out["revisionRegime"] = "Mild Downward"
    else:
        out["revisionRegime"] = "Divergent / Mixed"
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
        nestatus, nesource, nesource_url = infer_next_earnings_status(next_raw, digest_hint)
        nesession = extract_earnings_session(next_raw)
        next_display = format_next_earnings_display(next_raw, nestatus, nesession) if next_raw else None

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
            "nextEarningsSourceUrl": nesource_url,
            "nextEarningsSession": nesession,
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
            **(
                compute_revision_regime(
                    (eps_out.get(year_keys[1]) or {}).get("rev1M") if len(year_keys) > 1 else None,
                    (eps_out.get(year_keys[2]) or {}).get("rev1M") if len(year_keys) > 2 else None,
                )
                if len(year_keys) > 2
                else {"nearTermRevision": None, "longTermRevision": None, "revisionRegime": None}
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
            "nearTermRevision": c.get("nearTermRevision"),
            "longTermRevision": c.get("longTermRevision"),
            "revisionRegime": c.get("revisionRegime"),
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


def seed_and_append_daily_snapshots(
    companies: dict,
    tickers: list[str],
    snap: dict,
    snap_path: Path,
    year_keys: list[str] | None = None,
    *,
    persist: bool | None = None,
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
    if persist is None:
        persist = not NO_PERSISTENT_MUTATION
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
            "slot": slot,  # display mapping only
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

    # Snapshot-day consensus: append on first sight OR when EPS changed same day.
    # Persist ALL displayMappedYears (not only first 3 chartYears). UI still defaults to chartYears.
    # Partial/failed collection: do NOT append daily EPS historical observation for that ticker.
    snap_date = (snap.get("snapshot_utc") or snap_path.name)[:10]
    snap_utc = snap.get("snapshot_utc")
    persist_years = list(year_keys)  # all forward years
    for t in tickers:
        c = companies.get(t) or {}
        if c.get("collectionFailed") or c.get("usingLastKnownGood"):
            continue  # no fake fresh observation in JSONL / chart history
        for slot in persist_years:
            e = (c.get("eps") or {}).get(slot) or {}
            fiscal = (
                e.get("reportedFiscalLabel")
                or e.get("reportedFiscalPeriodEnding")
                or e.get("reported_fiscal_label")
            )
            if not fiscal:
                continue  # fiscal identity required — mapped slot is display only
            identity = str(fiscal).strip()
            key = (snap_date, t, identity)
            new_eps = e.get("consensus")
            # P2: null/unavailable consensus → do not append historical observation
            # (day summary JSON may still keep unavailable slots below)
            if new_eps is None or to_num(new_eps) is None:
                continue
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
                "reportedFiscalLabel": fiscal,
                "calendarAlignment": e.get("calendarAlignment"),
                "source": "daily_export",
                "updateTime": snap_utc,
            }
            new_rows.append(row)
            last_consensus[key] = new_eps

    # Dated JSON for the day reflecting latest values (overwrite that day file only).
    # Failed/LKG tickers may keep status=missing + lastKnownGood* without creating chart observations.
    day_payload = {
        "date": snap_date,
        "snapshotUtc": snap_utc,
        "tickers": {},
    }
    for t in tickers:
        c = companies.get(t) or {}
        failed = bool(c.get("collectionFailed") or c.get("usingLastKnownGood"))
        slots_payload = {
            slot: {
                "consensus": ((c.get("eps") or {}).get(slot) or {}).get("consensus"),
                "reportedFiscalLabel": ((c.get("eps") or {}).get(slot) or {}).get("reportedFiscalLabel"),
                "calendarAlignment": ((c.get("eps") or {}).get(slot) or {}).get("calendarAlignment"),
                "mappedYear": slot,
            }
            for slot in persist_years
        }
        if failed:
            # Preserve LKG display values but mark not a fresh observation
            lkg_as_of = c.get("lastSuccessfulCollection") or c.get("updateTime") or c.get("collectionAsOf")
            for slot, sp in slots_payload.items():
                sp["status"] = "missing"
                sp["usingLastKnownGood"] = True
                sp["lastKnownGoodValue"] = sp.get("consensus")
                sp["lastKnownGoodAsOf"] = lkg_as_of
            day_payload["tickers"][t] = {
                **slots_payload,
                "status": "missing",
                "usingLastKnownGood": True,
                "collectionFailed": True,
                "lastKnownGoodAsOf": lkg_as_of,
            }
        else:
            day_payload["tickers"][t] = slots_payload
        # Also expose by fiscal label for identity stability
        by_fiscal = {}
        for slot in persist_years:
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
    if persist:
        if new_rows:
            # Fail-closed: atomic JSONL failure aborts (no non-atomic append fallback)
            from atomic_io import append_jsonl_atomic
            append_jsonl_atomic(jsonl_path, new_rows)
            existing.extend(new_rows)
        write_json(DAILY_SNAP_DIR / f"{snap_date}.json", day_payload)
    else:
        # Pure-build: stage pending mutations for ingest COMMIT
        if STAGE_DIR is not None:
            STAGE_DIR.mkdir(parents=True, exist_ok=True)
            write_json(STAGE_DIR / "pending_daily_rows.json", {"rows": new_rows})
            day_payload["date"] = snap_date
            write_json(STAGE_DIR / "pending_day_summary.json", day_payload)
        # Include new rows in returned series for this export's eps_history
        existing = list(existing) + list(new_rows)

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
                data = ensure_earnings_provenance(data)
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
            data = ensure_earnings_provenance(data)
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
    """Atomic JSON write (temp + rename) under process flock.

    Fail-closed: NO silent non-atomic fallback for core persistent/public JSON.
    Atomic failure aborts (raises) so callers do not publish half-written state.
    """
    from atomic_io import atomic_write_json
    atomic_write_json(Path(path), obj)


def run_build_alerts(
    current_snapshot: dict | None = None,
    snapshot_utc: str | None = None,
    display_years: list[str] | None = None,
    *,
    persist: bool | None = None,
) -> dict:
    """Call deterministic alert engine before writing web/data/alerts.json.

    Pass THIS RUN's Quality-Gated snapshot — do not let Alert Engine rediscover
    current generation from snapshots dir (manifest.json breaks Source 1M).

    On evaluate_alerts() exception: preserve last-known-good alertHistory and
    previous active alerts; set alertEngineStatus=error. Never overwrite with
    empty arrays.
    """
    import build_alerts as ba

    prior = {}
    try:
        if ALERTS_PATH.exists():
            prior = json.loads(ALERTS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        prior = {}

    if persist is None:
        persist = not NO_PERSISTENT_MUTATION
    try:
        payload = ba.evaluate_alerts(
            current_snapshot=current_snapshot,
            snapshot_utc=snapshot_utc,
            display_years=display_years,
        )
        payload = dict(payload)
        payload["alertEngineLastSuccessfulEvaluation"] = payload.get("alertEngineLastEvaluated")
        payload["alertEngineLastAttempt"] = payload.get("alertEngineLastEvaluated")
        if persist:
            ba.write_alerts(payload)
        elif STAGE_DIR is not None:
            STAGE_DIR.mkdir(parents=True, exist_ok=True)
            write_json(STAGE_DIR / "pending_alerts.json", payload)
        return payload
    except Exception as exc:
        now = now_utc_iso()
        prior_active = prior.get("activeAlerts") or prior.get("alerts") or []
        prior_hist = prior.get("alertHistory") or prior_active
        err = {
            "alerts": list(prior_active),
            "activeAlerts": list(prior_active),
            "alertHistory": list(prior_hist),
            "alertDiagnostics": prior.get("alertDiagnostics") or [],
            "homepageAttentionQueue": prior.get("homepageAttentionQueue") or [],
            "changedSinceLastCollection": prior.get("changedSinceLastCollection") or [],
            "alertEngineLastEvaluated": prior.get("alertEngineLastEvaluated"),
            "alertEngineLastAttempt": now,
            "alertEngineLastSuccessfulEvaluation": prior.get("alertEngineLastSuccessfulEvaluation")
            or prior.get("alertEngineLastEvaluated"),
            "alertEngineStatus": "error",
            "alertEngineError": f"{type(exc).__name__}: {exc}",
            "oneShotActiveDays": prior.get("oneShotActiveDays"),
            "lastSuccessfulCollection": prior.get("lastSuccessfulCollection"),
        }
        try:
            if persist is None:
                persist = not NO_PERSISTENT_MUTATION
            if persist:
                ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                write_json(ALERTS_PATH, err)
            elif STAGE_DIR is not None:
                STAGE_DIR.mkdir(parents=True, exist_ok=True)
                write_json(STAGE_DIR / "pending_alerts.json", err)
        except Exception:
            # Preserve prior file bytes on write failure
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



OPERATIONAL_FIELDS = {
    "updateTime",
    "collectionAsOf",
    "dataAsOf",
    "lastSuccessfulCollection",
    "alertEngineLastEvaluated",
    "ageDays",
    "sitePublished",
    "sitePublishedDisplay",
    "lastSuccessfulCollectionDisplay",
    "consensusDataAsOf",
    "consensusDataAsOfDisplay",
    "lastUpdated",
    "lastUpdatedDisplay",
    "refreshVersion",
    "dataStale",
    "nextExpected",
    "staleAfter",
    "collectionStatus",
    "collectionStatusLabel",
    "successfulTickers",
    "failedTickers",
    "successfulCount",
    "totalCount",
}


def strip_operational_fields(obj):
    """Recursively drop operational/timestamp fields before dataVersion hashing."""
    if isinstance(obj, list):
        return [strip_operational_fields(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in OPERATIONAL_FIELDS:
                continue
            out[k] = strip_operational_fields(v)
        return out
    return obj


def strip_operational_alert_fields(alerts_list: list) -> list:
    """Drop ageDays / other operational fields so dataVersion stays stable across calendar ticks."""
    return strip_operational_fields(alerts_list or [])



def load_prior_companies() -> dict:
    p = WEB_DATA / "companies.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_last_known_good_for_missing(
    companies: dict,
    tickers: list[str],
    snap: dict,
    gate: dict | None = None,
) -> dict:
    """For missing/failed watchlist tickers: fill last-known-good + STALE/FAILED badges.

    Never invent EPS — only reuse prior web/data/companies.json or prior snapshot tickers
    already exported. Marks collectionFailed=True and dataFreshnessBadge=FAILED|STALE.
    """
    missing = set((gate or {}).get("missingTickers") or [])
    prior_companies = load_prior_companies()
    prior_snap_tickers = {}
    # Prefer prior dated snapshot (excluding current) already loaded into gate flow externally
    for t in tickers:
        c = companies.get(t) or {}
        in_snap = t in ((snap.get("tickers") or {}) if isinstance(snap, dict) else {})
        failed = bool(c.get("collectionFailed")) or (t.upper() in {x.upper() for x in missing}) or not in_snap
        if not failed and in_snap:
            # Successful fresh ticker
            c = dict(c)
            c["dataFreshnessBadge"] = None
            companies[t] = c
            continue
        # Need LKG
        lkg = prior_companies.get(t)
        if isinstance(lkg, dict) and (lkg.get("eps") or lkg.get("price") is not None or lkg.get("lastClose") is not None):
            merged = dict(lkg)
            merged["ticker"] = t
            merged["collectionFailed"] = True
            merged["dataFreshnessBadge"] = "FAILED" if (t.upper() in {x.upper() for x in missing} or not in_snap) else "STALE"
            merged["usingLastKnownGood"] = True
            gaps = list(merged.get("dataGaps") or [])
            note = "missing from snapshot — last-known-good retained"
            if note not in gaps:
                gaps.append(note)
            merged["dataGaps"] = gaps
            companies[t] = merged
        else:
            # Minimal failed stub — still present so UI can show FAILED
            stub = dict(c) if c else {"ticker": t, "eps": {}, "dataGaps": []}
            stub["ticker"] = t
            stub["collectionFailed"] = True
            stub["dataFreshnessBadge"] = "FAILED"
            stub["usingLastKnownGood"] = False
            gaps = list(stub.get("dataGaps") or [])
            gaps.append("missing from snapshot — no last-known-good")
            stub["dataGaps"] = gaps
            companies[t] = stub
    return companies


def _hostname_of(url: str | None) -> str | None:
    """Real hostname via urlparse — never substring match on full URL."""
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        host = urlparse(str(url)).hostname
    except Exception:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def load_official_domains_by_ticker(root: Path | None = None) -> dict[str, list[str]]:
    root = root or ROOT
    path = Path(root) / "data" / "universe.json"
    if not path.exists():
        return {}
    try:
        u = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = u.get("officialDomainsByTicker") or {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k).upper()] = [str(x).lower().rstrip(".") for x in v]
        elif isinstance(v, str):
            out[str(k).upper()] = [v.lower().rstrip(".")]
    return out


def _is_sec_hostname(host: str | None) -> bool:
    """SEC only sec.gov or *.sec.gov — spoofed-sec.gov.evil must NOT match."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "sec.gov" or h.endswith(".sec.gov")


def _is_official_ir_hostname(host: str | None, ticker: str | None = None) -> bool:
    """Tier1 IR only via officialDomainsByTicker — never generic investor.*/ir.*."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    mapping = load_official_domains_by_ticker()
    if ticker:
        domains = mapping.get(str(ticker).upper()) or []
        for d in domains:
            if h == d or h.endswith("." + d):
                return True
        return False  # ticker known → only its official list (not generic investor.*)
    # Ticker unknown: still Tier1 if host is listed for ANY ticker
    for domains in mapping.values():
        for d in domains:
            if h == d or h.endswith("." + d):
                return True
    return False


def _is_generic_ir_hostname(host: str | None) -> bool:
    """investor.*/investors.*/ir.* host patterns — at most unverified_ir_candidate."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    if h.startswith("investor.") or h.startswith("investors."):
        return True
    if h.startswith("ir.") and not (h == "seekingalpha.com" or h.endswith(".seekingalpha.com")):
        return True
    return False


def _is_seekingalpha_hostname(host: str | None) -> bool:
    """Strict: host == seekingalpha.com or endswith .seekingalpha.com (not evilseekingalpha.com)."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "seekingalpha.com" or h.endswith(".seekingalpha.com")


def classify_source_tier(
    url: str | None,
    *,
    source_type: str | None = None,
    ticker: str | None = None,
) -> str | int | None:
    """Deterministic source classifier by real hostname (urlparse).

    Prefer ingest sourceType as source-of-truth when provided.
    Official IR (officialDomainsByTicker) / SEC → Tier1; generic investor.*/ir.* → unverified_ir_candidate;
    official transcript → official;
    Reuters/Bloomberg/WSJ/CNBC → Tier3; Seeking Alpha → Tier4;
    Unknown → Unknown. Never Tier1 merely because a URL / query string exists.
    """
    # Prefer explicit ingest sourceType
    if source_type:
        st = str(source_type).strip().lower()
        if st in {"sec", "company_ir", "ir", "8-k", "10-k", "10-q", "earnings_presentation"}:
            return 1
        if st in {"official_transcript", "transcript"}:
            return "official"
        if st in {"reuters", "bloomberg", "wsj", "cnbc", "dowjones"}:
            return 3
        if st in {"seeking_alpha", "seekingalpha", "sa"}:
            return 4
    if not url:
        return None
    host = _hostname_of(url)
    u = str(url).lower()
    if _is_sec_hostname(host):
        return 1
    if _is_official_ir_hostname(host, ticker=ticker):
        return 1
    # Generic investor.*/ir.* — NOT Tier1 (unverified_ir_candidate at most)
    if _is_generic_ir_hostname(host):
        return "unverified_ir_candidate"
    # Spoofed domains / investor string in query must NOT be Tier1
    if host and any(host == d or host.endswith("." + d) for d in (
        "reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "dowjones.com"
    )):
        return 3
    if _is_seekingalpha_hostname(host):
        return 4
    if host and "transcript" in u and _is_official_ir_hostname(host, ticker=ticker):
        return "official"
    return "Unknown"


def ensure_earnings_provenance(data: dict) -> dict:
    """Ensure hasDigest digests carry actuals + consensusComparison with tiers.

    Never claim SA consensus beat/miss as Company IR Tier 1.
    actuals = company IR metrics (Tier 1 when IR URL present).
    consensusComparison = beat/miss attribution (typically SA Tier 4).
    """
    if not isinstance(data, dict):
        return data
    if not data.get("hasDigest"):
        return data
    out = dict(data)

    # --- actuals ---
    actuals = out.get("actuals") if isinstance(out.get("actuals"), dict) else None
    results = out.get("results") if isinstance(out.get("results"), dict) else {}
    if actuals is None:
        metrics = {}
        for k in ("eps", "revenue", "grossMargin", "operatingMargin", "fcf"):
            if results.get(k) is not None:
                metrics[k] = results.get(k)
        # Also pull from list forms
        if not metrics.get("eps"):
            for row in out.get("eps") or []:
                if isinstance(row, dict) and row.get("value"):
                    metrics["eps"] = row.get("value")
                    if row.get("sourceUrl") and not results.get("sourceUrl"):
                        results = dict(results)
                        results["sourceUrl"] = row.get("sourceUrl")
                        results["sourceTier"] = row.get("sourceTier") or 1
                    break
        if not metrics.get("revenue"):
            for row in out.get("revenue") or []:
                if isinstance(row, dict) and row.get("value"):
                    metrics["revenue"] = row.get("value")
                    break
        if not metrics.get("grossMargin"):
            for row in out.get("margins") or []:
                if isinstance(row, dict) and "gross" in str(row.get("label") or "").lower() and row.get("value"):
                    metrics["grossMargin"] = row.get("value")
                    break
        # Parentheses matter: missing guidanceDetail must NOT null results.sourceUrl
        gd = out.get("guidanceDetail")
        gd_url = gd.get("sourceUrl") if isinstance(gd, dict) else None
        src_url = results.get("sourceUrl") or gd_url
        # Prefer IR / sec from results
        if not src_url:
            for row in (out.get("eps") or []) + (out.get("revenue") or []):
                if isinstance(row, dict) and row.get("sourceUrl"):
                    src_url = row.get("sourceUrl")
                    break
        tier = results.get("sourceTier")
        classified = classify_source_tier(src_url)
        if classified is not None:
            # Never retain Tier1 when the URL is clearly not Company IR/SEC
            if tier is None or (tier == 1 and classified != 1):
                tier = classified
            elif tier is None:
                tier = classified
        elif tier is None and not src_url:
            tier = None
        # Always materialize actuals for hasDigest (may be sparse)
        actuals = {
            "metrics": metrics,
            "eps": metrics.get("eps"),
            "revenue": metrics.get("revenue"),
            "grossMargin": metrics.get("grossMargin"),
            "sourceUrl": src_url,
            "secUrl": results.get("secUrl") if isinstance(results, dict) else None,
            "sourceTier": tier if src_url else None,
        }
        out["actuals"] = actuals
    else:
        # Normalize metrics key
        actuals = dict(actuals)
        if "metrics" not in actuals:
            metrics = {}
            for k in ("eps", "revenue", "grossMargin", "operatingMargin", "fcf"):
                if actuals.get(k) is not None:
                    metrics[k] = actuals.get(k)
            actuals["metrics"] = metrics
        if actuals.get("sourceUrl") is None and results.get("sourceUrl"):
            actuals["sourceUrl"] = results.get("sourceUrl")
        if actuals.get("sourceTier") is None and results.get("sourceTier") is not None:
            actuals["sourceTier"] = results.get("sourceTier")
        out["actuals"] = actuals

    # --- consensusComparison ---
    cc = out.get("consensusComparison") if isinstance(out.get("consensusComparison"), dict) else None
    vs = None
    if cc:
        vs = cc.get("vsConsensus") or cc.get("beatMiss") or cc.get("comparison")
    if vs is None:
        vs = out.get("comparison") or results.get("vsConsensus") or out.get("guidanceVsConsensus")
    # Find SA (non-IR) source for beat/miss
    sa_url = None
    sa_tier = 4
    if cc and cc.get("sourceUrl"):
        sa_url = cc.get("sourceUrl")
        if cc.get("sourceTier") is not None:
            sa_tier = cc.get("sourceTier")
    if not sa_url:
        for s in out.get("sources") or []:
            if not isinstance(s, dict):
                continue
            u = str(s.get("url") or "")
            if "seekingalpha.com" in u.lower():
                sa_url = u
                sa_tier = s.get("sourceTier") if s.get("sourceTier") is not None else 4
                break
    if not sa_url:
        # Supplemental URLs on digest bullets
        for key in ("positives", "negatives", "uncertainties"):
            for item in out.get(key) or []:
                if not isinstance(item, dict):
                    continue
                u = item.get("supplementalUrl") or item.get("url")
                if u and "seekingalpha.com" in str(u).lower():
                    sa_url = u
                    sa_tier = 4
                    break
            if sa_url:
                break
    # Never use Company IR URL as consensusComparison source claiming beat/miss
    ir_url = (out.get("actuals") or {}).get("sourceUrl") if isinstance(out.get("actuals"), dict) else None
    if sa_url and ir_url and sa_url == ir_url:
        # Prefer to keep IR only on actuals; clear SA claim
        if "seekingalpha.com" not in str(sa_url).lower():
            sa_url = None

    # Always materialize consensusComparison for hasDigest
    beat_miss = str(vs) if vs is not None else None
    if True:
        new_cc = {
            "vsConsensus": beat_miss,
            "beatMiss": beat_miss,
            "sourceUrl": sa_url,
            "sourceTier": sa_tier if sa_url else None,
            "note": (
                "Beat/miss vs consensus attributed to Seeking Alpha / secondary summary "
                "(not Company IR actuals Tier 1)"
                if sa_url and "seekingalpha.com" in str(sa_url).lower()
                else "Beat/miss attribution; actuals remain on Company IR when Tier 1"
            ),
        }
        # If existing cc had fields, preserve extras but enforce tier separation
        if cc:
            for k, v in cc.items():
                if k not in new_cc or new_cc[k] is None:
                    new_cc[k] = v
            # Force: if sourceUrl is IR-looking and claims consensus, demote
            u = str(new_cc.get("sourceUrl") or "").lower()
            if new_cc.get("sourceTier") == 1 and "seekingalpha.com" not in u and (
                "investor." in u or "investors." in u or "sec.gov" in u
            ):
                # IR URL must not be the consensusComparison claim as Tier 1 beat/miss
                if sa_url:
                    new_cc["sourceUrl"] = sa_url
                    new_cc["sourceTier"] = sa_tier
                else:
                    new_cc["sourceTier"] = 4
                    new_cc["note"] = (
                        "Consensus beat/miss is not Company IR Tier 1; tier adjusted"
                    )
        out["consensusComparison"] = new_cc

    return out


def collection_completeness(companies: dict, tickers: list[str]) -> dict:
    """COMPLETE only when every watchlist ticker succeeded (none missing/failed)."""
    successful, failed = [], []
    for t in tickers:
        c = companies.get(t) or {}
        if c.get("collectionFailed") or c.get("usingLastKnownGood"):
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






COMPARISON_CHECKPOINT_PATH = ROOT / "data" / "comparison_checkpoint.json"


def load_comparison_checkpoint() -> dict:
    if COMPARISON_CHECKPOINT_PATH.exists():
        try:
            return json.loads(COMPARISON_CHECKPOINT_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def advance_comparison_checkpoint(snap_utc: str) -> dict:
    """Atomic update of What-Changed checkpoint AFTER successful collection/export."""
    payload = {
        "snapUtc": snap_utc,
        "comparisonCheckpoint": snap_utc,
        "advancedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_json(COMPARISON_CHECKPOINT_PATH, payload)
    return payload


def stamp_build_id(obj, build_id: str, *, ticker_map: bool = False):
    """Attach buildId to a JSON root for multi-file consistency checks.

    ticker_map=True (companies/earnings): use _buildId so ticker keys stay clean.
    """
    if isinstance(obj, dict):
        out = dict(obj)
        if ticker_map:
            out["_buildId"] = build_id
        else:
            out["buildId"] = build_id
        return out
    return {"buildId": build_id, "data": obj}



def quarantine_snapshot(snap_path: Path, snap: dict, gate: dict) -> Path:
    """Save invalid snapshot to quarantine; do not touch public web/data."""
    qdir = ROOT / "data" / "snapshots" / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    name = snap_path.name if snap_path else "unknown.json"
    try:
        import snapshot_quality as _sq_q
        qpath = _sq_q.immutable_quarantine_path(qdir, name, snap if isinstance(snap, dict) else {"raw": snap})
    except Exception:
        qpath = qdir / f"{name}.quarantine"
    payload = dict(snap) if isinstance(snap, dict) else {"raw": snap}
    payload["qualityGate"] = gate
    payload["status"] = gate.get("status")
    write_json(qpath, payload)
    return qpath


def main(argv: list[str] | None = None) -> int:
    """Export web data. Fail-closed when qualityGate.publishable != true.

    DEFAULT: read-only pure exporter — does NOT mutate daily/revision/alert/
    checkpoint persistent financial state. ingest_snapshot.py is the sole writer.
    Legacy mutation only via explicit --legacy-mutate (production/scheduler must not use it).
    Pure-build (--no-persistent-mutation / default): write staged or web JSON only.
    """
    import argparse
    global WEB_DATA, NO_PERSISTENT_MUTATION, STAGE_DIR, ROOT
    global SNAP_DIR, REV_PATH, ALERTS_PATH, DAILY_SNAP_DIR, COMPARISON_CHECKPOINT_PATH

    ap = argparse.ArgumentParser(description="Export web JSON (read-only by default; sole writer is ingest)")
    ap.add_argument("--input-root", help="Optional alternate project root for reads")
    ap.add_argument("--output-root", help="Write web output under this root (…/data)")
    ap.add_argument(
        "--no-persistent-mutation",
        action="store_true",
        help="Pure-build: do not mutate daily/alerts/checkpoint (DEFAULT behavior)",
    )
    ap.add_argument(
        "--legacy-mutate",
        action="store_true",
        help="LEGACY ONLY: allow mutating daily/revision/alert/checkpoint (not for production/scheduler)",
    )
    ap.add_argument("--stage-dir", help="Staging dir for pending_* commit payloads")
    ap.add_argument("--backfill", action="store_true", help="Allow old snapshot timestamps")
    args, _unknown = ap.parse_known_args(argv)

    # Default = read-only (no persistent financial mutation). --legacy-mutate opts in.
    if args.legacy_mutate or os.environ.get("LEGACY_MUTATE") == "1":
        NO_PERSISTENT_MUTATION = False
    else:
        NO_PERSISTENT_MUTATION = True
    if args.no_persistent_mutation or os.environ.get("NO_PERSISTENT_MUTATION") == "1":
        NO_PERSISTENT_MUTATION = True
    if args.stage_dir:
        STAGE_DIR = Path(args.stage_dir)
    elif os.environ.get("INGEST_STAGE_DIR"):
        STAGE_DIR = Path(os.environ["INGEST_STAGE_DIR"])
    else:
        STAGE_DIR = None

    if args.input_root:
        ROOT = Path(args.input_root)
        SNAP_DIR = ROOT / "data" / "snapshots"
        REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
        ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
        DAILY_SNAP_DIR = ROOT / "data" / "daily_eps_snapshots"
        COMPARISON_CHECKPOINT_PATH = ROOT / "data" / "comparison_checkpoint.json"

    if args.output_root:
        out = Path(args.output_root)
        WEB_DATA = out / "data" if out.name != "data" else out
    elif NO_PERSISTENT_MUTATION and STAGE_DIR is not None:
        WEB_DATA = STAGE_DIR / "web" / "data"

    if NO_PERSISTENT_MUTATION:
        os.environ["INGEST_DEFER_HISTORY"] = os.environ.get("INGEST_DEFER_HISTORY", "0")
        # Daily handled via persist=False path; keep DEFER off so we compute pending rows
        os.environ.pop("INGEST_DEFER_HISTORY", None)

    # stash backfill for gate
    os.environ["EXPORT_BACKFILL"] = "1" if args.backfill else os.environ.get("EXPORT_BACKFILL", "0")

    # Global pipeline lock: Quality Gate → persist → alerts → export
    # Skip if parent publish script already holds data/.pipeline.lock
    _lock_cm = None
    if os.environ.get("PIPELINE_LOCK_HELD") != "1":
        from atomic_io import (
            GlobalPipelineLock,
            PipelineBusy,
            default_pipeline_lock_path,
            RUN_IN_PROGRESS_MSG,
        )
        try:
            _lock_cm = GlobalPipelineLock(default_pipeline_lock_path(ROOT), non_blocking=True)
            _lock_cm.__enter__()
        except PipelineBusy:
            print(RUN_IN_PROGRESS_MSG, flush=True)
            return 2
    try:
        return _main_locked()
    finally:
        if _lock_cm is not None:
            _lock_cm.__exit__(None, None, None)


def _main_locked() -> int:
    """Core export under pipeline lock."""
    fault = os.environ.get("FAULT_INJECT_EXPORT_EXIT")
    if fault:
        try:
            code = int(fault)
        except Exception:
            code = 9
        print(f"FAULT_INJECT_EXPORT_EXIT={code}", flush=True)
        return code
    # Readers reconcile CURRENT → live cache before using live paths
    try:
        import ingest_snapshot as _ing_live
        _ing_live.rebind_paths(ROOT)
        _ing_live.ensure_live_matches_current()
    except Exception as _live_exc:
        print(f"WARNING: ensure_live_matches_current: {_live_exc}")
    snap_path, snap = load_latest_snapshot()
    tickers = load_watchlist()
    prior_snap = None
    snaps = list_full_snapshots()
    for p in reversed(snaps):
        if p.resolve() == snap_path.resolve():
            continue
        try:
            prior_snap = json.loads(p.read_text(encoding="utf-8"))
            break
        except Exception:
            continue

    # Per-ticker LKG from validated history (not only previous whole snapshot)
    lkg_by_ticker = {}
    try:
        import snapshot_quality as sq
        lkg_by_ticker = sq.load_last_known_good_by_ticker(
            SNAP_DIR, exclude_path=snap_path, expected_tickers=tickers, root=ROOT
        )
    except Exception:
        lkg_by_ticker = {}

    gate = {
        "status": "ok",
        "publishable": True,
        "reason": None,
        "message": "ok",
        "extremeChanges": [],
        "expectedTickers": list(tickers),
        "receivedTickers": list((snap.get("tickers") or {}).keys()) if isinstance(snap, dict) else [],
        "missingTickers": [],
        "unexpectedTickers": [],
    }
    try:
        import snapshot_quality as sq
        gate = sq.gate_snapshot(
            snap,
            prior_snap,
            expected_tickers=tickers,
            lkg_by_ticker=lkg_by_ticker,
            root=ROOT,
            backfill=os.environ.get("EXPORT_BACKFILL") == "1",
        )
        if gate.get("normalizedSnapshot"):
            snap = gate["normalizedSnapshot"]
    except Exception as exc:
        print(f"quality gate error (fail-closed): {exc}")
        gate = {
            "status": "reject",
            "reason": "gate_exception",
            "message": f"{type(exc).__name__}: {exc}",
            "publishable": False,
            "extremeChanges": [],
            "expectedTickers": list(tickers),
            "receivedTickers": [],
            "missingTickers": list(tickers),
            "unexpectedTickers": [],
        }

    snap = dict(snap) if isinstance(snap, dict) else {"tickers": {}}
    snap["qualityGate"] = {
        "status": gate.get("status"),
        "reason": gate.get("reason"),
        "publishable": gate.get("publishable"),
        "extremeChanges": gate.get("extremeChanges") or [],
        "coverageRegressions": gate.get("coverageRegressions") or [],
        "priceOutliers": gate.get("priceOutliers") or [],
        "zeroAnalystConsensus": gate.get("zeroAnalystConsensus") or [],
        "expectedTickers": gate.get("expectedTickers") or tickers,
        "receivedTickers": gate.get("receivedTickers") or [],
        "missingTickers": gate.get("missingTickers") or [],
        "unexpectedTickers": gate.get("unexpectedTickers") or [],
        "message": gate.get("message"),
    }

    if gate.get("publishable") is not True:
        qpath = quarantine_snapshot(snap_path, snap, gate)
        print(
            f"ERROR: qualityGate.publishable!=true status={gate.get('status')} "
            f"reason={gate.get('reason')} — aborting export (no public web/data write, "
            f"no daily snapshots, no lastSuccessfulCollection update). quarantine={qpath}"
        )
        # Do NOT modify public web/data, daily snapshots, revisions, lastSuccessfulCollection
        return 1

    if gate.get("status") == "partial":
        print(f"WARNING: partial watchlist collection — {gate.get('message')}")
    elif gate.get("status") == "needs_verification":
        # Should be non-publishable already; belt-and-suspenders
        print(f"ERROR: needs_verification — {gate.get('message')}")
        quarantine_snapshot(snap_path, snap, gate)
        return 1

    driver_errors = ensure_driver_files(tickers)
    snap_utc = snap.get("snapshot_utc") or ""
    source = snap.get("source") or "Seeking Alpha"
    display = taipei_display_safe(snap_utc)

    year_keys = display_mapped_years(now_taipei().year, include_y3=True)
    chart_years = chart_years_from(year_keys)

    prior_meta = load_prior_meta()

    companies = build_companies(snap, tickers, year_keys=year_keys)
    companies = apply_last_known_good_for_missing(companies, tickers, snap, gate)

    valuation = build_valuation(companies, tickers, snap_utc, year_keys=year_keys)
    history_raw = load_history()
    revisions = [map_history_row(r) for r in history_raw]

    # Daily snapshots: mutate only when NOT pure-build; else stage pending_* for COMMIT
    daily_rows = seed_and_append_daily_snapshots(
        companies,
        tickers,
        snap,
        snap_path,
        year_keys=year_keys,
        persist=not NO_PERSISTENT_MUTATION,
    )
    if NO_PERSISTENT_MUTATION:
        print("NO_PERSISTENT_MUTATION — daily/alerts/checkpoint staged for ingest COMMIT")
    eps_history = build_eps_history_from_daily(daily_rows)

    earnings = load_or_init_earnings(companies, tickers)

    # Auto-generate revision events BEFORE Alert Engine when not already done by ingest.
    # Identity: ticker + reportedFiscalPeriodEnding. Must complete so single_revision_gt_2pct works.
    # Read-only default: NEVER append to revision history (ingest is sole writer).
    if os.environ.get("INGEST_REVISIONS_DONE") != "1":
        if NO_PERSISTENT_MUTATION:
            print("read-only exporter — skip revision history mutation (ingest is sole writer)")
        else:
            try:
                import ingest_snapshot as ing
                hist_for_rev = ing.load_revision_history(REV_PATH)
                events = ing.generate_revision_events(
                    snap, lkg_by_ticker, existing_history=hist_for_rev
                )
                n_rev = ing.append_revision_events(events)
                if n_rev:
                    print(f"revision events appended: +{n_rev}")
            except Exception as exc:
                # Fail-closed: do not continue COMPLETE with stale revision history
                print(f"ERROR: revision event generation failed — aborting export: {exc}")
                return 1

    alerts_payload = run_build_alerts(
        current_snapshot=snap,
        snapshot_utc=snap_utc,
        display_years=year_keys,
    )
    active = alerts_payload.get("activeAlerts") or alerts_payload.get("alerts") or []
    history_alerts = alerts_payload.get("alertHistory") or active
    alerts = {
        "activeAlerts": active,
        "alertHistory": history_alerts,
        "alertDiagnostics": alerts_payload.get("alertDiagnostics") or [],
        "homepageAttentionQueue": alerts_payload.get("homepageAttentionQueue") or [],
        "changedSinceLastCollection": alerts_payload.get("changedSinceLastCollection") or [],
        "alerts": active,
        "alertEngineLastEvaluated": alerts_payload.get("alertEngineLastEvaluated"),
        "alertEngineLastAttempt": alerts_payload.get("alertEngineLastAttempt")
        or alerts_payload.get("alertEngineLastEvaluated"),
        "alertEngineLastSuccessfulEvaluation": alerts_payload.get("alertEngineLastSuccessfulEvaluation"),
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
    # Never claim COMPLETE when gate reports missing tickers
    if gate.get("missingTickers") and coll["collectionStatus"] == "complete":
        coll["collectionStatus"] = "partial"
        coll["collectionStatusLabel"] = (
            f"PARTIAL · {coll['successfulCount']}/{coll['totalCount']}"
        )

    # refreshVersion: operational collection state ONLY — exclude sitePublished
    # (changing only sitePublished must NOT change refreshVersion / publish loop)
    collection_run_id = (
        os.environ.get("INGEST_COLLECTION_RUN_ID")
        or snap.get("collectionRunId")
        or snap_path.name
    )
    refresh_version = hashlib.sha256(
        canonical_json_bytes(
            {
                "collectionRunId": collection_run_id,
                "lastSuccessfulCollection": snap_utc,
                "collectionStatus": coll.get("collectionStatus"),
                "alertEngineStatus": alerts.get("alertEngineStatus"),
            }
        )
    ).hexdigest()

    payload_parts = {
        "companies": strip_operational_fields(companies),
        "valuation": strip_operational_fields({"rows": valuation}),
        "revisions": strip_operational_fields({"revisions": revisions}),
        "eps_history": strip_operational_fields(eps_history),
        "earnings": strip_operational_fields(earnings),
        "alerts": strip_operational_fields({
            "activeAlerts": active,
            "alertEngineStatus": alerts.get("alertEngineStatus"),
            "homepageAttentionQueue": alerts.get("homepageAttentionQueue") or [],
        }),
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
        "momentumFormula": MOMENTUM_FORMULA_DOC,
        "revisionRegimeDoc": REVISION_REGIME_DOC,
        "consensusDataAsOf": snap_utc,
        "consensusDataAsOfDisplay": display,
        "lastSuccessfulCollection": snap_utc,
        "lastSuccessfulCollectionDisplay": display,
        "sitePublished": site_published,
        "sitePublishedDisplay": site_published_display,
        "dataStale": data_stale,
        "nextExpected": freshness.get("nextExpected"),
        "staleAfter": freshness.get("staleAfter"),
        "lastDueGraceDeadline": freshness.get("lastDueGraceDeadline"),
        "freshnessRule": freshness.get("freshnessRule"),
        "graceHours": freshness.get("graceHours"),
        "displayMappedYears": year_keys,
        "chartYears": chart_years,
        "dataVersion": data_version,
        "refreshVersion": refresh_version,
        "buildId": build_id,
        "alertEngineLastEvaluated": alerts.get("alertEngineLastEvaluated"),
        "alertEngineLastAttempt": alerts.get("alertEngineLastAttempt"),
        "alertEngineLastSuccessfulEvaluation": alerts.get("alertEngineLastSuccessfulEvaluation"),
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
        "qualityGate": snap.get("qualityGate"),
        "schemaVersion": SCHEMA_VERSION,
    }
    # Release identity: full static deps (incl. vendor) + schema + data + refresh
    # Prefer production web shell even when output-root is staging
    try:
        import ingest_snapshot as _ing_ver
        app_version = _ing_ver.compute_app_version(ROOT / "web")
    except Exception:
        h = hashlib.sha256()
        web_shell = ROOT / "web"
        for name in ("index.html", "app.js", "styles.css"):
            wp = web_shell / name
            if wp.exists():
                h.update(wp.read_bytes())
            h.update(b"|")
        vendor = web_shell / "vendor"
        if vendor.is_dir():
            for vp in sorted(vendor.rglob("*")):
                if vp.is_file():
                    h.update(str(vp.relative_to(web_shell)).encode("utf-8"))
                    h.update(vp.read_bytes())
                    h.update(b"|")
        app_version = h.hexdigest()
    release_version = hashlib.sha256(
        f"{app_version}|{SCHEMA_VERSION}|{data_version}|{refresh_version}".encode("utf-8")
    ).hexdigest()
    meta["appVersion"] = app_version
    meta["releaseVersion"] = release_version
    meta["schemaVersion"] = SCHEMA_VERSION

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    # Stamp buildId on every public JSON so frontend can reject mixed generations
    watchlist_o = stamp_build_id({"tickers": tickers}, build_id)
    companies_o = stamp_build_id(companies, build_id, ticker_map=True)
    valuation_o = stamp_build_id({"rows": valuation}, build_id)
    revisions_o = stamp_build_id({"revisions": revisions}, build_id)
    eps_o = stamp_build_id(eps_history if isinstance(eps_history, dict) else {"series": eps_history}, build_id, ticker_map=True)
    earnings_o = stamp_build_id(earnings, build_id, ticker_map=True)
    alerts_o = stamp_build_id(alerts, build_id)
    meta["buildId"] = build_id

    write_json(WEB_DATA / "watchlist.json", watchlist_o)
    write_json(WEB_DATA / "meta.json", meta)
    write_json(WEB_DATA / "companies.json", companies_o)
    write_json(WEB_DATA / "valuation.json", valuation_o)
    write_json(WEB_DATA / "revisions.json", revisions_o)
    write_json(WEB_DATA / "eps_history.json", eps_o)
    write_json(WEB_DATA / "earnings.json", earnings_o)
    write_json(WEB_DATA / "alerts.json", alerts_o)

    # Prefer single atomic dashboard.json (meta/companies/valuation/revisions/epsHistory/earnings/alerts/watchlist)
    dashboard = {
        "buildId": build_id,
        "meta": meta,
        "companies": companies,
        "valuation": {"rows": valuation},
        "revisions": {"revisions": revisions},
        "epsHistory": eps_history,
        "earnings": earnings,
        "alerts": alerts,
        "watchlist": {"tickers": tickers},
    }
    write_json(WEB_DATA / "dashboard.json", dashboard)

    # Comparison checkpoint: only advance on persistent commit (not pure-build)
    try:
        if NO_PERSISTENT_MUTATION:
            cp = {
                "snapUtc": snap_utc,
                "comparisonCheckpoint": snap_utc,
                "advancedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if STAGE_DIR is not None:
                STAGE_DIR.mkdir(parents=True, exist_ok=True)
                write_json(STAGE_DIR / "pending_comparison_checkpoint.json", cp)
            print(f"  comparisonCheckpoint staged → {cp.get('snapUtc')}")
        else:
            cp = advance_comparison_checkpoint(snap_utc)
            print(f"  comparisonCheckpoint → {cp.get('snapUtc')}")
    except Exception as cp_exc:
        print(f"ERROR: failed to advance comparisonCheckpoint: {cp_exc}")
        raise

    try:
        regenerate_markdown_backups(companies, valuation, snap, tickers, year_keys=year_keys)
    except Exception as md_exc:
        # Markdown backup may exception without aborting core JSON export
        print(f"markdown backup skipped: {md_exc}")

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
    print(f"  qualityGate: {gate.get('status')} publishable={gate.get('publishable')}")
    print(f"  alertEngineStatus: {alerts.get('alertEngineStatus')} ({len(alerts.get('alerts') or [])} alerts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

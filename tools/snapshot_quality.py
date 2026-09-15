#!/usr/bin/env python3
"""Deterministic Snapshot Quality Gate (browser collection → persistent snapshot).

Validates ticker EPS payloads before treating a snapshot as successful.
Parser 0 rows must NOT write as a successful snapshot.
Extreme EPS consensus swings vs last-known-good (>30%) → needs_verification.
Full watchlist: expected vs received; missing tickers → not ok/complete.

Per-ticker LKG: Extreme EPS / Price Outlier / Fiscal Coverage use ticker-specific
last-known-good from validated snapshot history (not only the previous whole snapshot).

Fiscal identity: numeric consensus rows require normalized reportedFiscalPeriodEnding
(month+year). FY2027-only labels are normalized via universe.json fiscalEndMonthByTicker.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_MONTH = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

EXTREME_EPS_CHANGE_PCT = 30.0
MIN_EXPECTED_FISCAL_ROWS = 2
PRICE_OUTLIER_PCT = 30.0
PRICE_SCALE_FACTORS = (10.0, 100.0)
FISCAL_COVERAGE_DROP_MIN = 2


def to_num(x):
    """Parse number; reject non-finite (NaN/Infinity) — math.isfinite only."""
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "data unavailable", "null", "none", "—", "-", "unavailable", "nan", "inf", "-inf", "+inf", "infinity", "-infinity"}:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def is_explicit_unavailable(x) -> bool:
    if x is None:
        return True
    s = str(x).strip().lower()
    return s in {"", "n/a", "na", "data unavailable", "unavailable", "null", "none", "—", "-"}


def load_fiscal_end_config(root: Path | None = None) -> dict[str, int]:
    """Load ticker → fiscal-end month (1-12) from universe.json."""
    if root is None:
        root = Path(__file__).resolve().parent.parent
    path = root / "data" / "universe.json"
    if not path.exists():
        return {}
    try:
        u = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cfg = u.get("fiscalEndMonthByTicker") or {}
    out: dict[str, int] = {}
    for k, v in cfg.items():
        try:
            out[str(k).upper()] = int(v)
        except Exception:
            continue
    return out


def normalize_reported_fiscal(
    label,
    ticker: str | None = None,
    fiscal_end_by_ticker: dict[str, int] | None = None,
    slot: str | None = None,
) -> tuple[str | None, bool]:
    """Return (normalized 'Mon YYYY' or None, was_normalized).

    Accepts: 'Jan 2027', 'January 2027', '2027-01', 'FY2027', '2027E'.
    FY/year-only forms require ticker fiscal-end config (or slot year + config).
    """
    if label is None or is_explicit_unavailable(label):
        # Slot (2027E) is a calendar mapping key — NOT a Reported Fiscal Period Ending.
        # Missing fiscal identity must fail closed (do not invent from slot).
        return None, False
    s = str(label).strip()
    # Already Mon YYYY
    m = re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+(\d{4})$", s, re.I)
    if m:
        mon = _MONTH.get(m.group(1).lower()[:3], _MONTH.get(m.group(1).lower()))
        if mon:
            return f"{_MONTH_ABBR[mon]} {m.group(2)}", False
    # YYYY-MM
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        mon = int(m.group(2))
        if 1 <= mon <= 12:
            return f"{_MONTH_ABBR[mon]} {m.group(1)}", False
    # FY2027 / 2027E / 2027
    m = re.match(r"^(?:FY\s*)?(\d{4})E?$", s, re.I)
    if m:
        year = int(m.group(1))
        cfg = fiscal_end_by_ticker or {}
        mon = cfg.get(str(ticker or "").upper())
        if mon and 1 <= int(mon) <= 12:
            return f"{_MONTH_ABBR[int(mon)]} {year}", True
        return None, False
    return None, False


def fiscal_label_parsable(label) -> bool:
    if label is None:
        return False
    s = str(label).strip()
    if is_explicit_unavailable(s):
        return True
    if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4}$", s, re.I):
        return True
    if re.match(r"^\d{4}-\d{2}$", s):
        return True
    if re.match(r"^FY\s*\d{4}$", s, re.I):
        return True
    if re.match(r"^\d{4}E$", s, re.I):
        return True
    return False


def fiscal_identity_ok(label, ticker=None, fiscal_end_by_ticker=None, slot=None) -> bool:
    """True when we can produce a normalized month+year fiscal identity."""
    norm, _ = normalize_reported_fiscal(label, ticker, fiscal_end_by_ticker, slot=slot)
    return norm is not None


def calendar_mapping_from_fiscal(fiscal_label: str | None) -> str | None:
    """Jan–Mar → prior CY; else ending year. Slot mapping only."""
    if not fiscal_label:
        return None
    m = re.match(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+(\d{4})$",
        str(fiscal_label).strip(),
        re.I,
    )
    if not m:
        return None
    mon = _MONTH.get(m.group(1).lower()[:3])
    year = int(m.group(2))
    if mon is None:
        return None
    if mon <= 3:
        return f"CY{year - 1}"
    return f"CY{year}"


def validate_eps_row(
    row: dict,
    *,
    slot: str | None = None,
    ticker: str | None = None,
    fiscal_end_by_ticker: dict[str, int] | None = None,
    require_fiscal_identity: bool = True,
) -> list[str]:
    errs = []
    if not isinstance(row, dict):
        return ["row_not_object"]
    label = (
        row.get("reported_fiscal_label")
        or row.get("reportedFiscalLabel")
        or row.get("reportedFiscalPeriodEnding")
        or row.get("fiscalPeriodEnding")
    )
    cons_raw = row.get("consensus") if "consensus" in row else row.get("eps")
    cons = to_num(cons_raw)
    # Numeric consensus requires normalized month+year fiscal identity
    if cons is not None and require_fiscal_identity:
        if not fiscal_identity_ok(label, ticker, fiscal_end_by_ticker, slot=slot):
            if label is None or is_explicit_unavailable(label):
                errs.append("missing_fiscal_identity")
            else:
                errs.append(f"fiscal_identity_unnormalizable:{label}")
    elif label is not None and str(label).strip() and not fiscal_label_parsable(label):
        if not re.match(r"^\d{4}E$", str(label).strip(), re.I):
            errs.append(f"fiscal_label_unparsable:{label}")
    high = to_num(row.get("high"))
    low = to_num(row.get("low"))
    if cons is None and not is_explicit_unavailable(cons_raw):
        errs.append("consensus_not_numeric")
    if high is not None and low is not None and cons is not None:
        if not (low <= cons <= high):
            errs.append(f"dispersion_order:Low({low})<=Consensus({cons})<=High({high})")
    analysts = row.get("analysts") if "analysts" in row else row.get("analystCount")
    an = to_num(analysts)
    if (
        analysts is not None
        and not is_explicit_unavailable(analysts)
        and (an is None or an < 0 or an != int(an))
    ):
        errs.append(f"analyst_count_invalid:{analysts}")
    for key in ("rev_1M_pct", "rev1M", "rev_3M_pct", "rev3M", "rev_6M_pct", "rev6M"):
        if key not in row:
            continue
        v = row.get(key)
        if v is None:
            continue
        if isinstance(v, str) and v.strip().lower() in {
            "n/a", "na", "data unavailable", "unavailable", "—", "-"
        }:
            continue
        if to_num(v) is None:
            errs.append(f"revision_not_numeric_or_unavailable:{key}={v}")
    return errs


def apply_fiscal_normalization(
    snap: dict,
    fiscal_end_by_ticker: dict[str, int] | None = None,
) -> dict:
    """In-place normalize fiscal labels on a copy; set fiscalPeriodNormalized flags."""
    snap = dict(snap) if isinstance(snap, dict) else {"tickers": {}}
    tickers = dict(snap.get("tickers") or {})
    cfg = fiscal_end_by_ticker if fiscal_end_by_ticker is not None else load_fiscal_end_config()
    for t, td in list(tickers.items()):
        if not isinstance(td, dict):
            continue
        td = dict(td)
        eps = dict(td.get("eps") or {})
        for slot, row in list(eps.items()):
            if not isinstance(row, dict):
                continue
            row = dict(row)
            label = (
                row.get("reported_fiscal_label")
                or row.get("reportedFiscalLabel")
                or row.get("reportedFiscalPeriodEnding")
                or row.get("fiscalPeriodEnding")
            )
            norm, was_norm = normalize_reported_fiscal(label, t, cfg, slot=slot)
            if norm:
                row["reported_fiscal_label"] = norm
                row["reportedFiscalLabel"] = norm
                row["reportedFiscalPeriodEnding"] = norm
                if was_norm:
                    row["fiscalPeriodNormalized"] = True
                if not row.get("calendar_alignment") and not row.get("calendarAlignment"):
                    cm = calendar_mapping_from_fiscal(norm)
                    if cm:
                        row["calendar_alignment"] = cm
                        row["calendarAlignment"] = cm
            eps[slot] = row
        td["eps"] = eps
        tickers[t] = td
    snap["tickers"] = tickers
    return snap


def validate_ticker_payload(
    ticker: str,
    td: dict,
    *,
    min_fiscal_rows: int = MIN_EXPECTED_FISCAL_ROWS,
    fiscal_end_by_ticker: dict[str, int] | None = None,
) -> dict:
    """Validate one ticker block from a snapshot. Returns gate result dict."""
    errors: list[str] = []
    warnings: list[str] = []
    status = "ok"
    if not isinstance(td, dict):
        return {"ticker": ticker, "status": "reject", "errors": ["ticker_payload_not_object"], "warnings": []}

    price = to_num(td.get("price") if "price" in td else td.get("lastClose"))
    if price is None or price <= 0:
        errors.append(f"price_invalid:{td.get('price')}")

    eps = td.get("eps") or {}
    if not isinstance(eps, dict):
        errors.append("eps_not_object")
        eps = {}

    rows = []
    usable = []
    labels_seen = []
    for slot, row in eps.items():
        if not isinstance(row, dict):
            continue
        rows.append((slot, row))
        lab = (
            row.get("reported_fiscal_label")
            or row.get("reportedFiscalLabel")
            or row.get("reportedFiscalPeriodEnding")
        )
        if lab and not is_explicit_unavailable(lab):
            labels_seen.append(str(lab).strip())
        errors.extend([
            f"{slot}:{e}"
            for e in validate_eps_row(
                row, slot=slot, ticker=ticker, fiscal_end_by_ticker=fiscal_end_by_ticker
            )
        ])
        cons_raw = row.get("consensus") if "consensus" in row else row.get("eps")
        if to_num(cons_raw) is not None:
            usable.append((slot, row))

    if len(usable) < min_fiscal_rows:
        errors.append(f"min_fiscal_rows:{len(usable)}<{min_fiscal_rows}")

    dup = {x for x in labels_seen if labels_seen.count(x) > 1}
    if dup:
        errors.append(f"duplicate_reported_fiscal:{sorted(dup)}")

    if errors:
        status = "reject"
    return {"ticker": ticker, "status": status, "errors": errors, "warnings": warnings, "rowCount": len(usable)}


def _fiscal_label_of_eps_row(row: dict) -> str | None:
    """Normalized Reported Fiscal Period Ending identity (never mapped slot)."""
    if not isinstance(row, dict):
        return None
    for k in (
        "reportedFiscalPeriodEnding",
        "reported_fiscal_label",
        "reportedFiscalLabel",
        "fiscalPeriodEnding",
    ):
        v = row.get(k)
        if v and not is_explicit_unavailable(v):
            return str(v).strip()
    return None


def extreme_eps_change(
    ticker: str,
    new_td: dict,
    prior_td: dict | None,
    threshold_pct: float = EXTREME_EPS_CHANGE_PCT,
) -> list[dict]:
    """Extreme consensus changes vs LKG by fiscal-period identity ONLY.

    Mapped calendar slots are display-only. Different Reported Fiscal Period Ending
    → new baseline, NEVER an extreme revision of the prior mapped slot.
    """
    hits = []
    if not prior_td or not isinstance(prior_td, dict):
        return hits
    new_eps = (new_td or {}).get("eps") or {}
    old_eps = (prior_td or {}).get("eps") or {}
    prior_by_fiscal: dict[str, tuple] = {}
    if isinstance(old_eps, dict):
        for slot, orow in old_eps.items():
            if not isinstance(orow, dict):
                continue
            fiscal = _fiscal_label_of_eps_row(orow)
            if not fiscal:
                continue
            prior_by_fiscal[fiscal] = (orow, slot)
    for slot, nrow in new_eps.items():
        if not isinstance(nrow, dict):
            continue
        fiscal = _fiscal_label_of_eps_row(nrow)
        if not fiscal:
            continue
        prev = prior_by_fiscal.get(fiscal)
        if prev is None:
            continue  # new fiscal identity → not a revision / not extreme-vs-prior
        orow, prior_slot = prev
        nc = to_num(nrow.get("consensus"))
        oc = to_num(orow.get("consensus"))
        if nc is None or oc is None or oc == 0:
            continue
        change = abs(nc - oc) / abs(oc) * 100.0
        if change > threshold_pct:
            hits.append({
                "ticker": ticker,
                "slot": slot,  # display only
                "priorSlot": prior_slot,
                "fiscalPeriodEnding": fiscal,
                "priorConsensus": oc,
                "newConsensus": nc,
                "changePct": change,
                "thresholdPct": threshold_pct,
            })
    return hits


def count_usable_fiscal_periods(td: dict) -> int:
    eps = (td or {}).get("eps") or {}
    n = 0
    for row in eps.values():
        if not isinstance(row, dict):
            continue
        cons_raw = row.get("consensus") if "consensus" in row else row.get("eps")
        if to_num(cons_raw) is not None:
            n += 1
    return n


def fiscal_coverage_regression(
    ticker: str,
    new_td: dict,
    prior_td: dict | None,
) -> dict | None:
    """Sudden drop in reportedFiscalPeriodEnding coverage vs LKG without normal rollover."""
    if not prior_td or not isinstance(prior_td, dict):
        return None
    new_n = count_usable_fiscal_periods(new_td)
    old_n = count_usable_fiscal_periods(prior_td)
    if old_n < FISCAL_COVERAGE_DROP_MIN:
        return None

    def labels(td):
        out = []
        for row in ((td or {}).get("eps") or {}).values():
            if not isinstance(row, dict):
                continue
            lab = (
                row.get("reported_fiscal_label")
                or row.get("reportedFiscalLabel")
                or row.get("reportedFiscalPeriodEnding")
            )
            if lab and not is_explicit_unavailable(lab):
                out.append(str(lab).strip())
        return out

    old_labs = labels(prior_td)
    new_labs = labels(new_td)
    shared = set(old_labs) & set(new_labs)
    if new_n >= old_n - 0:
        return None
    if new_n <= old_n - FISCAL_COVERAGE_DROP_MIN or (old_n >= 4 and new_n <= old_n // 2):
        if shared and new_n >= MIN_EXPECTED_FISCAL_ROWS and abs(new_n - old_n) <= 1:
            return None
        return {
            "ticker": ticker,
            "priorPeriods": old_n,
            "newPeriods": new_n,
            "priorLabels": old_labs,
            "newLabels": new_labs,
            "reason": "fiscal_coverage_regression",
        }
    return None


def price_outlier(
    ticker: str,
    new_td: dict,
    prior_td: dict | None,
    threshold_pct: float = PRICE_OUTLIER_PCT,
) -> dict | None:
    """Abnormal Last Close vs LKG → needs_verification (not auto-reject as wrong)."""
    if not prior_td or not isinstance(prior_td, dict):
        return None
    np_ = to_num(new_td.get("price") if "price" in (new_td or {}) else (new_td or {}).get("lastClose"))
    op = to_num(prior_td.get("price") if "price" in (prior_td or {}) else (prior_td or {}).get("lastClose"))
    if np_ is None or op is None or op == 0 or np_ <= 0:
        return None
    change_pct = abs(np_ - op) / abs(op) * 100.0
    ratio = np_ / op if op else None
    scale_hit = False
    if ratio is not None:
        for f in PRICE_SCALE_FACTORS:
            if abs(ratio - f) < 0.05 * f or abs(ratio - 1.0 / f) < 0.05 / f:
                scale_hit = True
                break
            if abs(ratio - f) / f < 0.02 or abs(ratio * f - 1.0) < 0.02:
                scale_hit = True
                break
    if change_pct > threshold_pct or scale_hit:
        return {
            "ticker": ticker,
            "priorPrice": op,
            "newPrice": np_,
            "changePct": change_pct,
            "ratio": ratio,
            "scalePattern": scale_hit,
            "thresholdPct": threshold_pct,
            "reason": "price_outlier",
        }
    return None


def zero_analyst_consensus_issues(ticker: str, td: dict) -> list[dict]:
    """Numeric consensus with analystCount=0 → warning / needs_verification signal."""
    hits = []
    eps = (td or {}).get("eps") or {}
    for slot, row in eps.items():
        if not isinstance(row, dict):
            continue
        cons = to_num(row.get("consensus") if "consensus" in row else row.get("eps"))
        analysts = row.get("analysts") if "analysts" in row else row.get("analystCount")
        an = to_num(analysts)
        if cons is not None and an is not None and an == 0:
            hits.append({
                "ticker": ticker,
                "slot": slot,
                "consensus": cons,
                "analystCount": 0,
                "coverageStatus": "no_analyst_coverage",
                "reason": "consensus_with_zero_analysts",
            })
    return hits


def _snapshot_sort_key(path: Path) -> str:
    return path.stem


def list_validated_snapshots(snap_dir: Path) -> list[Path]:
    """Validated snapshot candidates only (exclude raw_/quarantine/incoming/latest/manifest)."""
    if not snap_dir.exists():
        return []
    out = []
    for p in snap_dir.glob("20*.json"):
        if p.name.startswith("raw_"):
            continue
        if p.name in {"latest.json", "manifest.json"}:
            continue
        if "quarantine" in p.parts:
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}(T\d{6}Z)?(_[A-Za-z0-9]+)?(_\d+)?\.json$", p.name):
            continue
        # Skip explicitly non-publishable stamped snapshots
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            qg = (data or {}).get("qualityGate") or {}
            if qg.get("publishable") is False:
                continue
            if qg.get("status") in {"reject", "needs_verification"}:
                continue
            utc = (data or {}).get("snapshot_utc") or (data or {}).get("collectedAt")
            if utc:
                s = str(utc).strip()
                try:
                    if s.endswith("Z"):
                        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        continue
                    if dt > datetime.now(timezone.utc) + timedelta(minutes=10):
                        continue  # future-dated must not be LKG
                except Exception:
                    continue
        except Exception:
            continue
        out.append(p)
    return sorted(out, key=_snapshot_sort_key)


def load_last_known_good_by_ticker(
    snap_dir: Path | None = None,
    *,
    exclude_path: Path | None = None,
    expected_tickers: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, dict]:
    """For each ticker, walk validated snapshots (newest→oldest) for latest successful fresh observation.

    A successful fresh observation means the ticker is present in that snapshot's tickers
    block with usable EPS (not a collectionFailed / LKG-only stub).
    Returns {TICKER: ticker_payload_dict}.
    """
    if snap_dir is None:
        if root is None:
            root = Path(__file__).resolve().parent.parent
        snap_dir = root / "data" / "snapshots"
    validated = list_validated_snapshots(snap_dir)
    # newest first
    validated = list(reversed(validated))
    exclude_res = exclude_path.resolve() if exclude_path else None
    found: dict[str, dict] = {}
    needed = set(str(t).upper() for t in (expected_tickers or []))
    for p in validated:
        if exclude_res and p.resolve() == exclude_res:
            continue
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        tickers = (snap or {}).get("tickers") or {}
        if not isinstance(tickers, dict):
            continue
        for t, td in tickers.items():
            tu = str(t).upper()
            if tu in found:
                continue
            if not isinstance(td, dict):
                continue
            if td.get("collectionFailed") or td.get("usingLastKnownGood"):
                continue
            # Must have at least one numeric consensus to count as fresh observation
            eps = td.get("eps") or {}
            has_cons = False
            if isinstance(eps, dict):
                for row in eps.values():
                    if isinstance(row, dict) and to_num(
                        row.get("consensus") if "consensus" in row else row.get("eps")
                    ) is not None:
                        has_cons = True
                        break
            if not has_cons:
                continue
            found[tu] = td
        if needed and needed.issubset(set(found.keys())):
            break
        if not needed and expected_tickers is None:
            # keep scanning until all seen tickers filled — no early break
            pass
    return found



FUTURE_SKEW_MAX = timedelta(minutes=10)
# Snapshots older than this (without --backfill) → needs_verification
STALE_SNAPSHOT_MAX_AGE = timedelta(days=14)


def parse_aware_iso8601(value) -> datetime | None:
    """Parse ISO8601 timestamp that MUST be timezone-aware. Returns None if invalid."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Accept trailing Z
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        return None  # naive rejected
    return dt


def validate_snapshot_timestamps(
    snap: dict,
    *,
    now: datetime | None = None,
    backfill: bool = False,
) -> dict | None:
    """Validate collectedAt / sourceDataAsOf / snapshot_utc.

    Returns gate override dict if reject/needs_verification, else None.
    Live production must not accept future-dated as newest LKG.
    Historical backfill only via explicit backfill=True.
    """
    now = now or datetime.now(timezone.utc)
    # Prefer collectedAt / sourceDataAsOf; fall back to snapshot_utc
    candidates = []
    for key in ("collectedAt", "sourceDataAsOf", "snapshot_utc"):
        if key in (snap or {}) and snap.get(key) is not None:
            candidates.append((key, snap.get(key)))
    if not candidates:
        # No timestamp fields: skip (unit tests). Ingest always sets snapshot_utc.
        return None
    # Validate all present timestamp fields
    parsed_primary = None
    primary_key = None
    for key, raw in candidates:
        dt = parse_aware_iso8601(raw)
        if dt is None:
            return {
                "status": "reject",
                "reason": "invalid_snapshot_timestamp",
                "message": f"{key} must be ISO8601 timezone-aware (got {raw!r})",
                "publishable": False,
            }
        if primary_key is None:
            primary_key = key
            parsed_primary = dt
        # Future skew
        if dt > now + FUTURE_SKEW_MAX:
            return {
                "status": "reject",
                "reason": "future_snapshot_timestamp",
                "message": (
                    f"{key}={raw} is > now+10min skew — live production must not "
                    "accept future-dated as newest LKG"
                ),
                "publishable": False,
            }
    assert parsed_primary is not None
    age = now - parsed_primary
    if (not backfill) and age > STALE_SNAPSHOT_MAX_AGE:
        return {
            "status": "needs_verification",
            "reason": "stale_snapshot_timestamp",
            "message": (
                f"{primary_key} age {age.days}d exceeds {STALE_SNAPSHOT_MAX_AGE.days}d — "
                "needs_verification (use --backfill for historical)"
            ),
            "publishable": False,
        }
    return None


def gate_snapshot(
    snap: dict,
    prior_snap: dict | None = None,
    *,
    min_fiscal_rows: int = MIN_EXPECTED_FISCAL_ROWS,
    expected_tickers: list[str] | None = None,
    lkg_by_ticker: dict[str, dict] | None = None,
    fiscal_end_by_ticker: dict[str, int] | None = None,
    root: Path | None = None,
    backfill: bool = False,
    now: datetime | None = None,
) -> dict:
    """Full snapshot gate. status: ok | partial | needs_verification | reject.

    - Parser/collector 0 ticker rows → reject (do not publish as successful)
    - expectedTickers missing → not ok/complete (partial if some received; reject if none)
    - Per-ticker validation failures → reject those tickers
    - Extreme EPS / price / coverage vs per-ticker LKG → needs_verification
    """
    if fiscal_end_by_ticker is None:
        fiscal_end_by_ticker = load_fiscal_end_config(root)

    # Normalize FY-only labels before validation
    snap = apply_fiscal_normalization(snap, fiscal_end_by_ticker)

    tickers = snap.get("tickers") if isinstance(snap, dict) else None
    if not isinstance(tickers, dict):
        tickers = {}

    expected = list(expected_tickers) if expected_tickers is not None else list(tickers.keys())
    expected_set = [str(t).upper() for t in expected]
    received = [str(t).upper() for t in tickers.keys()]
    received_set = set(received)
    expected_norm = set(expected_set)
    missing = sorted(expected_norm - received_set)
    unexpected = sorted(received_set - expected_norm)

    base_watch = {
        "expectedTickers": expected_set,
        "receivedTickers": received,
        "missingTickers": missing,
        "unexpectedTickers": unexpected,
    }

    if len(tickers) == 0:
        return {
            "status": "reject",
            "reason": "parser_zero_rows",
            "message": "Parser/collector produced 0 ticker rows — must NOT write as successful snapshot",
            "tickerResults": [],
            "extremeChanges": [],
            "publishable": False,
            "normalizedSnapshot": snap,
            **base_watch,
        }

    if missing and len(received) == 0:
        return {
            "status": "reject",
            "reason": "missing_watchlist_tickers",
            "message": f"Missing all expected watchlist tickers: {missing}",
            "tickerResults": [
                {"ticker": t, "status": "failed", "errors": ["missing_from_snapshot"], "warnings": []}
                for t in missing
            ],
            "extremeChanges": [],
            "publishable": False,
            "normalizedSnapshot": snap,
            **base_watch,
        }

    # Snapshot timestamp Quality Gate (after structural rejects)
    ts_fail = validate_snapshot_timestamps(snap, now=now, backfill=backfill)
    if ts_fail is not None:
        return {
            **ts_fail,
            "tickerResults": [],
            "extremeChanges": [],
            "normalizedSnapshot": snap,
            **base_watch,
        }

    # Build LKG map: prefer explicit lkg_by_ticker; else prior_snap tickers; else empty
    prior_tickers = (prior_snap or {}).get("tickers") if isinstance(prior_snap, dict) else {}
    lkg_map: dict[str, dict] = {}
    if lkg_by_ticker:
        for k, v in lkg_by_ticker.items():
            if isinstance(v, dict):
                lkg_map[str(k).upper()] = v
    if prior_tickers:
        for k, v in (prior_tickers or {}).items():
            ku = str(k).upper()
            if ku not in lkg_map and isinstance(v, dict):
                lkg_map[ku] = v

    results = []
    extreme = []
    any_reject = False
    any_extreme = False

    for t in missing:
        results.append({
            "ticker": t,
            "status": "failed",
            "errors": ["missing_from_snapshot"],
            "warnings": ["use_last_known_good"],
            "rowCount": 0,
        })

    coverage_regs = []
    price_outliers = []
    zero_analyst = []
    any_coverage = False
    any_price = False
    any_zero_an = False

    for t, td in tickers.items():
        tu = str(t).upper()
        tr = validate_ticker_payload(
            t, td, min_fiscal_rows=min_fiscal_rows, fiscal_end_by_ticker=fiscal_end_by_ticker
        )
        results.append(tr)
        if tr["status"] == "reject":
            any_reject = True
        lkg_td = lkg_map.get(tu)
        hits = extreme_eps_change(t, td, lkg_td)
        if hits:
            any_extreme = True
            extreme.extend(hits)
        cre = fiscal_coverage_regression(t, td, lkg_td)
        if cre:
            any_coverage = True
            coverage_regs.append(cre)
            tr.setdefault("warnings", []).append("fiscal_coverage_regression")
        po = price_outlier(t, td, lkg_td)
        if po:
            any_price = True
            price_outliers.append(po)
            tr.setdefault("warnings", []).append("price_outlier")
        za = zero_analyst_consensus_issues(t, td)
        if za:
            any_zero_an = True
            zero_analyst.extend(za)
            tr.setdefault("warnings", []).append("consensus_with_zero_analysts")
            for z in za:
                tr.setdefault("coverageStatus", "no_analyst_coverage")

    if any_extreme:
        status = "needs_verification"
        publishable = False
        reason = "extreme_eps_change"
        message = (
            f"EPS consensus vs last-known-good changed >{EXTREME_EPS_CHANGE_PCT}% — "
            "status=needs_verification; do not publish; re-fetch or require human confirm"
        )
    elif any_coverage:
        status = "needs_verification"
        publishable = False
        reason = "fiscal_coverage_regression"
        message = (
            "reportedFiscalPeriodEnding coverage dropped vs LKG without normal rollover — "
            "needs_verification; no direct publish"
        )
    elif any_price:
        status = "needs_verification"
        publishable = False
        reason = "price_outlier"
        message = (
            f"Last Close vs LKG abnormal (>{PRICE_OUTLIER_PCT}% or ×10/×100 scale) — "
            "needs_verification (not auto-reject as wrong)"
        )
    elif any_zero_an:
        status = "needs_verification"
        publishable = False
        reason = "consensus_with_zero_analysts"
        message = (
            "Numeric consensus with analystCount=0 — needs_verification; "
            "do not treat as normal consensus"
        )
    elif any_reject:
        status = "reject"
        publishable = False
        reason = "validation_failed"
        message = "One or more tickers failed snapshot quality validation"
    elif missing:
        status = "partial"
        publishable = True
        reason = "missing_watchlist_tickers"
        message = (
            f"Missing watchlist tickers {missing} — not COMPLETE; "
            "partial collection uses last-known-good + STALE/FAILED"
        )
    else:
        status = "ok"
        publishable = True
        reason = None
        message = "ok"

    return {
        "status": status,
        "reason": reason,
        "message": message,
        "tickerResults": results,
        "extremeChanges": extreme,
        "coverageRegressions": coverage_regs,
        "priceOutliers": price_outliers,
        "zeroAnalystConsensus": zero_analyst,
        "publishable": publishable,
        "normalizedSnapshot": snap,
        **base_watch,
    }



def immutable_quarantine_path(quarantine_dir: Path, base_name: str, snap: dict) -> Path:
    """Never overwrite an existing quarantine file for the same snapshot_utc.

    Uses content-hash / sequence suffix on collision.
    """
    quarantine_dir = Path(quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    # Stable content fingerprint (exclude gate stamps)
    payload = {k: v for k, v in (snap or {}).items() if k not in {"qualityGate", "status", "runId", "runStatus"}}
    ch = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    base = f"{base_name}.quarantine"
    qpath = quarantine_dir / base
    if not qpath.exists():
        return qpath
    try:
        existing = json.loads(qpath.read_text(encoding="utf-8"))
        ep = {k: v for k, v in (existing or {}).items() if k not in {"qualityGate", "status", "runId", "runStatus"}}
        ech = hashlib.sha256(
            json.dumps(ep, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
        ).hexdigest()[:12]
        if ech == ch:
            return qpath  # identical reject payload
    except Exception:
        pass
    alt = quarantine_dir / f"{base_name}_{ch}.quarantine"
    n = 0
    while alt.exists():
        try:
            existing = json.loads(alt.read_text(encoding="utf-8"))
            ep = {k: v for k, v in (existing or {}).items() if k not in {"qualityGate", "status", "runId", "runStatus"}}
            ech = hashlib.sha256(
                json.dumps(ep, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
            ).hexdigest()[:12]
            if ech == ch:
                return alt
        except Exception:
            pass
        n += 1
        alt = quarantine_dir / f"{base_name}_{ch}_{n}.quarantine"
    return alt

def persist_snapshot_if_ok(
    path: Path,
    snap: dict,
    prior_snap: dict | None = None,
    *,
    force: bool = False,
    expected_tickers: list[str] | None = None,
    lkg_by_ticker: dict[str, dict] | None = None,
    quarantine_dir: Path | None = None,
) -> dict:
    """Write snapshot only when gate says publishable (unless force).

    REJECT/needs_verification → quarantine only; never leave as validated snapshot candidate.
    """
    try:
        from atomic_io import atomic_write_json
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from atomic_io import atomic_write_json

    gate = gate_snapshot(
        snap, prior_snap, expected_tickers=expected_tickers, lkg_by_ticker=lkg_by_ticker
    )
    snap_out = gate.get("normalizedSnapshot") or snap
    if gate["publishable"] or force:
        if not force:
            snap_out = dict(snap_out)
            snap_out["qualityGate"] = {
                "status": gate["status"],
                "checked": True,
                "publishable": gate.get("publishable"),
                "missingTickers": gate.get("missingTickers") or [],
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, snap_out)
        gate["wrote"] = True
        gate["path"] = str(path)
    else:
        if quarantine_dir is None:
            qpath = path.with_suffix(path.suffix + ".quarantine")
            # Prefer dedicated quarantine dir when writing into snapshots/
            if path.parent.name == "snapshots":
                quarantine_dir = path.parent / "quarantine"
        if quarantine_dir is not None:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            qpath = immutable_quarantine_path(quarantine_dir, path.name, snap_out)
        else:
            qpath = immutable_quarantine_path(path.parent, path.name, snap_out)
        quarantine = dict(snap_out)
        quarantine["qualityGate"] = gate
        quarantine["status"] = gate["status"]
        atomic_write_json(qpath, quarantine)
        # Ensure no validated candidate remains at target path
        if path.exists() and "quarantine" not in path.parts:
            try:
                path.unlink()
            except Exception:
                pass
        gate["wrote"] = False
        gate["quarantinePath"] = str(qpath)
    return gate

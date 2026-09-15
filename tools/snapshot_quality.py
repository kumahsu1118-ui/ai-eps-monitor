#!/usr/bin/env python3
"""Deterministic Snapshot Quality Gate (browser collection → persistent snapshot).

Validates ticker EPS payloads before treating a snapshot as successful.
Parser 0 rows must NOT write as a successful snapshot.
Extreme EPS consensus swings vs last-known-good (>30%) → needs_verification.
Full watchlist: expected vs received; missing tickers → not ok/complete.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MONTH = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

EXTREME_EPS_CHANGE_PCT = 30.0
MIN_EXPECTED_FISCAL_ROWS = 2
PRICE_OUTLIER_PCT = 30.0  # abnormal single-day move vs LKG → needs_verification
PRICE_SCALE_FACTORS = (10.0, 100.0)  # ×10/×100/÷10/÷100 patterns
FISCAL_COVERAGE_DROP_MIN = 2  # e.g. 4→2 periods without rollover


def to_num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "data unavailable", "null", "none", "—", "-", "unavailable"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def is_explicit_unavailable(x) -> bool:
    if x is None:
        return True
    s = str(x).strip().lower()
    return s in {"", "n/a", "na", "data unavailable", "unavailable", "null", "none", "—", "-"}


def fiscal_label_parsable(label) -> bool:
    if label is None:
        return False
    s = str(label).strip()
    if is_explicit_unavailable(s):
        return True  # explicit unavailable is allowed (not a parse failure)
    if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4}$", s, re.I):
        return True
    if re.match(r"^\d{4}-\d{2}$", s):
        return True
    if re.match(r"^FY\s*\d{4}$", s, re.I):
        return True
    if re.match(r"^\d{4}E$", s, re.I):
        return True
    return False


def validate_eps_row(row: dict, *, slot: str | None = None) -> list[str]:
    errs = []
    if not isinstance(row, dict):
        return ["row_not_object"]
    label = row.get("reported_fiscal_label") or row.get("reportedFiscalLabel") or row.get("fiscalPeriodEnding")
    if label is not None and str(label).strip() and not fiscal_label_parsable(label):
        if not re.match(r"^\d{4}E$", str(label).strip(), re.I):
            errs.append(f"fiscal_label_unparsable:{label}")
    cons_raw = row.get("consensus") if "consensus" in row else row.get("eps")
    cons = to_num(cons_raw)
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


def validate_ticker_payload(ticker: str, td: dict, *, min_fiscal_rows: int = MIN_EXPECTED_FISCAL_ROWS) -> dict:
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
        lab = row.get("reported_fiscal_label") or row.get("reportedFiscalLabel")
        if lab and not is_explicit_unavailable(lab):
            labels_seen.append(str(lab).strip())
        errors.extend([f"{slot}:{e}" for e in validate_eps_row(row, slot=slot)])
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


def extreme_eps_change(
    ticker: str,
    new_td: dict,
    prior_td: dict | None,
    threshold_pct: float = EXTREME_EPS_CHANGE_PCT,
) -> list[dict]:
    """Return list of extreme consensus changes (>threshold) vs last-known-good."""
    hits = []
    if not prior_td or not isinstance(prior_td, dict):
        return hits
    new_eps = (new_td or {}).get("eps") or {}
    old_eps = (prior_td or {}).get("eps") or {}
    for slot, nrow in new_eps.items():
        if not isinstance(nrow, dict):
            continue
        orow = old_eps.get(slot)
        if not isinstance(orow, dict):
            continue
        nc = to_num(nrow.get("consensus"))
        oc = to_num(orow.get("consensus"))
        if nc is None or oc is None or oc == 0:
            continue
        change = abs(nc - oc) / abs(oc) * 100.0
        if change > threshold_pct:
            hits.append({
                "ticker": ticker,
                "slot": slot,
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
    # Rollover detection: if new labels are a shifted/superset calendar of old, OK
    def labels(td):
        out = []
        for row in ((td or {}).get("eps") or {}).values():
            if not isinstance(row, dict):
                continue
            lab = row.get("reported_fiscal_label") or row.get("reportedFiscalLabel")
            if lab and not is_explicit_unavailable(lab):
                out.append(str(lab).strip())
        return out
    old_labs = labels(prior_td)
    new_labs = labels(new_td)
    # Normal rollover: at least one shared label OR new max year > old max year with similar count
    shared = set(old_labs) & set(new_labs)
    if new_n >= old_n - 0:  # no drop
        return None
    if new_n <= old_n - FISCAL_COVERAGE_DROP_MIN or (old_n >= 4 and new_n <= old_n // 2):
        # Allow if clearly a fiscal rollover (shared nonempty and new has forward labels)
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
            # also exact-ish decade factors
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



def gate_snapshot(
    snap: dict,
    prior_snap: dict | None = None,
    *,
    min_fiscal_rows: int = MIN_EXPECTED_FISCAL_ROWS,
    expected_tickers: list[str] | None = None,
) -> dict:
    """Full snapshot gate. status: ok | partial | needs_verification | reject.

    - Parser/collector 0 ticker rows → reject (do not publish as successful)
    - expectedTickers missing → not ok/complete (partial if some received; reject if none)
    - Per-ticker validation failures → reject those tickers
    - Extreme EPS vs last-known-good → needs_verification (do not publish)
    """
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
            **base_watch,
        }

    # Missing expected watchlist tickers: not COMPLETE / not ok
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
            **base_watch,
        }

    prior_tickers = (prior_snap or {}).get("tickers") if isinstance(prior_snap, dict) else {}
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
        tr = validate_ticker_payload(t, td, min_fiscal_rows=min_fiscal_rows)
        results.append(tr)
        if tr["status"] == "reject":
            any_reject = True
        hits = extreme_eps_change(t, td, (prior_tickers or {}).get(t))
        if hits:
            any_extreme = True
            extreme.extend(hits)
        cre = fiscal_coverage_regression(t, td, (prior_tickers or {}).get(t))
        if cre:
            any_coverage = True
            coverage_regs.append(cre)
            tr.setdefault("warnings", []).append("fiscal_coverage_regression")
        po = price_outlier(t, td, (prior_tickers or {}).get(t))
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
        # Partial: some expected tickers missing — not ok/complete; still publishable
        # so export can fill last-known-good + STALE/FAILED and set collectionStatus=PARTIAL.
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
        **base_watch,
    }


def persist_snapshot_if_ok(
    path: Path,
    snap: dict,
    prior_snap: dict | None = None,
    *,
    force: bool = False,
    expected_tickers: list[str] | None = None,
) -> dict:
    """Write snapshot only when gate says publishable (unless force)."""
    try:
        from atomic_io import atomic_write_json
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from atomic_io import atomic_write_json

    gate = gate_snapshot(snap, prior_snap, expected_tickers=expected_tickers)
    if gate["publishable"] or force:
        if not force:
            snap = dict(snap)
            snap["qualityGate"] = {
                "status": gate["status"],
                "checked": True,
                "publishable": gate.get("publishable"),
                "missingTickers": gate.get("missingTickers") or [],
            }
        atomic_write_json(path, snap)
        gate["wrote"] = True
        gate["path"] = str(path)
    else:
        qpath = path.with_suffix(path.suffix + ".quarantine")
        quarantine = dict(snap)
        quarantine["qualityGate"] = gate
        quarantine["status"] = gate["status"]
        atomic_write_json(qpath, quarantine)
        gate["wrote"] = False
        gate["quarantinePath"] = str(qpath)
    return gate

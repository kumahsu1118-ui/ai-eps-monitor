#!/usr/bin/env python3
"""Snapshot quality gate — run BEFORE persisting a collected snapshot.

Hard fail (do not persist, do not run Alert Engine, snapshot not publishable):
  - empty snapshot / 0 tickers
  - parser produced 0 estimate rows
  - mixed build generation (dashboard.json vs meta.json buildId mismatch)

Quarantine (persist, stamp needs_verification, still publishable):
  - |new−old| / |old| > 30% on any mapped-year consensus
  - fiscal coverage regression (fewer non-null consensus years than prior)
  - price outlier ×10 / ×100 / large move vs prior last close
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
EXTREME_EPS_JUMP = 0.30  # 30%
PRICE_LARGE_MOVE = 0.50  # 50%
PRICE_X10 = 10.0
PRICE_X100 = 100.0

try:
    from atomic_io import atomic_write_text
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from atomic_io import atomic_write_text


class SnapshotQualityError(ValueError):
    """Hard fail — snapshot must not be persisted; Alert DB must not mutate."""


class MixedBuildError(ValueError):
    """dashboard.json / meta.json / buildId from different generations."""


def _to_num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "null", "none", "data unavailable"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _eps_map(ticker_blob: dict | None) -> dict:
    if not isinstance(ticker_blob, dict):
        return {}
    return ticker_blob.get("eps") or {}


def _filled_fiscal_slots(eps: dict) -> list[str]:
    filled = []
    for slot, year in (eps or {}).items():
        if not isinstance(year, dict):
            continue
        if _to_num(year.get("consensus")) is not None:
            filled.append(str(slot))
    return filled


def _price_of(blob: dict | None) -> float | None:
    if not isinstance(blob, dict):
        return None
    return _to_num(blob.get("price") or blob.get("lastClose") or blob.get("last_close"))


def snapshot_quality_gate(
    snapshot: dict | None,
    prior: dict | None = None,
    *,
    extreme_jump: float = EXTREME_EPS_JUMP,
    price_large_move: float = PRICE_LARGE_MOVE,
) -> dict:
    """Inspect a candidate snapshot.

    Returns dict with:
      ok (bool) — False means hard fail, do not persist
      publishable (bool)
      failReason (str|None)
      issues (list)
      quarantined (list of {ticker, slot/field, status=needs_verification, ...})
    """
    issues: list[dict] = []
    quarantined: list[dict] = []
    if not isinstance(snapshot, dict):
        return {
            "ok": False,
            "publishable": False,
            "failReason": "snapshot_not_object",
            "issues": [{"reason": "snapshot_not_object"}],
            "quarantined": [],
            "status": "reject",
        }
    tickers = snapshot.get("tickers")
    if not isinstance(tickers, dict) or len(tickers) == 0:
        return {
            "ok": False,
            "publishable": False,
            "failReason": "empty_snapshot",
            "issues": [{"reason": "empty_snapshot", "message": "0 tickers — refuse persist"}],
            "quarantined": [],
            "status": "reject",
        }

    prior_tickers = (prior or {}).get("tickers") if isinstance(prior, dict) else {}
    if not isinstance(prior_tickers, dict):
        prior_tickers = {}

    for ticker, blob in tickers.items():
        eps = _eps_map(blob if isinstance(blob, dict) else None)
        if not eps:
            issues.append(
                {
                    "ticker": ticker,
                    "reason": "zero_eps_rows",
                    "message": f"{ticker}: 0 EPS rows in snapshot",
                }
            )
            continue
        prior_blob = (prior_tickers or {}).get(ticker)
        prior_eps = _eps_map(prior_blob if isinstance(prior_blob, dict) else None)

        # Extreme EPS jump → needs_verification (keep prior consensus on apply)
        for slot, year in eps.items():
            if not isinstance(year, dict):
                continue
            new_c = _to_num(year.get("consensus"))
            old_c = _to_num(
                (prior_eps.get(slot) or {}).get("consensus") if isinstance(prior_eps.get(slot), dict) else None
            )
            if old_c is None or new_c is None or old_c == 0:
                continue
            jump = abs(new_c - old_c) / abs(old_c)
            if jump > extreme_jump:
                quarantined.append(
                    {
                        "ticker": ticker,
                        "slot": slot,
                        "field": "consensus",
                        "status": "needs_verification",
                        "reason": "extreme_eps_change",
                        "previousConsensus": old_c,
                        "rejectedConsensus": new_c,
                        "jumpPct": round(jump * 100.0, 4),
                    }
                )

        # Fiscal coverage regression → needs_verification
        prior_filled = _filled_fiscal_slots(prior_eps)
        new_filled = _filled_fiscal_slots(eps)
        if prior_filled and len(new_filled) < len(prior_filled):
            lost = sorted(set(prior_filled) - set(new_filled))
            quarantined.append(
                {
                    "ticker": ticker,
                    "field": "fiscalCoverage",
                    "status": "needs_verification",
                    "reason": "fiscal_coverage_regression",
                    "previousSlots": prior_filled,
                    "currentSlots": new_filled,
                    "lostSlots": lost,
                }
            )

        # Price outlier ×10 / ×100 / large move → needs_verification
        old_px = _price_of(prior_blob if isinstance(prior_blob, dict) else None)
        new_px = _price_of(blob if isinstance(blob, dict) else None)
        if old_px is not None and new_px is not None and old_px != 0:
            ratio = abs(new_px / old_px)
            move = abs(new_px - old_px) / abs(old_px)
            reason = None
            if ratio >= PRICE_X100 or ratio <= (1.0 / PRICE_X100):
                reason = "price_outlier_x100"
            elif ratio >= PRICE_X10 or ratio <= (1.0 / PRICE_X10):
                reason = "price_outlier_x10"
            elif move > price_large_move:
                reason = "price_outlier_large_move"
            if reason:
                quarantined.append(
                    {
                        "ticker": ticker,
                        "field": "price",
                        "status": "needs_verification",
                        "reason": reason,
                        "previousPrice": old_px,
                        "rejectedPrice": new_px,
                        "ratio": round(ratio, 6),
                        "movePct": round(move * 100.0, 4),
                    }
                )

    if all(not _eps_map(b if isinstance(b, dict) else None) for b in tickers.values()):
        return {
            "ok": False,
            "publishable": False,
            "failReason": "parser_zero_rows",
            "issues": issues or [{"reason": "parser_zero_rows"}],
            "quarantined": quarantined,
            "status": "reject",
        }

    return {
        "ok": True,
        "publishable": True,
        "failReason": None,
        "issues": issues,
        "quarantined": quarantined,
        "status": "pass" if not quarantined else "needs_verification",
    }


def apply_quarantine(snapshot: dict, gate: dict) -> dict:
    """Replace extreme consensus/price with prior canonical value; stamp needs_verification."""
    out = copy.deepcopy(snapshot)
    tickers = out.setdefault("tickers", {})
    for q in gate.get("quarantined") or []:
        t = q.get("ticker")
        blob = tickers.get(t)
        if not isinstance(blob, dict):
            continue
        blob.setdefault("needs_verification", True)
        blob.setdefault("quarantine", []).append(q)
        reason = q.get("reason") or ""
        if q.get("field") == "price" or str(reason).startswith("price_outlier"):
            if q.get("previousPrice") is not None:
                blob["price"] = q.get("previousPrice")
            blob["priceNeedsVerification"] = True
            blob["rejectedPrice"] = q.get("rejectedPrice")
            blob["priceQuarantineReason"] = reason
            continue
        if q.get("field") == "fiscalCoverage" or reason == "fiscal_coverage_regression":
            blob["fiscalCoverageNeedsVerification"] = True
            blob["fiscalCoverageStatus"] = "needs_verification"
            continue
        slot = q.get("slot")
        if not slot:
            continue
        eps = blob.setdefault("eps", {})
        year = eps.get(slot)
        if not isinstance(year, dict):
            year = {}
            eps[slot] = year
        if q.get("previousConsensus") is not None:
            year["consensus"] = q.get("previousConsensus")
        year["needs_verification"] = True
        year["quarantineStatus"] = "needs_verification"
        year["quarantineReason"] = reason or "extreme_eps_change"
        year["rejectedConsensus"] = q.get("rejectedConsensus")
    if gate.get("quarantined"):
        out["qualityGate"] = {
            "status": "needs_verification",
            "publishable": True,
            "quarantinedCount": len(gate["quarantined"]),
            "quarantined": gate["quarantined"],
        }
    else:
        out.setdefault(
            "qualityGate",
            {"status": gate.get("status") or "pass", "publishable": True, "quarantined": []},
        )
    return out


def persist_snapshot(
    path: Path | str,
    snapshot: dict,
    prior: dict | None = None,
    *,
    extreme_jump: float = EXTREME_EPS_JUMP,
) -> dict:
    """Quality-gate then atomically persist. Raises SnapshotQualityError on hard fail.

    Rejected snapshots are NOT written. Callers must not run the Alert Engine.
    """
    path = Path(path)
    gate = snapshot_quality_gate(snapshot, prior, extreme_jump=extreme_jump)
    if not gate.get("ok") or not gate.get("publishable"):
        gate["persisted"] = False
        gate["publishable"] = False
        raise SnapshotQualityError(gate.get("failReason") or "quality_gate_failed")
    to_write = apply_quarantine(snapshot, gate)
    atomic_write_text(path, json.dumps(to_write, indent=2, ensure_ascii=False) + "\n")
    gate["persisted"] = True
    gate["publishable"] = True
    gate["path"] = str(path)
    gate["written"] = to_write
    return gate


def persist_raw_snapshot(
    snap_dir: Path | str,
    snapshot: dict,
    *,
    when: datetime | None = None,
) -> Path:
    """Write a timestamped raw snapshot. Same-day files are preserved (never overwrite)."""
    snap_dir = Path(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)
    stamp = utc.strftime("%Y%m%dT%H%M%SZ")
    path = snap_dir / f"raw_{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = snap_dir / f"raw_{stamp}_{n}.json"
    atomic_write_text(path, json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    return path


def check_build_generation_consistency(
    web_data: Path | str,
    *,
    dashboard_name: str = "dashboard.json",
    meta_name: str = "meta.json",
) -> dict:
    """Reject mixed generations: dashboard.json buildId must match meta.json buildId.

    Missing dashboard.json on a pre-gate tree is allowed (nothing to mix).
    If both exist, buildId (or dataVersion) must be identical and non-empty.
    """
    web_data = Path(web_data)
    dash_path = web_data / dashboard_name
    meta_path = web_data / meta_name
    if not dash_path.exists() or not meta_path.exists():
        return {"ok": True, "reason": "incomplete_pair", "publishable": True}

    def _load(p: Path) -> dict:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MixedBuildError(f"unreadable {p.name}: {exc}") from exc
        if not isinstance(obj, dict):
            raise MixedBuildError(f"{p.name} is not an object")
        return obj

    dash = _load(dash_path)
    meta = _load(meta_path)
    dash_id = dash.get("buildId") or dash.get("dataVersion")
    meta_id = meta.get("buildId") or meta.get("dataVersion")
    if not dash_id or not meta_id:
        raise MixedBuildError("missing buildId on dashboard.json or meta.json")
    if str(dash_id) != str(meta_id):
        raise MixedBuildError(
            f"mixed build generation: dashboard.buildId={dash_id} meta.buildId={meta_id}"
        )
    # Optional extra files stamped with buildId
    for extra in ("companies.json", "alerts.json"):
        p = web_data / extra
        if not p.exists():
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        extra_id = None
        if isinstance(obj, dict):
            extra_id = obj.get("buildId") or obj.get("_buildId")
        if extra_id and str(extra_id) != str(meta_id):
            raise MixedBuildError(f"mixed build generation: {extra} buildId={extra_id} meta.buildId={meta_id}")
    return {"ok": True, "buildId": str(meta_id), "publishable": True}


def load_prior_snapshot(snap_dir: Path | str, exclude: Path | str | None = None) -> dict | None:
    """Latest dated canonical snapshot, skipping raw_* and optionally `exclude`."""
    snap_dir = Path(snap_dir)
    if not snap_dir.exists():
        return None
    exclude_name = Path(exclude).name if exclude else None
    dated = sorted(
        p
        for p in snap_dir.glob("20*.json")
        if not p.name.startswith("raw_") and re.match(r"^\d{4}-\d{2}-\d{2}", p.name)
        and p.name != exclude_name
    )
    if not dated:
        return None
    try:
        return json.loads(dated[-1].read_text(encoding="utf-8"))
    except Exception:
        return None

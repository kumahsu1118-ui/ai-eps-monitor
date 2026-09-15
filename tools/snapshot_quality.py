#!/usr/bin/env python3
"""Snapshot quality gate — run BEFORE persisting a collected snapshot.

Hard fail (do not persist):
  - empty snapshot / 0 tickers
  - parser produced 0 estimate rows (see sa_parser.ParserZeroRowsError)

Quarantine (persist prior canonical EPS, mark needs_verification):
  - |new−old| / |old| > 30% on any mapped-year consensus
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

EXTREME_EPS_JUMP = 0.30  # 30%

try:
    from atomic_io import atomic_write_text
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from atomic_io import atomic_write_text


class SnapshotQualityError(ValueError):
    """Hard fail — snapshot must not be persisted."""


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


def snapshot_quality_gate(
    snapshot: dict | None,
    prior: dict | None = None,
    *,
    extreme_jump: float = EXTREME_EPS_JUMP,
) -> dict:
    """Inspect a candidate snapshot.

    Returns dict with:
      ok (bool) — False means hard fail, do not persist
      failReason (str|None)
      issues (list)
      quarantined (list of {ticker, slot, status=needs_verification, ...})
    """
    issues: list[dict] = []
    quarantined: list[dict] = []
    if not isinstance(snapshot, dict):
        return {
            "ok": False,
            "failReason": "snapshot_not_object",
            "issues": [{"reason": "snapshot_not_object"}],
            "quarantined": [],
        }
    tickers = snapshot.get("tickers")
    if not isinstance(tickers, dict) or len(tickers) == 0:
        return {
            "ok": False,
            "failReason": "empty_snapshot",
            "issues": [{"reason": "empty_snapshot", "message": "0 tickers — refuse persist"}],
            "quarantined": [],
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
        prior_eps = _eps_map((prior_tickers or {}).get(ticker))
        for slot, year in eps.items():
            if not isinstance(year, dict):
                continue
            new_c = _to_num(year.get("consensus"))
            old_c = _to_num((prior_eps.get(slot) or {}).get("consensus") if isinstance(prior_eps.get(slot), dict) else None)
            if old_c is None or new_c is None:
                continue
            if old_c == 0:
                continue
            jump = abs(new_c - old_c) / abs(old_c)
            if jump > extreme_jump:
                quarantined.append(
                    {
                        "ticker": ticker,
                        "slot": slot,
                        "status": "needs_verification",
                        "reason": "extreme_eps_change",
                        "previousConsensus": old_c,
                        "rejectedConsensus": new_c,
                        "jumpPct": round(jump * 100.0, 4),
                    }
                )

    # 0-row across ALL tickers is a hard fail (parser produced nothing usable)
    if all(not _eps_map(b if isinstance(b, dict) else None) for b in tickers.values()):
        return {
            "ok": False,
            "failReason": "parser_zero_rows",
            "issues": issues or [{"reason": "parser_zero_rows"}],
            "quarantined": quarantined,
        }

    return {
        "ok": True,
        "failReason": None,
        "issues": issues,
        "quarantined": quarantined,
    }


def apply_quarantine(snapshot: dict, gate: dict) -> dict:
    """Replace extreme consensus with prior canonical value; stamp needs_verification."""
    out = copy.deepcopy(snapshot)
    tickers = out.setdefault("tickers", {})
    for q in gate.get("quarantined") or []:
        t = q.get("ticker")
        slot = q.get("slot")
        blob = tickers.get(t)
        if not isinstance(blob, dict):
            continue
        eps = blob.setdefault("eps", {})
        year = eps.get(slot)
        if not isinstance(year, dict):
            year = {}
            eps[slot] = year
        year["consensus"] = q.get("previousConsensus")
        year["needs_verification"] = True
        year["quarantineStatus"] = "needs_verification"
        year["quarantineReason"] = q.get("reason") or "extreme_eps_change"
        year["rejectedConsensus"] = q.get("rejectedConsensus")
        blob.setdefault("needs_verification", True)
        blob.setdefault("quarantine", []).append(q)
    if gate.get("quarantined"):
        out["qualityGate"] = {
            "quarantinedCount": len(gate["quarantined"]),
            "quarantined": gate["quarantined"],
        }
    return out


def persist_snapshot(
    path: Path | str,
    snapshot: dict,
    prior: dict | None = None,
    *,
    extreme_jump: float = EXTREME_EPS_JUMP,
) -> dict:
    """Quality-gate then atomically persist. Raises SnapshotQualityError on hard fail.

    Extreme EPS jumps are quarantined (prior consensus kept, needs_verification set)
    rather than published as canonical.
    """
    path = Path(path)
    gate = snapshot_quality_gate(snapshot, prior, extreme_jump=extreme_jump)
    if not gate.get("ok"):
        raise SnapshotQualityError(gate.get("failReason") or "quality_gate_failed")
    to_write = apply_quarantine(snapshot, gate)
    atomic_write_text(path, json.dumps(to_write, indent=2, ensure_ascii=False) + "\n")
    gate["persisted"] = True
    gate["path"] = str(path)
    gate["written"] = to_write
    return gate

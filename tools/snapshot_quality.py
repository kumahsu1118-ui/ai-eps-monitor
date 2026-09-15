#!/usr/bin/env python3
"""Snapshot / export quality gate — fail-closed.

Hard reject (publishable=False): empty snapshot, 0 expected tickers received,
parser produced 0 usable EPS rows.

Partial (publishable=True, collectionStatus != complete): missing expected
watchlist tickers. Caller fills those names from last-known-good + badge.

Quarantine: |new−old|/|old| > 30% EPS jump keeps prior consensus and stamps
needs_verification. Extreme jumps alone do not reject the whole snapshot.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXTREME_EPS_JUMP = 0.30  # 30%

try:
    from atomic_io import atomic_write_text
except ImportError:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import atomic_write_text


class SnapshotQualityError(ValueError):
    """Hard fail — snapshot must not be persisted / published."""


class QualityGateReject(RuntimeError):
    """Export/publish blocked: qualityGate.publishable is not True."""

    def __init__(self, gate: dict):
        self.gate = gate
        super().__init__(gate.get("reason") or gate.get("message") or "quality_gate_rejected")


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


def _usable_eps(ticker_blob: dict | None) -> bool:
    eps = _eps_map(ticker_blob)
    if not isinstance(eps, dict) or not eps:
        return False
    for year in eps.values():
        if not isinstance(year, dict):
            continue
        if _to_num(year.get("consensus")) is not None:
            return True
    return False


def snapshot_quality_gate(
    snapshot: dict | None,
    prior: dict | None = None,
    *,
    expected_tickers: list[str] | None = None,
    extreme_jump: float = EXTREME_EPS_JUMP,
) -> dict:
    """Inspect a candidate snapshot. See module docstring."""
    issues: list[dict] = []
    quarantined: list[dict] = []
    expected = [str(t).strip().upper() for t in (expected_tickers or []) if str(t).strip()]

    def _reject(reason: str, message: str, extra: dict | None = None) -> dict:
        out = {
            "ok": False,
            "status": "reject",
            "reason": reason,
            "publishable": False,
            "failReason": reason,
            "issues": issues or [{"reason": reason, "message": message}],
            "quarantined": quarantined,
            "extremeChanges": quarantined,
            "expectedTickers": expected,
            "receivedTickers": [],
            "missingTickers": list(expected),
            "unexpectedTickers": [],
            "message": message,
        }
        if extra:
            out.update(extra)
        return out

    if not isinstance(snapshot, dict):
        return _reject("snapshot_not_object", "snapshot is not an object")

    tickers = snapshot.get("tickers")
    if not isinstance(tickers, dict) or len(tickers) == 0:
        return _reject("empty_snapshot", "0 tickers — refuse persist/publish")

    received = [str(t).strip().upper() for t in tickers.keys()]
    received_set = set(received)
    expected_set = set(expected)
    missing = [t for t in expected if t not in received_set]
    unexpected = [t for t in received if expected_set and t not in expected_set]

    prior_tickers = (prior or {}).get("tickers") if isinstance(prior, dict) else {}
    if not isinstance(prior_tickers, dict):
        prior_tickers = {}

    usable_any = False
    for ticker, blob in tickers.items():
        eps = _eps_map(blob if isinstance(blob, dict) else None)
        if _usable_eps(blob if isinstance(blob, dict) else None):
            usable_any = True
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
            old_slot = prior_eps.get(slot) if isinstance(prior_eps.get(slot), dict) else None
            old_c = _to_num((old_slot or {}).get("consensus"))
            if old_c is None or new_c is None or old_c == 0:
                continue
            jump = abs(new_c - old_c) / abs(old_c)
            if jump > extreme_jump:
                quarantined.append(
                    {
                        "ticker": str(ticker).upper(),
                        "slot": slot,
                        "status": "needs_verification",
                        "reason": "extreme_eps_change",
                        "previousConsensus": old_c,
                        "rejectedConsensus": new_c,
                        "jumpPct": round(jump * 100.0, 4),
                    }
                )

    if all(not _eps_map(b if isinstance(b, dict) else None) for b in tickers.values()):
        return _reject(
            "parser_zero_rows",
            "parser produced 0 EPS rows — refuse persist/publish",
            extra={
                "receivedTickers": received,
                "missingTickers": missing,
                "unexpectedTickers": unexpected,
            },
        )

    if expected and not any(t in received_set for t in expected):
        return _reject(
            "missing_all_expected_tickers",
            "none of the watchlist expectedTickers are in the snapshot",
            extra={
                "receivedTickers": received,
                "missingTickers": missing,
                "unexpectedTickers": unexpected,
            },
        )

    if expected and not usable_any:
        # Received names but no usable consensus — still not publishable
        return _reject(
            "no_usable_consensus",
            "no usable consensus EPS for expected tickers",
            extra={
                "receivedTickers": received,
                "missingTickers": missing,
                "unexpectedTickers": unexpected,
            },
        )

    status = "ok"
    reason = None
    message = "ok"
    if missing:
        status = "partial"
        reason = "missing_watchlist_tickers"
        message = f"missing expectedTickers: {', '.join(missing)}"

    return {
        "ok": True,
        "status": status,
        "reason": reason,
        "publishable": True,
        "failReason": None,
        "issues": issues,
        "quarantined": quarantined,
        "extremeChanges": quarantined,
        "expectedTickers": expected,
        "receivedTickers": received,
        "missingTickers": missing,
        "unexpectedTickers": unexpected,
        "message": message,
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


def quarantine_invalid_snapshot(snap_path: Path, snapshot: dict, gate: dict) -> Path:
    """Copy invalid snapshot into data/snapshots/quarantine/. Keep original bytes."""
    snap_path = Path(snap_path)
    qdir = snap_path.parent / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = qdir / f"{snap_path.stem}.{stamp}{snap_path.suffix or '.json'}"
    payload = {
        "quarantinedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sourcePath": str(snap_path),
        "qualityGate": gate,
        "snapshot": snapshot,
    }
    atomic_write_text(dest, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    gate_path = dest.with_suffix(dest.suffix + ".gate.json")
    atomic_write_text(gate_path, json.dumps(gate, indent=2, ensure_ascii=False) + "\n")
    return dest


def persist_snapshot(
    path: Path | str,
    snapshot: dict,
    prior: dict | None = None,
    *,
    expected_tickers: list[str] | None = None,
    extreme_jump: float = EXTREME_EPS_JUMP,
) -> dict:
    """Quality-gate then atomically persist. Raises SnapshotQualityError on hard fail."""
    path = Path(path)
    gate = snapshot_quality_gate(
        snapshot, prior, expected_tickers=expected_tickers, extreme_jump=extreme_jump
    )
    if not gate.get("ok") or gate.get("publishable") is not True:
        raise SnapshotQualityError(gate.get("failReason") or gate.get("reason") or "quality_gate_failed")
    to_write = apply_quarantine(snapshot, gate)
    atomic_write_text(path, json.dumps(to_write, indent=2, ensure_ascii=False) + "\n")
    gate["persisted"] = True
    gate["path"] = str(path)
    gate["written"] = to_write
    return gate

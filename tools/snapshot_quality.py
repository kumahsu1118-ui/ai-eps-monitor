#!/usr/bin/env python3
"""Snapshot quality gate, per-ticker last-known-good (LKG), and validated listing.

Incoming collection artifacts are NOT canonical. Only snapshots that pass this
gate are written to data/snapshots/. Rejects go to data/quarantine/.

load_latest / validated history read ONLY data/snapshots/ (never incoming,
never quarantine, never raw_*).
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json, atomic_write_text
except ImportError:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import atomic_write_json, atomic_write_text


EXTREME_EPS_JUMP = 0.30  # 30%
PRICE_LARGE_MOVE = 0.50
PRICE_X10 = 10.0
PRICE_X100 = 100.0

VALIDATED_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.*\.json$")


class SnapshotQualityError(ValueError):
    """Hard fail — snapshot must not be persisted to validated history."""


def to_num(x):
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
    return ticker_blob.get("eps") if isinstance(ticker_blob.get("eps"), dict) else {}


def _price_of(blob: dict | None) -> float | None:
    if not isinstance(blob, dict):
        return None
    return to_num(blob.get("price") or blob.get("lastClose") or blob.get("last_close"))


def _fiscal_label(year: dict | None) -> str | None:
    if not isinstance(year, dict):
        return None
    lab = year.get("reported_fiscal_label") or year.get("reportedFiscalLabel") or year.get("fiscalPeriodEnding")
    if lab is None:
        return None
    s = str(lab).strip()
    if not s or s.lower() in {"data unavailable", "n/a", "na", "none", "null"}:
        return None
    return s


def _filled_slots(eps: dict) -> list[str]:
    filled = []
    for slot, year in (eps or {}).items():
        if not isinstance(year, dict):
            continue
        if to_num(year.get("consensus")) is not None:
            filled.append(str(slot))
    return filled


def _analysts_of(year: dict | None) -> float | None:
    if not isinstance(year, dict):
        return None
    return to_num(year.get("analysts") or year.get("analystCount") or year.get("analyst_count"))


def list_validated_snapshot_paths(snap_dir: Path | str) -> list[Path]:
    """Canonical validated snapshots only (data/snapshots/).

    Excludes incoming/, quarantine/, raw_*, and non-dated files.
    """
    snap_dir = Path(snap_dir)
    if not snap_dir.exists():
        return []
    out = []
    for p in snap_dir.iterdir():
        if not p.is_file() or p.suffix.lower() != ".json":
            continue
        if p.name.startswith("raw_") or p.name.startswith("."):
            continue
        if not VALIDATED_NAME_RE.match(p.name):
            continue
        out.append(p)
    return sorted(out)


def load_latest_validated_snapshot(snap_dir: Path | str) -> tuple[Path, dict] | tuple[None, None]:
    dated = list_validated_snapshot_paths(snap_dir)
    if not dated:
        return None, None
    path = dated[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def load_universe(root: Path | str) -> dict:
    root = Path(root)
    for cand in (root / "data" / "universe.json", root / "fixtures" / "data" / "universe.json"):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def universe_fiscal_end(universe: dict | None) -> dict:
    """Map TICKER → {month, label, ...} from universe.json."""
    u = universe or {}
    raw = u.get("fiscalEnd") or u.get("fiscal_end") or u.get("fiscalEnds") or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        ticker = str(k).upper()
        if isinstance(v, dict):
            out[ticker] = dict(v)
        elif isinstance(v, (int, float)):
            out[ticker] = {"month": int(v)}
        elif isinstance(v, str):
            out[ticker] = {"label": v}
    return out


def build_per_ticker_lkg(snap_dir: Path | str) -> dict:
    """Walk validated snapshots oldest→newest; keep last good EPS/price/coverage.

    Missing / collection-failed tickers and null consensus slots do NOT replace
    prior LKG. That is what makes 19.38 → missing → 1.938 compare against 19.38.
    """
    lkg: dict = {"tickers": {}, "sourceSnapshots": []}
    for path in list_validated_snapshot_paths(snap_dir):
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(snap, dict):
            continue
        lkg["sourceSnapshots"].append(path.name)
        tickers = snap.get("tickers") if isinstance(snap.get("tickers"), dict) else {}
        for ticker, blob in tickers.items():
            if not isinstance(blob, dict):
                continue
            if blob.get("collection_failed") or blob.get("collectionFailed"):
                continue
            dest = lkg["tickers"].setdefault(str(ticker).upper(), {"eps": {}, "price": None, "filledSlots": []})
            px = _price_of(blob)
            if px is not None:
                dest["price"] = px
                dest["price_as_of"] = blob.get("price_as_of")
            eps = _eps_map(blob)
            for slot, year in eps.items():
                if not isinstance(year, dict):
                    continue
                cons = to_num(year.get("consensus"))
                if cons is None:
                    continue
                # Stored consensus is the kept value (LKG fill or verified).
                dest["eps"][str(slot)] = copy.deepcopy(year)
            dest["filledSlots"] = _filled_slots(dest.get("eps") or {})
            dest["lastSnapshot"] = path.name
    return lkg


def _lkg_consensus(lkg: dict, ticker: str, slot: str) -> float | None:
    year = (((lkg.get("tickers") or {}).get(str(ticker).upper()) or {}).get("eps") or {}).get(slot)
    if isinstance(year, dict):
        return to_num(year.get("consensus"))
    return None


def _lkg_year(lkg: dict, ticker: str, slot: str) -> dict:
    year = (((lkg.get("tickers") or {}).get(str(ticker).upper()) or {}).get("eps") or {}).get(slot)
    return year if isinstance(year, dict) else {}


def _lkg_price(lkg: dict, ticker: str) -> float | None:
    return to_num(((lkg.get("tickers") or {}).get(str(ticker).upper()) or {}).get("price"))


def _lkg_filled(lkg: dict, ticker: str) -> list[str]:
    t = (lkg.get("tickers") or {}).get(str(ticker).upper()) or {}
    slots = t.get("filledSlots")
    if isinstance(slots, list) and slots:
        return [str(s) for s in slots]
    return _filled_slots(t.get("eps") or {})


def snapshot_quality_gate(
    snapshot: dict | None,
    prior: dict | None = None,
    *,
    lkg: dict | None = None,
    universe: dict | None = None,
    extreme_jump: float = EXTREME_EPS_JUMP,
    price_large_move: float = PRICE_LARGE_MOVE,
) -> dict:
    """Inspect a candidate incoming snapshot.

    Hard fail (ok=False, persist to quarantine only):
      - not an object / empty tickers / parser 0 rows
      - missing fiscal identity on any consensus row
    Soft (ok=True, needs_verification on fields, still validated):
      - extreme EPS vs per-ticker LKG
      - price outlier vs LKG
      - coverage regression vs LKG
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

    if lkg is None and isinstance(prior, dict) and "tickers" in prior and "sourceSnapshots" not in prior:
        # Treat a prior snapshot as a one-frame LKG.
        lkg = {"tickers": {}}
        for t, blob in (prior.get("tickers") or {}).items():
            if not isinstance(blob, dict):
                continue
            lkg["tickers"][str(t).upper()] = {
                "eps": copy.deepcopy(_eps_map(blob)),
                "price": _price_of(blob),
                "filledSlots": _filled_slots(_eps_map(blob)),
            }
    lkg = lkg or {"tickers": {}}
    fiscal_end = universe_fiscal_end(universe)

    missing_fiscal = []
    for ticker, blob in tickers.items():
        eps = _eps_map(blob if isinstance(blob, dict) else None)
        if not eps:
            issues.append({"ticker": ticker, "reason": "zero_eps_rows", "message": f"{ticker}: 0 EPS rows"})
            continue
        for slot, year in eps.items():
            if not isinstance(year, dict):
                continue
            cons = to_num(year.get("consensus"))
            if cons is None:
                continue
            lab = _fiscal_label(year)
            if not lab:
                missing_fiscal.append({"ticker": ticker, "slot": slot, "reason": "missing_fiscal_identity"})

    if missing_fiscal:
        return {
            "ok": False,
            "publishable": False,
            "failReason": "missing_fiscal_identity",
            "issues": missing_fiscal,
            "quarantined": [],
            "status": "reject",
            "fiscalEnd": fiscal_end,
        }

    if all(not _eps_map(b if isinstance(b, dict) else None) for b in tickers.values()):
        return {
            "ok": False,
            "publishable": False,
            "failReason": "parser_zero_rows",
            "issues": issues or [{"reason": "parser_zero_rows"}],
            "quarantined": [],
            "status": "reject",
        }

    for ticker, blob in tickers.items():
        if not isinstance(blob, dict):
            continue
        tkey = str(ticker).upper()
        eps = _eps_map(blob)
        prior_filled = _lkg_filled(lkg, tkey)
        new_filled = _filled_slots(eps)
        collection_failed = bool(blob.get("collection_failed") or blob.get("collectionFailed"))

        for slot, year in eps.items():
            if not isinstance(year, dict):
                continue
            new_c = to_num(year.get("consensus"))
            old_c = _lkg_consensus(lkg, tkey, slot)
            if old_c is not None and new_c is not None and old_c != 0:
                jump = abs(new_c - old_c) / abs(old_c)
                if jump > extreme_jump:
                    quarantined.append(
                        {
                            "ticker": tkey,
                            "slot": slot,
                            "field": "consensus",
                            "status": "needs_verification",
                            "reason": "extreme_eps_change",
                            "previousConsensus": old_c,
                            "rejectedConsensus": new_c,
                            "jumpPct": round(jump * 100.0, 4),
                            "lkgConsensus": old_c,
                        }
                    )
            analysts = _analysts_of(year)
            old_year = _lkg_year(lkg, tkey, slot)
            old_an = _analysts_of(old_year)
            if old_an is not None and old_an >= 5 and analysts is not None and analysts == 0 and new_c is not None:
                quarantined.append(
                    {
                        "ticker": tkey,
                        "slot": slot,
                        "field": "coverage",
                        "status": "needs_verification",
                        "reason": "coverage_collapse",
                        "previousAnalysts": old_an,
                        "rejectedAnalysts": analysts,
                    }
                )

        if prior_filled and (collection_failed or len(new_filled) < len(prior_filled)):
            lost = sorted(set(prior_filled) - set(new_filled))
            if lost or collection_failed:
                quarantined.append(
                    {
                        "ticker": tkey,
                        "field": "fiscalCoverage",
                        "status": "needs_verification",
                        "reason": "fiscal_coverage_regression" if lost else "partial_collection",
                        "previousSlots": prior_filled,
                        "currentSlots": new_filled,
                        "lostSlots": lost,
                    }
                )

        old_px = _lkg_price(lkg, tkey)
        new_px = _price_of(blob)
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
                        "ticker": tkey,
                        "field": "price",
                        "status": "needs_verification",
                        "reason": reason,
                        "previousPrice": old_px,
                        "rejectedPrice": new_px,
                        "ratio": round(ratio, 6),
                        "movePct": round(move * 100.0, 4),
                    }
                )

    return {
        "ok": True,
        "publishable": True,
        "failReason": None,
        "issues": issues,
        "quarantined": quarantined,
        "status": "pass" if not quarantined else "needs_verification",
        "fiscalEnd": fiscal_end,
    }


def apply_lkg_and_quarantine(snapshot: dict, gate: dict, lkg: dict | None = None) -> dict:
    """Keep LKG consensus/price on extreme/missing fields; stamp needs_verification."""
    out = copy.deepcopy(snapshot)
    tickers = out.setdefault("tickers", {})
    lkg = lkg or {"tickers": {}}

    # Fill missing tickers/slots from LKG (partial collection).
    for tkey, lkg_blob in (lkg.get("tickers") or {}).items():
        blob = tickers.get(tkey)
        if blob is None:
            blob = {
                "collection_failed": True,
                "data_gaps": ["partial collection — filled from per-ticker LKG"],
                "eps": {},
                "lkgFill": True,
            }
            tickers[tkey] = blob
        if not isinstance(blob, dict):
            continue
        eps = blob.setdefault("eps", {})
        lkg_eps = lkg_blob.get("eps") or {}
        for slot, year in lkg_eps.items():
            cur = eps.get(slot)
            cur_cons = to_num(cur.get("consensus")) if isinstance(cur, dict) else None
            if cur_cons is None and isinstance(year, dict) and to_num(year.get("consensus")) is not None:
                filled = copy.deepcopy(year)
                filled["lkgFill"] = True
                filled["source"] = filled.get("source") or "last_known_good"
                eps[slot] = filled
                blob.setdefault("lkgFilledSlots", []).append(slot)
        if blob.get("collection_failed") and _price_of(blob) is None and lkg_blob.get("price") is not None:
            blob["price"] = lkg_blob.get("price")
            blob["priceLkgFill"] = True

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
        if q.get("field") == "fiscalCoverage" or reason in {"fiscal_coverage_regression", "partial_collection"}:
            blob["fiscalCoverageNeedsVerification"] = True
            blob["fiscalCoverageStatus"] = "needs_verification"
            # Restore lost slots from LKG
            for slot in q.get("lostSlots") or []:
                lkg_year = _lkg_year(lkg, t, slot)
                if lkg_year and to_num(lkg_year.get("consensus")) is not None:
                    year = copy.deepcopy(lkg_year)
                    year["lkgFill"] = True
                    year["needs_verification"] = True
                    year["quarantineStatus"] = "needs_verification"
                    year["quarantineReason"] = reason
                    blob.setdefault("eps", {})[slot] = year
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
        if q.get("field") == "coverage":
            year["coverageNeedsVerification"] = True

    out["qualityGate"] = {
        "status": gate.get("status") or ("needs_verification" if gate.get("quarantined") else "pass"),
        "publishable": True,
        "validated": True,
        "quarantinedCount": len(gate.get("quarantined") or []),
        "quarantined": gate.get("quarantined") or [],
        "failReason": gate.get("failReason"),
    }
    out["validated"] = True
    return out


def quarantine_reject(
    quarantine_dir: Path | str,
    snapshot: dict | None,
    *,
    reason: str,
    source_name: str | None = None,
    raw_text: str | None = None,
) -> Path:
    """Write a rejected incoming snapshot to data/quarantine/. Not validated history."""
    quarantine_dir = Path(quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_reason = re.sub(r"[^a-zA-Z0-9._-]+", "_", reason or "reject")[:60]
    stem = Path(source_name or "incoming").stem
    dest = quarantine_dir / f"{stem}_{safe_reason}_{stamp}.json"
    payload = {
        "quarantined": True,
        "validated": False,
        "failReason": reason,
        "sourceName": source_name,
        "quarantinedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot": snapshot if isinstance(snapshot, dict) else None,
        "rawTextPreview": (raw_text or "")[:4000] if snapshot is None else None,
    }
    atomic_write_json(dest, payload)
    sidecar = dest.with_suffix(".reason.json")
    atomic_write_json(
        sidecar,
        {"failReason": reason, "sourceName": source_name, "quarantinedAt": payload["quarantinedAt"]},
    )
    return dest


def existing_snapshot_with_timestamp(snap_dir: Path | str, snapshot_utc: str | None) -> Path | None:
    """Return the validated snapshot that already owns this snapshot_utc, if any."""
    if not snapshot_utc:
        return None
    for path in list_validated_snapshot_paths(snap_dir):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("snapshot_utc") == snapshot_utc:
            return path
    return None


def persist_validated_snapshot(path: Path | str, gated: dict) -> Path:
    """Write a validated snapshot. Same snapshot_utc is immutable (first write wins)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not gated.get("validated") and not ((gated.get("qualityGate") or {}).get("validated")):
        gated = dict(gated)
        gated["validated"] = True
        qg = dict(gated.get("qualityGate") or {})
        qg["validated"] = True
        gated["qualityGate"] = qg
    snap_utc = gated.get("snapshot_utc")
    existing = existing_snapshot_with_timestamp(path.parent, snap_utc)
    if existing is not None:
        # Immutable on timestamp collision — do not overwrite the first snapshot.
        return existing
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = None
        if isinstance(old, dict) and old.get("snapshot_utc") and old.get("snapshot_utc") != snap_utc:
            # Same calendar day, different timestamp: keep original, write a sibling.
            stamp = re.sub(r"[^0-9A-Za-z]", "", str(snap_utc or ""))[:15] or "alt"
            alt = path.parent / f"{path.stem}_{stamp}.json"
            atomic_write_json(alt, gated)
            return alt
    atomic_write_json(path, gated)
    return path

#!/usr/bin/env python3
"""Single ingestion entrypoint.

Order (hard):
  data/incoming/ → Quality Gate → validated data/snapshots/
  quarantine rejects (never validated history)
  per-ticker LKG for extreme EPS / price / coverage
  generate_revision_events → evaluate_alerts(gated_snapshot=...)

Do not glob for the current snapshot when evaluating alerts: the gated
object is passed explicitly.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from revision_events import generate_revision_events  # noqa: E402
from snapshot_quality import (  # noqa: E402
    apply_lkg_and_quarantine,
    build_per_ticker_lkg,
    list_validated_snapshot_paths,
    load_latest_validated_snapshot,
    load_universe,
    persist_validated_snapshot,
    quarantine_reject,
    snapshot_quality_gate,
)


def resolve_root(start: Path | None = None) -> Path:
    cand = start or Path(__file__).resolve().parent
    for _ in range(6):
        if (cand / "data").exists() or (cand / "tools").exists():
            return cand
        cand = cand.parent
    return Path(__file__).resolve().parent.parent


def _incoming_dir(root: Path) -> Path:
    return root / "data" / "incoming"


def _quarantine_dir(root: Path) -> Path:
    return root / "data" / "quarantine"


def _snap_dir(root: Path) -> Path:
    return root / "data" / "snapshots"


def _prior_snapshot_for_diff(root: Path, dest: Path | None = None) -> dict | None:
    """Latest validated snapshot to diff against (not incoming, not quarantine)."""
    paths = [p for p in list_validated_snapshot_paths(_snap_dir(root)) if dest is None or p.resolve() != Path(dest).resolve()]
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def ingest_snapshot(
    snapshot: dict,
    *,
    root: Path | str | None = None,
    source_name: str | None = None,
    raw_text: str | None = None,
    run_alerts: bool = True,
    now: datetime | None = None,
) -> dict:
    """Quality-gate one collected snapshot. Returns a result dict.

    Rejects are quarantined and are NOT listed by load_latest / validated history.
    """
    root = Path(root) if root else resolve_root()
    snap_dir = _snap_dir(root)
    quarantine_dir = _quarantine_dir(root)
    snap_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    universe = load_universe(root)
    lkg = build_per_ticker_lkg(snap_dir)
    prior = _prior_snapshot_for_diff(root)

    gate = snapshot_quality_gate(snapshot, prior, lkg=lkg, universe=universe)
    result = {
        "ok": bool(gate.get("ok")),
        "publishable": bool(gate.get("publishable")),
        "validated": False,
        "quarantined": False,
        "canonicalPath": None,
        "quarantinePath": None,
        "revisionEvents": [],
        "gate": {k: v for k, v in gate.items()},
    }

    if not gate.get("ok") or not gate.get("publishable"):
        qpath = quarantine_reject(
            quarantine_dir,
            snapshot if isinstance(snapshot, dict) else None,
            reason=gate.get("failReason") or "quality_gate_failed",
            source_name=source_name,
            raw_text=raw_text,
        )
        result["quarantined"] = True
        result["quarantinePath"] = str(qpath)
        result["ok"] = False
        result["publishable"] = False
        return result

    gated = apply_lkg_and_quarantine(snapshot, gate, lkg=lkg)
    snap_utc = str(gated.get("snapshot_utc") or "")
    day = snap_utc[:10] if len(snap_utc) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = snap_dir / f"{day}.json"
    persist_validated_snapshot(dest, gated)
    result["validated"] = True
    result["canonicalPath"] = str(dest)
    result["gatedSnapshot"] = gated

    # generate_revision_events BEFORE evaluate_alerts (order is part of the contract)
    hist_path = root / "data" / "revisions" / "history.jsonl"
    appended = generate_revision_events(prior, gated, hist_path, now=now)
    result["revisionEvents"] = appended

    if run_alerts:
        import build_alerts as ba

        payload = ba.evaluate_alerts(gated_snapshot=gated, now=now)
        ba.write_alerts(payload)
        result["alerts"] = {
            "status": payload.get("alertEngineStatus"),
            "active": len(payload.get("activeAlerts") or []),
        }

    return result


def ingest_incoming_file(
    path: Path | str,
    *,
    root: Path | str | None = None,
    run_alerts: bool = True,
    now: datetime | None = None,
) -> dict:
    path = Path(path)
    root = Path(root) if root else resolve_root()
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        snapshot = json.loads(raw)
    except Exception:
        qpath = quarantine_reject(
            _quarantine_dir(root),
            None,
            reason="invalid_json",
            source_name=path.name,
            raw_text=raw,
        )
        return {
            "ok": False,
            "publishable": False,
            "validated": False,
            "quarantined": True,
            "quarantinePath": str(qpath),
            "canonicalPath": None,
            "revisionEvents": [],
            "gate": {"failReason": "invalid_json", "ok": False},
        }
    result = ingest_snapshot(
        snapshot,
        root=root,
        source_name=path.name,
        raw_text=raw,
        run_alerts=run_alerts,
        now=now,
    )
    # Incoming file is consumed: move rejects already copied; remove original from incoming.
    try:
        if path.exists() and _incoming_dir(root) in path.resolve().parents:
            processed = _incoming_dir(root) / "processed"
            if result.get("quarantined"):
                path.unlink(missing_ok=True)
            else:
                processed.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(processed / path.name))
    except Exception:
        pass
    return result


def ingest_incoming_dir(root: Path | str | None = None, **kwargs) -> list[dict]:
    root = Path(root) if root else resolve_root()
    incoming = _incoming_dir(root)
    incoming.mkdir(parents=True, exist_ok=True)
    results = []
    for p in sorted(incoming.glob("*.json")):
        if p.name.startswith("."):
            continue
        results.append(ingest_incoming_file(p, root=root, **kwargs))
    return results


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = resolve_root()
    if args:
        path = Path(args[0])
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        result = ingest_incoming_file(path, root=root)
        print(json.dumps({k: v for k, v in result.items() if k != "gatedSnapshot"}, indent=2, default=str))
        return 0 if result.get("ok") else 2
    results = ingest_incoming_dir(root)
    print(json.dumps([{k: v for k, v in r.items() if k != "gatedSnapshot"} for r in results], indent=2, default=str))
    if any(not r.get("ok") for r in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Pipeline Integrity orchestrator.

Order (hard):
  Collection → Quality Gate → publishable → persist → Alert Engine → Export → Publish

A rejected snapshot MUST NOT:
  - persist canonical snapshot
  - append daily EPS observations
  - mutate the Alert DB (data/alerts/index.json)
  - advance comparisonCheckpoint
  - publish

Pre-gate `build_alerts` is forbidden. Publish scripts call this module (or
export after a publishable persist), never `build_alerts.py` first.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from atomic_io import (  # noqa: E402
    PipelineLockedError,
    acquire_global_pipeline_lock,
)
from snapshot_quality import (  # noqa: E402
    MixedBuildError,
    SnapshotQualityError,
    check_build_generation_consistency,
    load_prior_snapshot,
    persist_raw_snapshot,
    persist_snapshot,
    snapshot_quality_gate,
)

ROOT = _here.parent
if not (ROOT / "data" / "snapshots").exists():
    cand = Path(__file__).resolve().parent
    for _ in range(5):
        if (cand / "data" / "snapshots").exists():
            ROOT = cand
            break
        cand = cand.parent

SNAP_DIR = ROOT / "data" / "snapshots"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
WEB_DATA = ROOT / "web" / "data"


class PipelineRejected(RuntimeError):
    """Quality gate rejected the snapshot — persist/alerts/export skipped."""


def _alert_db_fingerprint(path: Path | None = None) -> bytes | None:
    path = path or ALERTS_PATH
    if not path.exists():
        return None
    return path.read_bytes()


def ingest_collected_snapshot(
    snapshot: dict,
    dest: Path | str | None = None,
    *,
    prior: dict | None = None,
    raw_when: datetime | None = None,
    root: Path | str | None = None,
    run_export: bool = False,
    acquire_lock: bool = True,
) -> dict:
    """Quality-gate a collected snapshot, persist only if publishable.

    Raw timestamped snapshot is always preserved (same-day files kept).
    Canonical persist + Alert Engine + export run ONLY when publishable.
    """
    root = Path(root) if root else ROOT
    snap_dir = root / "data" / "snapshots"
    alerts_path = root / "data" / "alerts" / "index.json"
    web_data = root / "web" / "data"

    lock = None
    if acquire_lock:
        lock = acquire_global_pipeline_lock(root)

    before_alerts = _alert_db_fingerprint(alerts_path)
    result: dict = {
        "publishable": False,
        "persisted": False,
        "alertsMutated": False,
        "exported": False,
        "rawPath": None,
        "canonicalPath": None,
        "gate": None,
    }
    try:
        # 1. Collection artifact: timestamped raw (never overwrites same-day peers)
        result["rawPath"] = str(persist_raw_snapshot(snap_dir, snapshot, when=raw_when))

        # Mixed public payload must not be published alongside a new ingest.
        try:
            check_build_generation_consistency(web_data)
        except MixedBuildError as exc:
            result["gate"] = {
                "ok": False,
                "publishable": False,
                "failReason": "mixed_build_generation",
                "status": "reject",
                "message": str(exc),
            }
            result["publishable"] = False
            after = _alert_db_fingerprint(alerts_path)
            result["alertsMutated"] = after != before_alerts
            return result

        if prior is None:
            prior = load_prior_snapshot(snap_dir, exclude=dest)

        gate = snapshot_quality_gate(snapshot, prior)
        result["gate"] = {k: v for k, v in gate.items() if k != "written"}
        if not gate.get("ok") or not gate.get("publishable"):
            result["publishable"] = False
            after = _alert_db_fingerprint(alerts_path)
            result["alertsMutated"] = after != before_alerts
            return result

        # 2. Persist canonical (quarantine applied)
        if dest is None:
            snap_utc = str(snapshot.get("snapshot_utc") or "")
            day = snap_utc[:10] if len(snap_utc) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            dest = snap_dir / f"{day}.json"
        dest = Path(dest)
        persisted = persist_snapshot(dest, snapshot, prior)
        result["publishable"] = True
        result["persisted"] = True
        result["canonicalPath"] = str(dest)
        result["gate"] = {k: v for k, v in persisted.items() if k != "written"}

        if run_export:
            import export_web_data as exp

            # Alert Engine + Export after persist. Nested lock is skipped.
            exp.main(acquire_lock=False)
            result["exported"] = True

        after = _alert_db_fingerprint(alerts_path)
        result["alertsMutated"] = after != before_alerts
        return result
    finally:
        if lock is not None:
            lock.release()


def run_pipeline(*, root: Path | str | None = None) -> dict:
    """Full post-collection pipeline for the latest canonical snapshot.

    Assumes collection already wrote a candidate; quality-gates it, then
    persist (no-op if already gated) → Alert Engine → Export.
    """
    root = Path(root) if root else ROOT
    lock = acquire_global_pipeline_lock(root)
    try:
        check_build_generation_consistency(root / "web" / "data")
        import export_web_data as exp

        # Export owns: daily persist → Alert Engine → web JSON → checkpoint advance
        exp.main(acquire_lock=False)
        return {"ok": True, "publishable": True}
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        run_pipeline()
        print("pipeline: publishable — export complete")
        return 0
    except PipelineLockedError as exc:
        print(f"PIPELINE LOCKED: {exc}", file=sys.stderr)
        return 3
    except MixedBuildError as exc:
        print(f"MIXED BUILD REJECTED: {exc}", file=sys.stderr)
        return 2
    except SnapshotQualityError as exc:
        print(f"QUALITY GATE REJECT: {exc}", file=sys.stderr)
        return 2
    except PipelineRejected as exc:
        print(f"PIPELINE REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

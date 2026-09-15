#!/usr/bin/env python3
"""Single ingestion entrypoint — transactional staged/commit.

Order (hard):
  data/incoming/ → Quality Gate → STAGE gated snapshot + revision events
  generate_revision_events → evaluate_alerts(gated_snapshot=...) → Export
  COMMIT validated snapshot + history.jsonl only if export succeeds.

Export failure aborts: no LKG (validated snapshot) commit, no revision commit.
Revision generation failure blocks export.
Rejects go to data/quarantine/ and never enter validated history.
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

from revision_events import (  # noqa: E402
    generate_revision_events,
    load_history_rows,
    persist_revision_events,
)
from snapshot_quality import (  # noqa: E402
    apply_lkg_and_quarantine,
    build_per_ticker_lkg,
    existing_snapshot_with_timestamp,
    list_validated_snapshot_paths,
    load_universe,
    persist_validated_snapshot,
    quarantine_reject,
    snapshot_quality_gate,
)

# Tests may assign a stub here (e.g. to simulate export failure) without relying
# on import identity of export_web_data.main.
export_main = None


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


def _staging_dir(root: Path) -> Path:
    return root / "data" / "staging"


def _hist_path(root: Path) -> Path:
    return root / "data" / "revisions" / "history.jsonl"


def _prior_snapshot_for_diff(root: Path, dest: Path | None = None) -> dict | None:
    """Latest validated snapshot to diff against (not incoming, not quarantine, not staging)."""
    paths = [
        p
        for p in list_validated_snapshot_paths(_snap_dir(root))
        if dest is None or p.resolve() != Path(dest).resolve()
    ]
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_staging(root: Path) -> None:
    staging = _staging_dir(root)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def _write_staging(root: Path, gated: dict, pending_events: list[dict]) -> Path:
    staging = _staging_dir(root)
    staging.mkdir(parents=True, exist_ok=True)
    snap_path = staging / "snapshot.json"
    snap_path.write_text(json.dumps(gated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    events_path = staging / "revisions.jsonl"
    events_path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in pending_events),
        encoding="utf-8",
    )
    return snap_path


def _backup_bytes(path: Path) -> tuple[Path, bytes | None]:
    return path, path.read_bytes() if path.exists() else None


def _restore_bytes(path: Path, data: bytes | None) -> None:
    if data is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _run_export(root: Path, snapshot: dict, extra_history: list[dict] | None = None) -> None:
    import build_alerts as ba
    import export_web_data as exp

    if hasattr(exp, "configure_root"):
        exp.configure_root(root)
    if hasattr(ba, "configure_root"):
        ba.configure_root(root)
    # Prefer an explicit stub (tests), else the live module's main.
    main = export_main if export_main is not None else exp.main
    main(
        snapshot=snapshot,
        extra_history=extra_history or [],
        persist_revisions=False,
    )


def ingest_snapshot(
    snapshot: dict,
    *,
    root: Path | str | None = None,
    source_name: str | None = None,
    raw_text: str | None = None,
    run_alerts: bool = True,
    run_export: bool = True,
    publish: bool = False,
    now: datetime | None = None,
) -> dict:
    """Quality-gate one collected snapshot. Commit only after successful export.

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
    hist_path = _hist_path(root)

    gate = snapshot_quality_gate(snapshot, prior, lkg=lkg, universe=universe)
    result = {
        "ok": bool(gate.get("ok")),
        "publishable": bool(gate.get("publishable")),
        "validated": False,
        "committed": False,
        "quarantined": False,
        "canonicalPath": None,
        "quarantinePath": None,
        "revisionEvents": [],
        "exported": False,
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
    result["gatedSnapshot"] = gated
    snap_utc = str(gated.get("snapshot_utc") or "")
    day = snap_utc[:10] if len(snap_utc) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = snap_dir / f"{day}.json"

    collision = existing_snapshot_with_timestamp(snap_dir, snap_utc)
    if collision is not None:
        result["ok"] = True
        result["validated"] = True
        result["committed"] = False
        result["collisionPreserved"] = True
        result["canonicalPath"] = str(collision)
        result["revisionEvents"] = []
        return result

    # generate_revision_events BEFORE evaluate_alerts (order is part of the contract).
    # persist=False — commit only after export succeeds (single owner of history.jsonl).
    try:
        pending = generate_revision_events(prior, gated, hist_path, now=now, persist=False)
    except Exception as exc:
        result["ok"] = False
        result["publishable"] = False
        result["validated"] = False
        result["committed"] = False
        result["revisionError"] = f"{type(exc).__name__}: {exc}"
        result["exportBlocked"] = True
        return result

    result["revisionEvents"] = pending
    _write_staging(root, gated, pending)

    overlay_history = load_history_rows(hist_path) + list(pending)

    if run_alerts:
        import build_alerts as ba

        if hasattr(ba, "configure_root"):
            ba.configure_root(root)
        # evaluate_alerts(gated_snapshot=...) — no glob for current
        payload = ba.evaluate_alerts(
            gated_snapshot=gated,
            now=now,
            history=overlay_history,
        )
        if not run_export:
            ba.write_alerts(payload)
        result["alerts"] = {
            "status": payload.get("alertEngineStatus"),
            "active": len(payload.get("activeAlerts") or []),
        }

    if run_export:
        daily_path = root / "data" / "daily_eps_snapshots" / "daily.jsonl"
        alerts_path = root / "data" / "alerts" / "index.json"
        backups = [_backup_bytes(daily_path), _backup_bytes(alerts_path)]
        try:
            _run_export(root, gated, extra_history=pending)
            result["exported"] = True
        except Exception as exc:
            for path, data in backups:
                _restore_bytes(path, data)
            _clear_staging(root)
            result["ok"] = False
            result["publishable"] = False
            result["validated"] = False
            result["committed"] = False
            result["exported"] = False
            result["exportError"] = f"{type(exc).__name__}: {exc}"
            return result

    # COMMIT: validated snapshot + revision events (LKG sees this snapshot only now)
    committed_path = persist_validated_snapshot(dest, gated)
    persisted_events = persist_revision_events(pending, hist_path)
    result["revisionEvents"] = persisted_events if persisted_events else pending
    result["validated"] = True
    result["committed"] = True
    result["canonicalPath"] = str(committed_path)
    result["ok"] = True
    _clear_staging(root)

    if publish:
        from publish_prebuilt import publish_prebuilt_site

        pub = publish_prebuilt_site(root)
        result["publish"] = {k: pub.get(k) for k in ("ok", "pushed", "noop", "error") if k in pub}

    return result


def ingest_and_publish(snapshot: dict, **kwargs) -> dict:
    """ingest → single export → publish_prebuilt_site (no second export)."""
    kwargs = dict(kwargs)
    kwargs["run_export"] = True
    kwargs["publish"] = True
    return ingest_snapshot(snapshot, **kwargs)


def ingest_incoming_file(
    path: Path | str,
    *,
    root: Path | str | None = None,
    run_alerts: bool = True,
    run_export: bool = True,
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
            "committed": False,
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
        run_export=run_export,
        now=now,
    )
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
    publish = "--publish" in args
    args = [a for a in args if a != "--publish"]
    if args:
        path = Path(args[0])
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        result = ingest_incoming_file(path, root=root)
        if publish and result.get("ok"):
            from publish_prebuilt import publish_prebuilt_site

            result["publish"] = publish_prebuilt_site(root)
        print(json.dumps({k: v for k, v in result.items() if k != "gatedSnapshot"}, indent=2, default=str))
        return 0 if result.get("ok") else 2
    results = ingest_incoming_dir(root)
    if publish:
        from publish_prebuilt import publish_prebuilt_site

        if any(r.get("ok") for r in results):
            publish_prebuilt_site(root)
    print(json.dumps([{k: v for k, v in r.items() if k != "gatedSnapshot"} for r in results], indent=2, default=str))
    if any(not r.get("ok") for r in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

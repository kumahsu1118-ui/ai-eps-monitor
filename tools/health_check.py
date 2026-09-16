#!/usr/bin/env python3
"""Read-only pipeline & canonical history health check.

Never mutates production state. Never materializes runtime daily.jsonl,
never ingests, never fetches Seeking Alpha, never persists canonical git,
never publishes Pages, never takes the pipeline lock.

Checks
------
1. Canonical history validity (Git-tracked ``data/history/eps_daily/``)
2. Runtime materialization health (compare cache to canonical in memory)
3. CURRENT presence/integrity (as applicable on checkout)
4. Canonical git sync state (state file + read-only porcelain)
5. Recent collection status
6. Quarantine status
7. Migration audit (in-process ``plan_migration``; no --apply)
8. Per-ticker Internal 30/60/90D history readiness

Output
------
Human-readable summary and machine-readable JSON.
Final status is exactly one of: HEALTHY / DEGRADED / FAILED.

Exit codes
----------
  0  HEALTHY
  1  DEGRADED
  2  FAILED

``--allow-degraded`` maps HEALTHY and DEGRADED to 0, FAILED to 1
(CI isolation gate: fail the job only when the pipeline is FAILED).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import canonical_eps_history as ceh  # noqa: E402
import migrate_eps_history as meh  # noqa: E402
from revision_windows import (  # noqa: E402
    INTERNAL_REVISION_WINDOWS,
    compute_internal_window,
    group_daily_by_fiscal_identity,
    parse_obs_datetime,
)

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAILED = "FAILED"
VALID_STATUSES = (STATUS_HEALTHY, STATUS_DEGRADED, STATUS_FAILED)
STATUS_RANK = {STATUS_HEALTHY: 0, STATUS_DEGRADED: 1, STATUS_FAILED: 2}

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2

CURRENT_REL = Path("data") / "CURRENT.json"
GIT_STATE_REL = Path("data") / "canonical_git_state.json"
QUARANTINE_REL = Path("data") / "snapshots" / "quarantine"
META_RELS = (Path("data") / "meta.json", Path("web") / "data" / "meta.json")
WATCHLIST_RELS = (
    Path("data") / "universe.json",
    Path("data") / "watchlist.json",
    Path("web") / "data" / "watchlist.json",
)
DEFAULT_TICKERS = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]
CANONICAL_GIT_OK = frozenset({"ok", "clean", "skipped"})
CANONICAL_GIT_DEGRADED = frozenset({"pending"})
CANONICAL_GIT_FAILED = frozenset({"failed"})
FINGERPRINT_SKIP_NAMES = frozenset({".pipeline.lock", ".locks"})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def fingerprint_production(root: Path) -> dict[str, str]:
    """Content fingerprints used to prove this tool is read-only.

    Broader than migrate.fingerprint_workspace: includes meta, quarantine,
    git state, and lockfiles so a stray write cannot hide.
    """
    root = Path(root)
    out: dict[str, str] = dict(meh.fingerprint_workspace(root))
    extra = [
        CURRENT_REL,
        GIT_STATE_REL,
        Path("data") / ".materialized_run_id",
        Path("data") / ".pipeline.lock",
        *META_RELS,
        Path("data") / "watchlist.json",
        Path("web") / "data" / "watchlist.json",
    ]
    for rel in extra:
        p = root / rel
        if p.is_file():
            out[str(rel)] = _sha256_file(p)
    qdir = root / QUARANTINE_REL
    if qdir.is_dir():
        for p in sorted(qdir.rglob("*")):
            if p.is_file():
                out[f"quarantine:{p.relative_to(qdir)}"] = _sha256_file(p)
    hist = ceh.history_dir(root)
    if hist.is_dir():
        for p in sorted(hist.rglob("*")):
            if p.is_file():
                out[f"history-all:{p.relative_to(hist)}"] = _sha256_file(p)
    # Detect newly created files under data/ (except known runtime lock dirs).
    data = root / "data"
    if data.is_dir():
        for p in sorted(data.rglob("*")):
            if not p.is_file():
                continue
            if p.name in FINGERPRINT_SKIP_NAMES or ".locks" in p.parts:
                continue
            rel = p.relative_to(root)
            key = f"data-file:{rel}"
            if key not in out:
                out[key] = _sha256_file(p)
    return out


def _check(
    name: str,
    status: str,
    summary: str,
    details: dict | None = None,
) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid check status {status!r}")
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": details or {},
    }


def _worse(a: str, b: str) -> str:
    return a if STATUS_RANK[a] >= STATUS_RANK[b] else b


def load_watchlist_tickers(root: Path) -> list[str]:
    for rel in WATCHLIST_RELS:
        path = Path(root) / rel
        if not path.is_file():
            continue
        try:
            obj = _read_json(path)
        except Exception:
            continue
        if isinstance(obj, dict):
            tickers = obj.get("tickers")
        elif isinstance(obj, list):
            tickers = obj
        else:
            continue
        out = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
        if out:
            return out
    return list(DEFAULT_TICKERS)


def _load_meta(root: Path) -> tuple[dict | None, str | None]:
    for rel in META_RELS:
        path = Path(root) / rel
        if not path.is_file():
            continue
        try:
            obj = _read_json(path)
        except Exception as exc:
            return None, f"{rel} unreadable: {exc}"
        if isinstance(obj, dict):
            return obj, str(rel)
    return None, None


def check_canonical_history(root: Path) -> dict:
    hist = ceh.history_dir(root)
    files = ceh.list_month_files(root)
    details: dict[str, Any] = {
        "historyDir": str(hist),
        "monthFiles": [p.name for p in files],
        "rowCount": 0,
        "identityConflicts": [],
        "duplicateIdentities": 0,
        "monthMismatches": [],
        "errors": [],
    }
    if not files:
        return _check(
            "canonical_history",
            STATUS_FAILED,
            "no Git-tracked canonical month files under data/history/eps_daily/",
            details,
        )
    rows: list[dict] = []
    try:
        for path in files:
            month_key = path.name[:7]
            month_rows = ceh.load_jsonl_rows(path, strict=True)
            for obs in month_rows:
                mk = ceh.month_key_from_date(obs.get("date") or "")
                if mk != month_key:
                    details["monthMismatches"].append(
                        {"file": path.name, "date": obs.get("date"), "expectedMonth": month_key}
                    )
            rows.extend(month_rows)
    except ceh.CanonicalHistoryError as exc:
        details["errors"].append(str(exc))
        return _check(
            "canonical_history",
            STATUS_FAILED,
            f"canonical history failed strict parse: {exc}",
            details,
        )
    except Exception as exc:
        details["errors"].append(f"{type(exc).__name__}: {exc}")
        return _check(
            "canonical_history",
            STATUS_FAILED,
            f"canonical history unreadable: {exc}",
            details,
        )

    details["rowCount"] = len(rows)
    by_ident: dict[tuple, list[str]] = defaultdict(list)
    for obs in rows:
        try:
            ident = ceh.identity_key(obs)
        except ceh.CanonicalHistoryError as exc:
            details["errors"].append(str(exc))
            return _check(
                "canonical_history",
                STATUS_FAILED,
                f"canonical row missing identity: {exc}",
                details,
            )
        by_ident[ident].append(ceh.canonical_payload_fingerprint(obs))

    conflicts = []
    dupes = 0
    for ident, fps in by_ident.items():
        uniq = set(fps)
        if len(uniq) > 1:
            conflicts.append(
                {
                    "ticker": ident[0],
                    "reportedFiscalPeriodEnding": ident[1],
                    "date": ident[2],
                    "updateTime": ident[3],
                    "payloads": sorted(uniq),
                }
            )
        elif len(fps) > 1:
            dupes += 1
    details["identityConflicts"] = conflicts
    details["duplicateIdentities"] = dupes

    if conflicts:
        return _check(
            "canonical_history",
            STATUS_FAILED,
            f"{len(conflicts)} canonical identity conflict(s) (same identity, different payload)",
            details,
        )
    if details["monthMismatches"]:
        return _check(
            "canonical_history",
            STATUS_FAILED,
            f"{len(details['monthMismatches'])} row(s) stored in the wrong month file",
            details,
        )
    if len(rows) == 0:
        return _check(
            "canonical_history",
            STATUS_FAILED,
            "canonical month files exist but contain 0 admitted observations",
            details,
        )
    if dupes:
        return _check(
            "canonical_history",
            STATUS_DEGRADED,
            f"{len(rows)} observations in {len(files)} month file(s); {dupes} exact duplicate identit(y/ies)",
            details,
        )
    return _check(
        "canonical_history",
        STATUS_HEALTHY,
        f"{len(rows)} observations in {len(files)} month file(s); strict parse ok",
        details,
    )


def check_runtime_materialization(root: Path) -> dict:
    dest = ceh.runtime_daily_path(root)
    details: dict[str, Any] = {
        "runtimePath": str(dest),
        "present": dest.is_file(),
        "runtimeRows": 0,
        "canonicalRows": 0,
        "matchesCanonical": False,
    }
    try:
        canonical_rows = ceh.load_all_canonical_rows(root, strict=True)
    except ceh.CanonicalHistoryError as exc:
        return _check(
            "runtime_materialization",
            STATUS_FAILED,
            f"cannot evaluate runtime cache; canonical strict parse failed: {exc}",
            details,
        )
    expected = ceh.format_jsonl(canonical_rows)
    details["canonicalRows"] = len(canonical_rows)
    if not dest.is_file():
        return _check(
            "runtime_materialization",
            STATUS_DEGRADED,
            "runtime daily.jsonl absent (rebuildable from canonical; expected on fresh clone)",
            details,
        )
    try:
        actual_rows = ceh.load_jsonl_rows(dest, strict=True)
    except ceh.CanonicalHistoryError as exc:
        return _check(
            "runtime_materialization",
            STATUS_FAILED,
            f"runtime daily.jsonl failed strict parse: {exc}",
            details,
        )
    except Exception as exc:
        return _check(
            "runtime_materialization",
            STATUS_FAILED,
            f"runtime daily.jsonl unreadable: {exc}",
            details,
        )
    actual_sorted = ceh.sort_canonical_rows(actual_rows)
    actual_text = ceh.format_jsonl(actual_sorted)
    details["runtimeRows"] = len(actual_rows)
    details["matchesCanonical"] = actual_text == expected
    if not details["matchesCanonical"]:
        return _check(
            "runtime_materialization",
            STATUS_FAILED,
            "runtime daily.jsonl does not match Git-tracked canonical history",
            details,
        )
    return _check(
        "runtime_materialization",
        STATUS_HEALTHY,
        f"runtime daily.jsonl matches canonical ({len(actual_rows)} rows)",
        details,
    )


def check_current_pointer(root: Path) -> dict:
    path = Path(root) / CURRENT_REL
    details: dict[str, Any] = {
        "path": str(path),
        "present": path.is_file(),
        "runId": None,
        "generationExists": False,
        "materializationStatus": None,
        "applicableOnCheckout": False,
    }
    if not path.is_file():
        details["applicableOnCheckout"] = True
        return _check(
            "current_pointer",
            STATUS_DEGRADED,
            "CURRENT.json absent (expected on fresh clone / CI checkout; not Git-tracked)",
            details,
        )
    try:
        obj = _read_json(path)
    except Exception as exc:
        return _check(
            "current_pointer",
            STATUS_FAILED,
            f"CURRENT.json unreadable: {exc}",
            details,
        )
    if not isinstance(obj, dict):
        return _check(
            "current_pointer",
            STATUS_FAILED,
            "CURRENT.json is not a JSON object",
            details,
        )
    run_id = obj.get("runId") or obj.get("run_id")
    details["runId"] = str(run_id) if run_id else None
    details["keys"] = sorted(obj.keys())
    if not run_id:
        return _check(
            "current_pointer",
            STATUS_FAILED,
            "CURRENT.json missing runId",
            details,
        )
    gen = Path(str(obj.get("generation") or ""))
    gen_dir = gen if gen.is_dir() else (Path(root) / "data" / "generations" / str(run_id))
    details["generationPath"] = str(gen_dir)
    details["generationExists"] = gen_dir.is_dir()
    if not gen_dir.is_dir():
        return _check(
            "current_pointer",
            STATUS_FAILED,
            f"CURRENT runId={run_id} points at a missing generation directory",
            details,
        )
    meta_path = gen_dir / "generation_meta.json"
    mat = None
    if meta_path.is_file():
        try:
            meta = _read_json(meta_path)
            if isinstance(meta, dict):
                mat = meta.get("materializationStatus")
                details["runStatus"] = meta.get("runStatus") or meta.get("status")
                details["abortReason"] = meta.get("abortReason") or meta.get("abort_reason")
        except Exception as exc:
            return _check(
                "current_pointer",
                STATUS_FAILED,
                f"generation_meta.json unreadable: {exc}",
                details,
            )
    details["materializationStatus"] = mat
    if details.get("abortReason") or str(details.get("runStatus") or "").lower() in {"aborted", "abort"}:
        return _check(
            "current_pointer",
            STATUS_FAILED,
            f"CURRENT generation is aborted (runId={run_id})",
            details,
        )
    mat_l = str(mat or "").strip().lower()
    if mat_l in {"failed", "pending"}:
        return _check(
            "current_pointer",
            STATUS_DEGRADED,
            f"CURRENT present (runId={run_id}) but materializationStatus={mat}",
            details,
        )
    return _check(
        "current_pointer",
        STATUS_HEALTHY,
        f"CURRENT present runId={run_id} generation exists materializationStatus={mat or 'n/a'}",
        details,
    )


def _git_porcelain_history(root: Path) -> tuple[list[str], str | None]:
    git_dir = Path(root) / ".git"
    if not git_dir.exists():
        return [], "not_a_git_repo"
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "data/history/eps_daily"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return [], f"git_unavailable:{exc}"
    if proc.returncode != 0:
        return [], f"git_status_rc={proc.returncode}"
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return lines, None


def check_canonical_git_sync(root: Path) -> dict:
    path = Path(root) / GIT_STATE_REL
    skip_persist = os.environ.get("SKIP_CANONICAL_GIT_PERSIST") == "1"
    skip_push = os.environ.get("SKIP_CANONICAL_GIT_PUSH") == "1"
    porcelain, git_err = _git_porcelain_history(root)
    details: dict[str, Any] = {
        "path": str(path),
        "present": path.is_file(),
        "skipCanonicalGitPersist": skip_persist,
        "skipCanonicalGitPush": skip_push,
        "stateStatus": None,
        "porcelain": porcelain,
        "gitError": git_err,
    }
    state: dict = {}
    if path.is_file():
        try:
            loaded = _read_json(path)
            state = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            return _check(
                "canonical_git_sync",
                STATUS_FAILED,
                f"canonical_git_state.json unreadable: {exc}",
                details,
            )
    details["stateStatus"] = state.get("status")
    details["lastAttempt"] = state.get("lastAttempt")
    details["lastSuccessful"] = state.get("lastSuccessful")
    details["error"] = state.get("error")
    details["ahead"] = state.get("ahead")

    st = str(state.get("status") or "").strip().lower()
    if st in CANONICAL_GIT_FAILED:
        return _check(
            "canonical_git_sync",
            STATUS_FAILED,
            f"canonical git persist failed: {state.get('error') or st}",
            details,
        )
    if st in CANONICAL_GIT_DEGRADED:
        return _check(
            "canonical_git_sync",
            STATUS_DEGRADED,
            f"canonical git persist pending (retry without rolling back CURRENT): {state.get('error') or st}",
            details,
        )
    if porcelain:
        return _check(
            "canonical_git_sync",
            STATUS_DEGRADED,
            f"uncommitted canonical history paths ({len(porcelain)})",
            details,
        )
    if path.is_file() and st in CANONICAL_GIT_OK:
        return _check(
            "canonical_git_sync",
            STATUS_HEALTHY,
            f"canonical git state={st}",
            details,
        )
    if skip_persist and not path.is_file():
        return _check(
            "canonical_git_sync",
            STATUS_HEALTHY,
            "SKIP_CANONICAL_GIT_PERSIST=1 and no state file (CI / isolated run)",
            details,
        )
    if not path.is_file():
        return _check(
            "canonical_git_sync",
            STATUS_DEGRADED,
            "canonical_git_state.json absent (unknown sync state)",
            details,
        )
    return _check(
        "canonical_git_sync",
        STATUS_DEGRADED,
        f"canonical git state unrecognized ({st or 'empty'})",
        details,
    )


def check_recent_collection(root: Path) -> dict:
    meta, source = _load_meta(root)
    details: dict[str, Any] = {"source": source, "present": meta is not None}
    if meta is None:
        if source:
            return _check(
                "recent_collection",
                STATUS_FAILED,
                source,
                details,
            )
        return _check(
            "recent_collection",
            STATUS_DEGRADED,
            "no data/meta.json or web/data/meta.json (collection status unknown)",
            details,
        )
    status = str(meta.get("collectionStatus") or "").strip().lower()
    label = meta.get("collectionStatusLabel")
    failed = list(meta.get("failedTickers") or [])
    successful = list(meta.get("successfulTickers") or [])
    last = meta.get("lastSuccessfulCollection") or meta.get("consensusDataAsOf")
    stale = bool(meta.get("dataStale"))
    details.update(
        {
            "collectionStatus": status or None,
            "collectionStatusLabel": label,
            "failedTickers": failed,
            "successfulTickers": successful,
            "lastSuccessfulCollection": last,
            "dataStale": stale,
            "qualityGate": (meta.get("qualityGate") or {}).get("status")
            if isinstance(meta.get("qualityGate"), dict)
            else None,
        }
    )
    if status in {"failed", "fail"}:
        return _check(
            "recent_collection",
            STATUS_FAILED,
            f"collectionStatus={status or label} failedTickers={failed}",
            details,
        )
    if status in {"partial", "incomplete"} or failed or stale:
        reason = []
        if status in {"partial", "incomplete"}:
            reason.append(f"status={status}")
        if failed:
            reason.append(f"failedTickers={failed}")
        if stale:
            reason.append("dataStale=true")
        return _check(
            "recent_collection",
            STATUS_DEGRADED,
            "recent collection degraded: " + ", ".join(reason),
            details,
        )
    if status in {"complete", "ok", "success"} or (not status and last and not stale):
        return _check(
            "recent_collection",
            STATUS_HEALTHY,
            f"collection {label or status or 'ok'} lastSuccessfulCollection={last}",
            details,
        )
    return _check(
        "recent_collection",
        STATUS_DEGRADED,
        f"collection status unclear ({label or status or 'missing'})",
        details,
    )


def check_quarantine(root: Path) -> dict:
    qdir = Path(root) / QUARANTINE_REL
    files: list[str] = []
    if qdir.is_dir():
        files = sorted(str(p.relative_to(qdir)) for p in qdir.rglob("*") if p.is_file())
    details = {
        "path": str(qdir),
        "present": qdir.is_dir(),
        "count": len(files),
        "files": files[:50],
    }
    if not files:
        return _check(
            "quarantine",
            STATUS_HEALTHY,
            "no quarantined snapshots",
            details,
        )
    return _check(
        "quarantine",
        STATUS_DEGRADED,
        f"{len(files)} quarantined snapshot file(s) under data/snapshots/quarantine/",
        details,
    )


def check_migration_audit(root: Path) -> dict:
    """In-process read-only migration plan. Does not acquire the pipeline lock."""
    try:
        plan = meh.plan_migration(root)
        public = meh.public_plan(plan)
    except ceh.CanonicalHistoryError as exc:
        return _check(
            "migration_audit",
            STATUS_FAILED,
            f"migration audit blocked by canonical error: {exc}",
            {"error": str(exc)},
        )
    except Exception as exc:
        return _check(
            "migration_audit",
            STATUS_FAILED,
            f"migration audit failed: {exc}",
            {"error": f"{type(exc).__name__}: {exc}"},
        )
    counts = public.get("counts") or {}
    conflicts = int(counts.get("conflicts") or 0)
    blocking = int(counts.get("blockingValidation") or 0)
    new_n = int(counts.get("new") or 0)
    ok = public.get("ok") is True
    details = {
        "ok": ok,
        "counts": {
            k: counts[k]
            for k in (
                "conflicts",
                "blockingValidation",
                "new",
                "alreadyCanonical",
                "proposedRows",
                "validCandidates",
                "rejected",
            )
            if k in counts
        },
        "payloadMonths": public.get("payloadMonths") or [],
        "conflictCount": conflicts,
        "blockingValidation": public.get("blockingValidation") or [],
    }
    if not ok or conflicts or blocking:
        return _check(
            "migration_audit",
            STATUS_FAILED,
            f"migration audit not ok (ok={ok} conflicts={conflicts} blockingValidation={blocking})",
            details,
        )
    if new_n > 0:
        return _check(
            "migration_audit",
            STATUS_DEGRADED,
            f"migration audit ok with {new_n} recoverable observation(s) not yet in canonical",
            details,
        )
    return _check(
        "migration_audit",
        STATUS_HEALTHY,
        "migration audit ok (conflicts=0 blockingValidation=0 new=0)",
        details,
    )


def _as_of_from_rows(rows: list[dict]) -> datetime | None:
    best: datetime | None = None
    for row in rows:
        dt = parse_obs_datetime(row.get("updateTime") or row.get("date"))
        if dt is None:
            continue
        if best is None or dt > best:
            best = dt
    return best


def check_history_readiness(root: Path, *, canonical_ok: bool) -> dict:
    tickers = load_watchlist_tickers(root)
    details: dict[str, Any] = {
        "tickers": tickers,
        "windows": list(INTERNAL_REVISION_WINDOWS),
        "perTicker": {},
        "asOf": None,
    }
    if not canonical_ok:
        return _check(
            "history_readiness",
            STATUS_FAILED,
            "cannot evaluate 30/60/90D readiness; canonical history is not valid",
            details,
        )
    try:
        rows = ceh.load_all_canonical_rows(root, strict=True)
    except ceh.CanonicalHistoryError as exc:
        return _check(
            "history_readiness",
            STATUS_FAILED,
            f"canonical load failed: {exc}",
            details,
        )
    as_of = _as_of_from_rows(rows)
    if as_of is None:
        as_of = _now_utc()
    details["asOf"] = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    groups = group_daily_by_fiscal_identity(rows)
    all_ready = True
    any_obs = False
    for ticker in tickers:
        ident_groups = {k: pts for k, pts in groups.items() if k[0] == ticker}
        window_ready = {int(n): False for n in INTERNAL_REVISION_WINDOWS}
        identities: list[dict] = []
        for key, pts in sorted(ident_groups.items()):
            any_obs = True
            entry = {"reportedFiscalPeriodEnding": key[1], "observationCount": len(pts), "windows": {}}
            for n in INTERNAL_REVISION_WINDOWS:
                w = compute_internal_window(pts, as_of=as_of, window_days=int(n))
                ok = w.get("status") == "ok"
                entry["windows"][str(n)] = {
                    "status": w.get("status"),
                    "reason": w.get("reason"),
                    "startDate": w.get("startDate"),
                    "endDate": w.get("endDate"),
                }
                if ok:
                    window_ready[int(n)] = True
            identities.append(entry)
        ticker_ready = {str(n): bool(window_ready[int(n)]) for n in INTERNAL_REVISION_WINDOWS}
        if not ident_groups or not all(window_ready.values()):
            all_ready = False
        details["perTicker"][ticker] = {
            "identities": identities,
            "observationCount": sum(len(pts) for pts in ident_groups.values()),
            "ready": ticker_ready,
        }
    if not tickers:
        return _check(
            "history_readiness",
            STATUS_DEGRADED,
            "no watchlist tickers found",
            details,
        )
    if all_ready and any_obs:
        return _check(
            "history_readiness",
            STATUS_HEALTHY,
            f"all {len(tickers)} ticker(s) have Internal 30/60/90D history",
            details,
        )
    missing: list[str] = []
    for ticker, info in details["perTicker"].items():
        unread = [w for w, ok in (info.get("ready") or {}).items() if not ok]
        if unread:
            missing.append(f"{ticker}:{','.join(unread)}D")
    return _check(
        "history_readiness",
        STATUS_DEGRADED,
        "Internal 30/60/90D not ready for: " + (", ".join(missing) if missing else "no observations"),
        details,
    )


def aggregate_status(checks: list[dict]) -> str:
    overall = STATUS_HEALTHY
    for c in checks:
        overall = _worse(overall, str(c.get("status") or STATUS_FAILED))
    if overall not in VALID_STATUSES:
        return STATUS_FAILED
    return overall


def format_human_report(report: dict) -> str:
    lines = [f"Pipeline health: {report.get('status')}", ""]
    for c in report.get("checks") or []:
        lines.append(f"  [{c.get('status')}] {c.get('name')} — {c.get('summary')}")
    mutated = report.get("mutated")
    if mutated:
        lines.append("")
        lines.append("ERROR: health check mutated production files (forbidden)")
    lines.append("")
    lines.append("Exit: HEALTHY=0 DEGRADED=1 FAILED=2  (CI: --allow-degraded → FAILED=1)")
    return "\n".join(lines) + "\n"


def run_health_check(root: Path | None = None) -> dict:
    """Run every check. Never writes. Returns a JSON-serializable report."""
    root = Path(root or ROOT)
    before = fingerprint_production(root)
    checks: list[dict] = []
    try:
        canonical = check_canonical_history(root)
        checks.append(canonical)
        checks.append(check_runtime_materialization(root))
        checks.append(check_current_pointer(root))
        checks.append(check_canonical_git_sync(root))
        checks.append(check_recent_collection(root))
        checks.append(check_quarantine(root))
        checks.append(check_migration_audit(root))
        checks.append(
            check_history_readiness(root, canonical_ok=canonical["status"] != STATUS_FAILED)
        )
    except Exception as exc:
        checks.append(
            _check(
                "health_check_engine",
                STATUS_FAILED,
                f"unhandled error: {type(exc).__name__}: {exc}",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        )
    after = fingerprint_production(root)
    mutated = before != after
    if mutated:
        checks.append(
            _check(
                "read_only_guard",
                STATUS_FAILED,
                "health check mutated production files",
                {
                    "beforeKeys": sorted(before.keys()),
                    "afterKeys": sorted(after.keys()),
                    "changed": sorted(set(before.items()) ^ set(after.items()))[:20],
                },
            )
        )
    status = aggregate_status(checks)
    return {
        "status": status,
        "ok": status != STATUS_FAILED,
        "readOnly": True,
        "mutated": mutated,
        "root": str(root),
        "checks": checks,
        "exitCodes": {
            STATUS_HEALTHY: EXIT_HEALTHY,
            STATUS_DEGRADED: EXIT_DEGRADED,
            STATUS_FAILED: EXIT_FAILED,
            "allowDegradedFailed": 1,
        },
    }


def status_to_exit(status: str, *, allow_degraded: bool) -> int:
    if status == STATUS_HEALTHY:
        return EXIT_HEALTHY
    if status == STATUS_DEGRADED:
        return EXIT_HEALTHY if allow_degraded else EXIT_DEGRADED
    return 1 if allow_degraded else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only pipeline & canonical history health check (never mutates)"
    )
    ap.add_argument("--root", type=Path, default=None, help="Project root (default: repo root)")
    ap.add_argument("--json", action="store_true", help="Print JSON only to stdout")
    ap.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Exit 0 on HEALTHY or DEGRADED; exit 1 only on FAILED (CI gate)",
    )
    args = ap.parse_args(argv)
    report = run_health_check(args.root)
    human = format_human_report(report)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.json:
        print(payload)
        print(human, file=sys.stderr, end="")
    else:
        print(human, end="")
        print("---JSON---")
        print(payload)
    return status_to_exit(str(report.get("status") or STATUS_FAILED), allow_degraded=args.allow_degraded)


if __name__ == "__main__":
    sys.exit(main())

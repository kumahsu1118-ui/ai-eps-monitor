#!/usr/bin/env python3
"""Single E2E ingest entrypoint (deterministic, transactional).

Fixed flow:
  Incoming raw → Quality Gate (per-ticker LKG)
  → STAGE under data/staging/<runId>/ (runStatus=staged)
  → revision/daily/alerts/web build
  → atomic COMMIT to validated persistent history (runStatus=committed)
  → (optional) publish_prebuilt_site (no second export)

On export failure → ABORT (runStatus=aborted): must NOT become LKG;
incoming must NOT be marked successfully processed.

Browser/collection writes ONLY data/incoming/<UTC timestamp>.json.
Only this module may COMMIT validated snapshots + revision history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from atomic_io import append_jsonl_atomic, atomic_write_json  # noqa: E402
import snapshot_quality as sq  # noqa: E402

INCOMING_DIR = ROOT / "data" / "incoming"
SNAP_DIR = ROOT / "data" / "snapshots"
QUARANTINE_DIR = SNAP_DIR / "quarantine"
STAGING_DIR = ROOT / "data" / "staging"
REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
UNIVERSE_PATH = ROOT / "data" / "universe.json"


def utc_timestamp_filename(snap_utc: str | None = None) -> str:
    """Return YYYY-MM-DDTHHMMSSZ.json from ISO utc or now."""
    utc = snap_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})Z$", str(utc))
    if m:
        return f"{m.group(1)}T{m.group(2)}{m.group(3)}{m.group(4)}Z.json"
    day = str(utc)[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H%M%SZ.json")
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H%M%SZ.json")


def load_watchlist() -> list[str]:
    if UNIVERSE_PATH.exists():
        u = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        return list(u.get("tickers") or [])
    return ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]


def list_incoming() -> list[Path]:
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in INCOMING_DIR.glob("*.json"):
        if p.name.startswith("."):
            continue
        files.append(p)
    return sorted(files, key=lambda x: x.name)


def eps_equal(a, b) -> bool:
    na, nb = sq.to_num(a), sq.to_num(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    return abs(na - nb) < 1e-9


def revision_pct_str(prev, cur) -> str:
    """Revision %; previous=0 → N/A / unavailable (no div0)."""
    p = sq.to_num(prev)
    c = sq.to_num(cur)
    if p is None or c is None:
        return "n/a"
    if p == 0:
        return "N/A / unavailable"
    pct = (c - p) / abs(p) * 100.0
    return round(pct, 4)


def _fiscal_of_row(row: dict) -> str | None:
    if not isinstance(row, dict):
        return None
    for k in (
        "reportedFiscalPeriodEnding",
        "reported_fiscal_label",
        "reportedFiscalLabel",
        "fiscalPeriodEnding",
    ):
        v = row.get(k)
        if v and not sq.is_explicit_unavailable(v):
            return str(v).strip()
    return None


def _consensus_of_row(row: dict):
    if not isinstance(row, dict):
        return None
    return sq.to_num(row.get("consensus") if "consensus" in row else row.get("eps"))


def _calendar_of_row(row: dict, fiscal: str | None) -> str | None:
    align = row.get("calendar_alignment") or row.get("calendarAlignment") or row.get("Calendar Mapping")
    if align:
        return str(align)
    return sq.calendar_mapping_from_fiscal(fiscal)


def _norm_eps_token(v) -> str:
    n = sq.to_num(v)
    if n is not None:
        return f"{n:.6f}"
    if v is None:
        return "null"
    return str(v).strip()


def revision_event_id(event: dict) -> str:
    """Deterministic eventId: ticker + fiscal + previousEPS + currentEPS + updateTime."""
    ticker = str(event.get("Ticker") or "").upper()
    fiscal = str(
        event.get("Reported Fiscal Period Ending")
        or event.get("Fiscal Year")
        or event.get("reportedFiscalPeriodEnding")
        or ""
    ).strip()
    prev = _norm_eps_token(event.get("Previous EPS"))
    cur = _norm_eps_token(event.get("Current EPS"))
    ut = str(event.get("Update Time") or event.get("updateTime") or "").strip()
    raw = f"{ticker}|{fiscal}|{prev}|{cur}|{ut}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_hash_snapshot(snap: dict) -> str:
    payload = {k: v for k, v in (snap or {}).items() if k not in {"qualityGate", "runId", "runStatus"}}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_revision_history(rev_path: Path | None = None) -> list[dict]:
    path = rev_path or REV_PATH
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def known_revision_identities(history: list[dict] | None) -> set[tuple[str, str]]:
    """Set of (TICKER, fiscal) already present in revision history."""
    out: set[tuple[str, str]] = set()
    for row in history or []:
        if not isinstance(row, dict):
            continue
        t = str(row.get("Ticker") or "").upper()
        fiscal = (
            row.get("Fiscal Year")
            or row.get("Reported Fiscal Period Ending")
            or row.get("reportedFiscalPeriodEnding")
        )
        if t and fiscal and not sq.is_explicit_unavailable(fiscal):
            out.add((t, str(fiscal).strip()))
    return out


def known_event_ids(history: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for row in history or []:
        if not isinstance(row, dict):
            continue
        eid = row.get("eventId")
        if eid:
            out.add(str(eid))
        else:
            out.add(revision_event_id(row))
    return out


def generate_revision_events(
    current_fresh_snapshot: dict,
    last_known_good: dict[str, dict] | None,
    *,
    source: str | None = None,
    existing_history: list[dict] | None = None,
) -> list[dict]:
    """Deterministic revision events from current fresh snapshot vs per-ticker LKG.

    Identity: ticker + reportedFiscalPeriodEnding.
    Each event carries deterministic eventId for idempotent append.
    """
    events: list[dict] = []
    if not isinstance(current_fresh_snapshot, dict):
        return events
    tickers = current_fresh_snapshot.get("tickers") or {}
    if not isinstance(tickers, dict):
        return events
    lkg = last_known_good or {}
    known = known_revision_identities(existing_history)
    hist_last_eps: dict[tuple[str, str], float] = {}
    for row in existing_history or []:
        if not isinstance(row, dict):
            continue
        t = str(row.get("Ticker") or "").upper()
        fiscal = (
            row.get("Fiscal Year")
            or row.get("Reported Fiscal Period Ending")
        )
        if not t or not fiscal or sq.is_explicit_unavailable(fiscal):
            continue
        cur = sq.to_num(row.get("Current EPS"))
        if cur is not None:
            hist_last_eps[(t, str(fiscal).strip())] = cur
    snap_utc = current_fresh_snapshot.get("snapshot_utc") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    date = str(snap_utc)[:10]
    src = source or current_fresh_snapshot.get("source") or "Seeking Alpha"

    for t, td in tickers.items():
        if not isinstance(td, dict):
            continue
        if td.get("collectionFailed") or td.get("usingLastKnownGood"):
            continue
        tu = str(t).upper()
        prior_td = lkg.get(tu) or lkg.get(t) or {}
        prior_eps = (prior_td or {}).get("eps") or {} if isinstance(prior_td, dict) else {}
        prior_by_fiscal: dict[str, tuple] = {}
        prior_by_slot: dict[str, tuple] = {}
        if isinstance(prior_eps, dict):
            for slot, row in prior_eps.items():
                if not isinstance(row, dict):
                    continue
                fiscal = _fiscal_of_row(row)
                cons = _consensus_of_row(row)
                if fiscal:
                    prior_by_fiscal[fiscal] = (cons, row, slot)
                prior_by_slot[slot] = (cons, row, slot)

        cur_eps = td.get("eps") or {}
        if not isinstance(cur_eps, dict):
            continue
        for slot, row in cur_eps.items():
            if not isinstance(row, dict):
                continue
            fiscal = _fiscal_of_row(row)
            cur_cons = _consensus_of_row(row)
            if cur_cons is None:
                continue
            if not fiscal:
                continue
            align = _calendar_of_row(row, fiscal)
            prev_tuple = prior_by_fiscal.get(fiscal) or prior_by_slot.get(slot)
            if prev_tuple is None and (tu, fiscal) in hist_last_eps:
                prev_tuple = (hist_last_eps[(tu, fiscal)], {}, slot)
            if prev_tuple is None:
                if (tu, fiscal) in known:
                    continue
                ev = {
                    "Date": date,
                    "Ticker": tu,
                    "Fiscal Year": fiscal,
                    "Reported Fiscal Period Ending": fiscal,
                    "Calendar Alignment": align,
                    "Calendar Mapping": align,
                    "Previous EPS": "n/a (baseline)",
                    "Current EPS": cur_cons,
                    "Change": "n/a",
                    "Revision %": "n/a",
                    "Reason": "Baseline snapshot established",
                    "Source": src,
                    "Source Date": date,
                    "Update Time": snap_utc,
                }
                ev["eventId"] = revision_event_id(ev)
                events.append(ev)
                known.add((tu, fiscal))
                continue
            prev_cons, _prev_row, _ = prev_tuple
            if eps_equal(prev_cons, cur_cons):
                continue
            if prev_cons is None:
                ev = {
                    "Date": date,
                    "Ticker": tu,
                    "Fiscal Year": fiscal,
                    "Reported Fiscal Period Ending": fiscal,
                    "Calendar Alignment": align,
                    "Calendar Mapping": align,
                    "Previous EPS": "n/a (baseline)",
                    "Current EPS": cur_cons,
                    "Change": "n/a",
                    "Revision %": "n/a",
                    "Reason": "Baseline snapshot established",
                    "Source": src,
                    "Source Date": date,
                    "Update Time": snap_utc,
                }
                ev["eventId"] = revision_event_id(ev)
                events.append(ev)
                continue
            change = cur_cons - prev_cons
            rev = revision_pct_str(prev_cons, cur_cons)
            direction = "upgrade" if change > 0 else "downgrade"
            ev = {
                "Date": date,
                "Ticker": tu,
                "Fiscal Year": fiscal,
                "Reported Fiscal Period Ending": fiscal,
                "Calendar Alignment": align,
                "Calendar Mapping": align,
                "Previous EPS": prev_cons,
                "Current EPS": cur_cons,
                "Change": round(change, 6),
                "Revision %": rev,
                "Reason": f"Consensus EPS {direction} vs last-known-good",
                "Source": src,
                "Source Date": date,
                "Update Time": snap_utc,
            }
            ev["eventId"] = revision_event_id(ev)
            events.append(ev)
    return events


def append_revision_events(events: list[dict], rev_path: Path | None = None) -> int:
    """Append revision events to history.jsonl, deduping by eventId. Returns count appended."""
    if not events:
        return 0
    path = rev_path or REV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = known_event_ids(load_revision_history(path))
    to_write: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("eventId") or revision_event_id(ev)
        ev = dict(ev)
        ev["eventId"] = eid
        if eid in existing:
            continue
        existing.add(eid)
        to_write.append(ev)
    if not to_write:
        return 0
    append_jsonl_atomic(path, to_write)
    return len(to_write)


def immutable_validated_path(snap: dict, snap_utc: str) -> Path:
    """Choose non-overwriting validated path: timestamp, or timestamp+contentHash on collision."""
    fname = utc_timestamp_filename(snap_utc)
    path = SNAP_DIR / fname
    ch = content_hash_snapshot(snap)[:12]
    if not path.exists():
        return path
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if content_hash_snapshot(existing) == content_hash_snapshot(snap):
            return path  # identical content — reuse (still immutable)
    except Exception:
        pass
    # Same timestamp, different content → preserve both via contentHash suffix
    stem = fname.replace(".json", "")
    alt = SNAP_DIR / f"{stem}_{ch}.json"
    n = 0
    while alt.exists():
        try:
            existing = json.loads(alt.read_text(encoding="utf-8"))
            if content_hash_snapshot(existing) == content_hash_snapshot(snap):
                return alt
        except Exception:
            pass
        n += 1
        alt = SNAP_DIR / f"{stem}_{ch}_{n}.json"
    return alt


def update_snapshot_manifest(committed_name: str, snap_utc: str) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    try:
        files = [p.name for p in sq.list_validated_snapshots(SNAP_DIR)]
    except Exception:
        files = sorted(
            p.name
            for p in SNAP_DIR.glob("20*.json")
            if p.name not in {"latest.json", "manifest.json"} and not p.name.startswith("raw_")
        )
    if committed_name not in files:
        files.append(committed_name)
        files = sorted(set(files))
    atomic_write_json(
        SNAP_DIR / "manifest.json",
        {
            "latest": committed_name,
            "snapshot_utc": snap_utc,
            "files": files,
            "immutable": True,
        },
    )


def write_incoming(snap: dict, snap_utc: str | None = None) -> Path:
    """Collection helper: write ONLY to data/incoming/<UTC timestamp>.json."""
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    utc = snap_utc or snap.get("snapshot_utc") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    snap = dict(snap)
    snap["snapshot_utc"] = utc
    fname = utc_timestamp_filename(utc)
    path = INCOMING_DIR / fname
    n = 0
    while path.exists():
        n += 1
        stem = fname.replace(".json", "")
        path = INCOMING_DIR / f"{stem}_{n}.json"
    atomic_write_json(path, snap)
    return path


def _write_run_status(stage_dir: Path, status: str, extra: dict | None = None) -> None:
    meta = {"runStatus": status, "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    # Prefer merging with existing run.json
    run_path = stage_dir / "run.json"
    existing = {}
    if run_path.exists():
        try:
            existing = json.loads(run_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.update(meta)
    atomic_write_json(run_path, existing)


def commit_staged_run(
    stage_dir: Path,
    snap_norm: dict,
    events: list[dict],
    snap_utc: str,
) -> Path:
    """Atomic COMMIT: validated snapshot + revision events → persistent history."""
    validated_path = immutable_validated_path(snap_norm, snap_utc)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    if not validated_path.exists():
        atomic_write_json(validated_path, snap_norm)
    try:
        atomic_write_json(SNAP_DIR / "latest.json", snap_norm)
    except Exception:
        pass
    update_snapshot_manifest(validated_path.name, snap_utc)
    n = append_revision_events(events, REV_PATH)
    _write_run_status(
        stage_dir,
        "committed",
        {
            "validatedPath": str(validated_path),
            "revisionEventsAppended": n,
            "committedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    return validated_path


def abort_staged_run(stage_dir: Path, reason: str) -> None:
    _write_run_status(stage_dir, "aborted", {"abortReason": reason})


def publish_prebuilt_site() -> int:
    """Publish already-exported web/ without recomputing EPS/history/alerts."""
    env = os.environ.copy()
    env["PIPELINE_LOCK_HELD"] = env.get("PIPELINE_LOCK_HELD", "1")
    env["SKIP_EXPORT"] = "1"
    env["PUBLISH_PREBUILT"] = "1"
    return subprocess.call(
        ["bash", str(_here / "publish_github_pages.sh")],
        cwd=str(ROOT),
        env=env,
    )


def ingest_and_build(
    incoming_path: Path,
    *,
    expected_tickers: list[str] | None = None,
    run_export: bool = True,
    run_publish: bool = False,
) -> dict:
    """Process one incoming raw JSON through transactional staged→commit pipeline."""
    expected = expected_tickers or load_watchlist()
    raw = json.loads(incoming_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"Incoming snapshot not an object: {incoming_path}")

    snap_utc = raw.get("snapshot_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + content_hash_snapshot(raw)[:8]
    stage_dir = STAGING_DIR / run_id
    stage_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "incoming": str(incoming_path),
        "runId": run_id,
        "runStatus": "staged",
        "gate": {},
        "revisionEventsAppended": 0,
        "validatedPath": None,
        "quarantinePath": None,
        "stageDir": str(stage_dir),
        "ok": False,
    }

    _write_run_status(
        stage_dir,
        "staged",
        {"runId": run_id, "incoming": str(incoming_path), "snapshot_utc": snap_utc},
    )

    # Gate against validated LKG only (exclude any same-named candidate)
    tentative_name = utc_timestamp_filename(snap_utc)
    lkg = sq.load_last_known_good_by_ticker(
        SNAP_DIR, exclude_path=SNAP_DIR / tentative_name, expected_tickers=expected, root=ROOT
    )
    prior_snap = {"tickers": dict(lkg)} if lkg else None

    gate = sq.gate_snapshot(
        raw,
        prior_snap,
        expected_tickers=expected,
        lkg_by_ticker=lkg,
        root=ROOT,
    )
    snap_norm = gate.get("normalizedSnapshot") or raw
    snap_norm = dict(snap_norm)
    snap_norm["snapshot_utc"] = snap_utc
    snap_norm["collectionRunId"] = run_id
    snap_norm["qualityGate"] = {
        "status": gate.get("status"),
        "reason": gate.get("reason"),
        "publishable": gate.get("publishable"),
        "missingTickers": gate.get("missingTickers") or [],
        "extremeChanges": gate.get("extremeChanges") or [],
        "checked": True,
    }
    result["gate"] = {
        "status": gate.get("status"),
        "publishable": gate.get("publishable"),
        "reason": gate.get("reason"),
        "missingTickers": gate.get("missingTickers") or [],
    }

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    if gate.get("publishable") is not True:
        qpath = QUARANTINE_DIR / f"{tentative_name}.quarantine"
        quarantine = dict(snap_norm)
        quarantine["qualityGate"] = gate
        quarantine["status"] = gate.get("status")
        atomic_write_json(qpath, quarantine)
        abort_staged_run(stage_dir, f"gate:{gate.get('status')}:{gate.get('reason')}")
        result["quarantinePath"] = str(qpath)
        result["runStatus"] = "aborted"
        result["ok"] = False
        print(
            f"INGEST REJECT status={gate.get('status')} reason={gate.get('reason')} "
            f"quarantine={qpath}"
        )
        return result

    # STAGE only — do NOT write validated / revisions / mark incoming yet
    stage_snap = stage_dir / "snapshot.json"
    atomic_write_json(stage_snap, snap_norm)
    events = generate_revision_events(
        snap_norm, lkg, existing_history=load_revision_history(REV_PATH)
    )
    for ev in events:
        if "eventId" not in ev:
            ev["eventId"] = revision_event_id(ev)
    atomic_write_json(stage_dir / "proposed_revisions.json", {"events": events})
    (stage_dir / "proposed_revisions.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )

    if run_export:
        env = os.environ.copy()
        env["PIPELINE_LOCK_HELD"] = env.get("PIPELINE_LOCK_HELD", "1")
        env["INGEST_GATED_SNAPSHOT"] = str(stage_snap)
        env["INGEST_SNAPSHOT_UTC"] = snap_utc
        env["INGEST_REVISIONS_DONE"] = "1"
        env["INGEST_COLLECTION_RUN_ID"] = run_id
        # Revisions owned by commit (INGEST_REVISIONS_DONE=1). Daily/alerts/web built here;
        # FAULT_INJECT at export start still aborts before history mutations.
        rc = subprocess.call(
            [sys.executable, str(_here / "export_web_data.py")],
            cwd=str(ROOT),
            env=env,
        )
        result["exportRc"] = rc
        if rc != 0:
            abort_staged_run(stage_dir, f"export_exit_{rc}")
            result["runStatus"] = "aborted"
            result["ok"] = False
            print(
                f"ERROR: export_web_data.py exited {rc} — ABORT staged run "
                f"(validated/revisions/incoming untouched)",
                file=sys.stderr,
            )
            return result

    # COMMIT after successful export (or when export skipped after gate)
    validated_path = commit_staged_run(stage_dir, snap_norm, events, snap_utc)
    result["validatedPath"] = str(validated_path)
    try:
        run_meta = json.loads((stage_dir / "run.json").read_text(encoding="utf-8"))
        result["revisionEventsAppended"] = int(run_meta.get("revisionEventsAppended") or 0)
    except Exception:
        result["revisionEventsAppended"] = 0

    # Mark processed incoming ONLY after successful commit
    try:
        processed = incoming_path.with_suffix(incoming_path.suffix + ".processed")
        if incoming_path.exists():
            incoming_path.rename(processed)
        result["incomingProcessed"] = str(processed)
    except Exception as rename_exc:
        print(f"WARNING: could not mark incoming processed: {rename_exc}", file=sys.stderr)

    result["runStatus"] = "committed"
    result["ok"] = True
    print(
        f"INGEST OK status={gate.get('status')} validated={validated_path} "
        f"revisions=+{result['revisionEventsAppended']} runId={run_id}"
    )

    if run_publish:
        # Publish prebuilt site — must NOT re-export / recompute revisions
        rc = publish_prebuilt_site()
        result["publishRc"] = rc
        if rc not in (0,):
            result["ok"] = False
            print(f"ERROR: publish_prebuilt_site exited {rc}", file=sys.stderr)

    return result


# Back-compat alias
ingest_incoming_file = ingest_and_build


def ingest_latest_incoming(**kwargs) -> dict:
    files = list_incoming()
    if not files:
        raise SystemExit(f"No incoming JSON under {INCOMING_DIR}")
    return ingest_and_build(files[-1], **kwargs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic transactional ingest entrypoint")
    ap.add_argument(
        "incoming",
        nargs="?",
        help="Path to incoming raw JSON (default: latest under data/incoming/)",
    )
    ap.add_argument("--no-export", action="store_true", help="Skip web export / alerts")
    ap.add_argument(
        "--publish",
        action="store_true",
        help="After single export+commit, publish_prebuilt_site (no second export)",
    )
    ap.add_argument("--write-incoming", help="Write given snapshot JSON path into data/incoming/ only")
    args = ap.parse_args(argv)

    if args.write_incoming:
        src = Path(args.write_incoming)
        snap = json.loads(src.read_text(encoding="utf-8"))
        path = write_incoming(snap)
        print(f"Wrote incoming {path}")
        return 0

    from atomic_io import GlobalPipelineLock, PipelineBusy, default_pipeline_lock_path, RUN_IN_PROGRESS_MSG

    lock_cm = None
    if os.environ.get("PIPELINE_LOCK_HELD") != "1":
        try:
            lock_cm = GlobalPipelineLock(default_pipeline_lock_path(ROOT), non_blocking=True)
            lock_cm.__enter__()
            os.environ["PIPELINE_LOCK_HELD"] = "1"
        except PipelineBusy:
            print(RUN_IN_PROGRESS_MSG, flush=True)
            return 2
    try:
        if args.incoming:
            result = ingest_and_build(
                Path(args.incoming),
                run_export=not args.no_export,
                run_publish=args.publish,
            )
        else:
            result = ingest_latest_incoming(
                run_export=not args.no_export,
                run_publish=args.publish,
            )
        return 0 if result.get("ok") else 1
    finally:
        if lock_cm is not None:
            lock_cm.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Production EPS history migration / backfill (fail-closed, non-destructive).

Reads recoverable observations from workspace sources and proposes Git-tracked
canonical files under ``data/history/eps_daily/YYYY-MM.jsonl``.

Commands
--------
  python3 tools/migrate_eps_history.py --audit   # completely read-only
  python3 tools/migrate_eps_history.py --apply   # atomic write; fail-closed

Never mutates ``data/CURRENT.json``. Never invents EPS or fiscal endings.
Never silently chooses a conflict winner. Never uses mappedYear/slot as identity.
Existing canonical always wins unless exact payload equality proves the same
observation; any identity with two distinct payloads aborts ``--apply``.

Source priority (documentation / audit; not a silent winner picker)
-------------------------------------------------------------------
1. existing Git canonical ``data/history/eps_daily/*.jsonl``
2. CURRENT generation canonical months
3. validated runtime ``data/daily_eps_snapshots/daily.jsonl``
4. CURRENT generation ``daily.jsonl``
5. older *committed* generation history / daily (orphan/aborted skipped)
6. public derived exports only when date, ticker, normalized fiscal identity,
   consensus, and updateTime (or a field proving its absence) are present

Fiscal reconstructability
-------------------------
Canonical identity uses PR #10/#12 ``fiscal_identity_key`` /
``normalize_fiscal_period_label``. Historical ``daily.jsonl`` rows often store
``reportedFiscalLabel`` (e.g. ``Jan 2027``) and omit ``reportedFiscalPeriodEnding``.
That label *is* the already-tested canonical fiscal identity (normalized
``Mon YYYY``); this tool does **not** invent a calendar date such as
``2027-01-31``. mappedYear / slot / ``YYYYE`` alone is rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from atomic_io import (  # noqa: E402
    RUN_IN_PROGRESS_MSG,
    GlobalPipelineLock,
    PipelineBusy,
    atomic_write_bytes,
    atomic_write_text,
    default_pipeline_lock_path,
)
import canonical_eps_history as ceh  # noqa: E402
from revision_windows import to_num  # noqa: E402

SLOT_RE = re.compile(r"^\d{4}E$", re.I)
CURRENT_REL = Path("data") / "CURRENT.json"
RUNTIME_DAILY_REL = Path("data") / "daily_eps_snapshots" / "daily.jsonl"
GENERATIONS_REL = Path("data") / "generations"
STAGING_REL = Path("data") / "staging"
PUBLIC_DERIVED_RELS = (
    Path("data") / "eps_history.json",
    Path("web") / "data" / "eps_history.json",
)
STAGING_DIRNAME = ".migration_staging"
BACKUP_DIRNAME = ".migration_backup"
# 1-based: FAULT_INJECT_MIGRATION_REPLACE_AFTER=2 → first live replace
# succeeds, second raises (rollback must restore every original month).
FAULT_INJECT_REPLACE_AFTER = "FAULT_INJECT_MIGRATION_REPLACE_AFTER"

# Lower number = higher priority (documented ordering; not a conflict winner).
SOURCE_PRIORITY = {
    "existing_canonical": 1,
    "current_generation_canonical": 2,
    "runtime_daily": 3,
    "current_generation_daily": 4,
    "older_committed_generation_canonical": 5,
    "older_committed_generation_daily": 6,
    "public_derived": 7,
}

COMMITTED_MAT_STATUSES = frozenset({"success", "ok", "failed", "pending"})
ABORTED_STATUSES = frozenset({"aborted", "abort"})


class MigrationError(ValueError):
    """Blocking migration failure (conflicts / validation). No partial write."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def fingerprint_workspace(root: Path) -> dict[str, str]:
    """Content fingerprints used to prove --audit is read-only / CURRENT unchanged."""
    root = Path(root)
    out: dict[str, str] = {}
    watch = [
        CURRENT_REL,
        RUNTIME_DAILY_REL,
        Path("data") / ".materialized_run_id",
    ]
    for rel in watch:
        p = root / rel
        if p.is_file():
            out[str(rel)] = _sha256_file(p)
    hist = ceh.history_dir(root)
    if hist.is_dir():
        for p in sorted(hist.glob("*.jsonl")):
            out[f"history:{p.name}"] = _sha256_file(p)
        keep = hist / ".gitkeep"
        if keep.is_file():
            out["history:.gitkeep"] = _sha256_file(keep)
    gens = root / GENERATIONS_REL
    if gens.is_dir():
        for p in sorted(gens.rglob("*")):
            if p.is_file():
                out[f"gen:{p.relative_to(root)}"] = _sha256_file(p)
    daily_dir = (root / RUNTIME_DAILY_REL).parent
    if daily_dir.is_dir():
        for p in sorted(daily_dir.glob("*")):
            if p.is_file():
                out[f"daily:{p.name}"] = _sha256_file(p)
    return out


def load_current_pointer(root: Path) -> dict | None:
    path = Path(root) / CURRENT_REL
    if not path.is_file():
        return None
    try:
        obj = _read_json(path)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def current_run_id(root: Path) -> str | None:
    cur = load_current_pointer(root)
    if not cur:
        return None
    rid = cur.get("runId") or cur.get("run_id")
    if rid:
        return str(rid)
    gen = str(cur.get("generation") or "")
    if gen:
        return Path(gen).name
    return None


def _meta_status(meta: dict | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    raw = meta.get("runStatus") or meta.get("status")
    if raw in (None, ""):
        return None
    return str(raw).strip().lower()


def generation_is_trusted(root: Path, run_id: str, *, current_id: str | None) -> tuple[bool, str]:
    """Committed/CURRENT lineage only. Orphan and aborted generations are not trusted."""
    gen_dir = Path(root) / GENERATIONS_REL / run_id
    meta_path = gen_dir / "generation_meta.json"
    meta: dict = {}
    if meta_path.is_file():
        try:
            loaded = _read_json(meta_path)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}

    status = _meta_status(meta)
    abort_reason = meta.get("abortReason") or meta.get("abort_reason")
    if status in ABORTED_STATUSES or abort_reason:
        return False, f"aborted:{status or abort_reason}"

    stage_meta = Path(root) / STAGING_REL / run_id / "run_status.json"
    if not stage_meta.is_file():
        # ingest writes _write_run_status under staging/<runId>/
        alt = Path(root) / STAGING_REL / run_id / "status.json"
        stage_meta = stage_meta if stage_meta.is_file() else alt
    if stage_meta.is_file():
        try:
            st = _read_json(stage_meta)
            if isinstance(st, dict):
                st_status = str(st.get("runStatus") or st.get("status") or "").strip().lower()
                if st_status in ABORTED_STATUSES or st.get("abortReason"):
                    return False, f"staging_aborted:{st_status or st.get('abortReason')}"
                if st_status == "committed" and current_id and run_id != current_id:
                    return True, "staging_committed"
        except Exception:
            pass

    if current_id and run_id == current_id:
        return True, "current_pointer"

    mat = str(meta.get("materializationStatus") or "").strip().lower()
    if mat in COMMITTED_MAT_STATUSES:
        return True, f"materializationStatus={mat}"
    if status == "committed":
        return True, "runStatus=committed"
    if meta.get("committedAt"):
        return True, "committedAt"

    # generation_meta is written *before* CURRENT; without a commit signal this
    # is an orphan / crash-before-CURRENT package and must not be imported.
    return False, "orphan_uncommitted"


def _iter_raw_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    """Load JSONL objects. Returns (rows, malformed_rejections). Never invents."""
    rows: list[dict] = []
    rejected: list[dict] = []
    if not path.is_file():
        return rows, rejected
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception as exc:
            rejected.append(
                {
                    "reason": "malformed_jsonl",
                    "path": str(path),
                    "line": i,
                    "detail": str(exc),
                }
            )
            continue
        if not isinstance(obj, dict):
            rejected.append(
                {
                    "reason": "malformed_jsonl",
                    "path": str(path),
                    "line": i,
                    "detail": "row is not an object",
                }
            )
            continue
        rows.append(obj)
    return rows, rejected


def _fiscal_raw(row: dict) -> Any:
    return (
        row.get("reportedFiscalPeriodEnding")
        or row.get("reportedFiscalLabel")
        or row.get("fiscalKey")
        or row.get("Fiscal Year")
        or row.get("reported_fiscal_label")
        or row.get("fiscalPeriodEnding")
    )


def _looks_like_slot(value: Any) -> bool:
    if value is None:
        return False
    return bool(SLOT_RE.match(str(value).strip()))


def row_had_update_time(row: dict) -> bool:
    raw = row.get("updateTime") if row.get("updateTime") not in (None, "") else row.get("Update Time")
    if raw in (None, ""):
        return False
    return bool(str(raw).strip())


def classify_source_row(row: dict) -> tuple[dict | None, str | None]:
    """Admit via PR #12 canonical rules with an extra mappedYear-only guard.

    Returns (canonical_obs, rejection_reason).
    """
    if not isinstance(row, dict):
        return None, "not_an_object"
    src = str(row.get("source") or "").strip()
    if src in ceh.SEED_SOURCES:
        return None, "seed_from_revision_history"
    if row.get("collectionFailed") or row.get("usingLastKnownGood"):
        return None, "collection_failed_or_lkg"
    fiscal = _fiscal_raw(row)
    if fiscal in (None, "") or _looks_like_slot(fiscal):
        # mappedYear / slot must never become identity, even if stuffed into a
        # fiscal-looking field.
        if row.get("mappedYear") or row.get("slot") or _looks_like_slot(fiscal):
            if fiscal in (None, "") or _looks_like_slot(fiscal):
                return None, "mapped_year_not_identity"
        return None, "invalid_fiscal_identity"
    cons = to_num(row.get("consensus") if "consensus" in row else row.get("eps"))
    if cons is None:
        return None, "invalid_consensus"
    date_str = str(row.get("date") or row.get("Date") or "").strip()[:10]
    if not ceh.DATE_RE.match(date_str):
        return None, "invalid_date"
    obs = ceh.canonical_observation_from_row(row)
    if obs is None:
        ident = ceh.observation_identity(row)
        if ident is None:
            return None, "invalid_fiscal_identity"
        return None, "rejected_by_canonical_admission"
    # Belt-and-suspenders: canonical fiscal must be Mon YYYY, not a display slot.
    if _looks_like_slot(obs.get("reportedFiscalPeriodEnding")):
        return None, "mapped_year_not_identity"
    return obs, None


def _empty_source_report(name: str, kind: str, path: str | None, *, exists: bool) -> dict:
    return {
        "name": name,
        "kind": kind,
        "path": path,
        "exists": exists,
        "trusted": True,
        "trustReason": None,
        "priority": SOURCE_PRIORITY.get(kind, 99),
        "rowCount": 0,
        "raw": 0,
        "validCandidates": 0,
        "uniqueIdentities": 0,
        "duplicateIdentities": 0,
        "conflictingIdentities": 0,
        "missingUpdateTimeCount": 0,
        "invalidFiscalIdentityCount": 0,
        "invalidConsensusCount": 0,
        "rejectedCount": 0,
        "earliest": None,
        "latest": None,
        "limitations": [],
        "rejectedReasons": {},
    }


def _source_stats(name: str, kind: str, path: Path | None, rows: list[dict], extra_rejected: list[dict] | None = None) -> dict:
    extra_rejected = extra_rejected or []
    exists = bool(path and path.exists()) if path is not None else False
    report = _empty_source_report(name, kind, str(path) if path else None, exists=exists)
    report["rowCount"] = len(rows)
    report["raw"] = len(rows) + len(extra_rejected)
    by_ident: dict[tuple, list[str]] = defaultdict(list)
    reasons: dict[str, int] = defaultdict(int)
    for rej in extra_rejected:
        reasons[str(rej.get("reason") or "malformed_jsonl")] += 1
        report["rejectedCount"] += 1
    dates: list[str] = []
    valid = 0
    missing_ut = 0
    for row in rows:
        if not row_had_update_time(row):
            missing_ut += 1
        obs, reason = classify_source_row(row)
        if obs is None:
            report["rejectedCount"] += 1
            rsn = reason or "rejected"
            reasons[rsn] += 1
            if rsn in {"invalid_fiscal_identity", "mapped_year_not_identity"}:
                report["invalidFiscalIdentityCount"] += 1
            if rsn == "invalid_consensus":
                report["invalidConsensusCount"] += 1
            continue
        valid += 1
        ident = ceh.identity_key(obs)
        by_ident[ident].append(ceh.canonical_payload_fingerprint(obs))
        dates.append(obs["date"])
    report["validCandidates"] = valid
    report["uniqueIdentities"] = len(by_ident)
    dupes = 0
    conflicts = 0
    for fps in by_ident.values():
        if len(fps) <= 1:
            continue
        uniq = set(fps)
        if len(uniq) == 1:
            dupes += 1
        else:
            conflicts += 1
    report["duplicateIdentities"] = dupes
    report["conflictingIdentities"] = conflicts
    report["missingUpdateTimeCount"] = missing_ut
    report["rejectedReasons"] = dict(reasons)
    if dates:
        report["earliest"] = min(dates)
        report["latest"] = max(dates)
    return report


def _public_derived_limitations() -> str:
    return (
        "Public derived eps_history.json is a chart collapse (ticker/slot/date). "
        "Admitted only when a row reconstructs date, ticker, normalized "
        "reportedFiscalPeriodEnding (from reportedFiscalLabel via PR #10/#12 "
        "normalize_fiscal_period_label), consensus, and updateTime or a field "
        "that proves updateTime was originally absent. mappedYear + EPS is not "
        "enough. Missing updateTime with no explicit null/empty field is not "
        "provable absence — those exports are skipped, not fabricated."
    )


def load_public_derived_rows(path: Path) -> tuple[list[dict], list[dict], list[str]]:
    """Attempt to reconstruct canonical-capable rows from public eps_history.json.

    Skip (do not fabricate) when updateTime cannot be proven present or absent.
    """
    limitations = [_public_derived_limitations()]
    rejected: list[dict] = []
    rows: list[dict] = []
    if not path.is_file():
        return rows, rejected, limitations
    try:
        payload = _read_json(path)
    except Exception as exc:
        rejected.append({"reason": "malformed_json", "path": str(path), "detail": str(exc)})
        limitations.append("file exists but is not parseable JSON")
        return rows, rejected, limitations
    if not isinstance(payload, dict):
        rejected.append({"reason": "malformed_json", "path": str(path), "detail": "not an object"})
        return rows, rejected, limitations

    skipped_no_ut = 0
    skipped_no_fiscal = 0
    for ticker, slots in payload.items():
        if not isinstance(slots, dict):
            continue
        for slot, series in slots.items():
            if not isinstance(series, list):
                continue
            for pt in series:
                if not isinstance(pt, dict):
                    continue
                fiscal = (
                    pt.get("reportedFiscalPeriodEnding")
                    or pt.get("reportedFiscalLabel")
                    or pt.get("fiscalKey")
                    or pt.get("reported_fiscal_label")
                )
                if fiscal in (None, "") or _looks_like_slot(fiscal):
                    skipped_no_fiscal += 1
                    rejected.append(
                        {
                            "reason": "mapped_year_not_identity",
                            "path": str(path),
                            "ticker": ticker,
                            "slot": slot,
                        }
                    )
                    continue
                # updateTime must be present as a key (value may be null → fallback)
                # or an explicit empty field. A missing key is not provable absence.
                if "updateTime" not in pt and "Update Time" not in pt:
                    skipped_no_ut += 1
                    rejected.append(
                        {
                            "reason": "public_derived_unreconstructable_updatetime",
                            "path": str(path),
                            "ticker": ticker,
                            "date": pt.get("date"),
                        }
                    )
                    continue
                rows.append(
                    {
                        "date": pt.get("date"),
                        "ticker": ticker,
                        "reportedFiscalPeriodEnding": fiscal,
                        "reportedFiscalLabel": fiscal,
                        "consensus": pt.get("consensus") if "consensus" in pt else pt.get("eps"),
                        "analysts": pt.get("analysts") if "analysts" in pt else pt.get("analystCount"),
                        "updateTime": pt.get("updateTime") if "updateTime" in pt else pt.get("Update Time"),
                        "source": "public_derived",
                    }
                )
    if skipped_no_ut:
        limitations.append(
            f"{skipped_no_ut} point(s) skipped: no updateTime field (not provable absence)"
        )
    if skipped_no_fiscal:
        limitations.append(
            f"{skipped_no_fiscal} point(s) skipped: no reconstructable fiscal identity"
        )
    if not rows:
        limitations.append("no reconstructable public-derived observations")
    return rows, rejected, limitations


def _generation_dirs(root: Path) -> list[Path]:
    d = Path(root) / GENERATIONS_REL
    if not d.is_dir():
        return []
    return sorted([p for p in d.iterdir() if p.is_dir()], key=lambda p: p.name)


def _canonical_month_paths(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    return sorted(
        [p for p in dir_path.iterdir() if p.is_file() and ceh.MONTH_FILE_RE.match(p.name)],
        key=lambda p: p.name,
    )


def discover_sources(root: Path, *, source_order: str = "priority") -> list[dict]:
    """Build ordered source descriptors. ``source_order`` is scan order only."""
    root = Path(root)
    cur_id = current_run_id(root)
    sources: list[dict] = []

    hist_dir = ceh.history_dir(root)
    month_files = _canonical_month_paths(hist_dir)
    sources.append(
        {
            "name": "existing_canonical",
            "kind": "existing_canonical",
            "path": hist_dir,
            "paths": month_files,
            "loader": "canonical_jsonl",
            "trusted": True,
            "trustReason": "git_canonical",
        }
    )

    gens = _generation_dirs(root)
    current_gen = None
    older_trusted: list[Path] = []
    skipped_gens: list[dict] = []
    for g in gens:
        trusted, reason = generation_is_trusted(root, g.name, current_id=cur_id)
        if not trusted:
            skipped_gens.append({"runId": g.name, "reason": reason})
            continue
        if cur_id and g.name == cur_id:
            current_gen = g
        else:
            older_trusted.append(g)

    if current_gen is not None:
        cdir = current_gen / "history" / "eps_daily"
        sources.append(
            {
                "name": f"current_generation_canonical:{current_gen.name}",
                "kind": "current_generation_canonical",
                "path": cdir,
                "paths": _canonical_month_paths(cdir),
                "loader": "canonical_jsonl",
                "trusted": True,
                "trustReason": "current_pointer",
                "runId": current_gen.name,
            }
        )

    runtime = root / RUNTIME_DAILY_REL
    sources.append(
        {
            "name": "runtime_daily",
            "kind": "runtime_daily",
            "path": runtime,
            "paths": [runtime] if runtime.is_file() else [],
            "loader": "raw_jsonl",
            "trusted": True,
            "trustReason": "runtime_cache",
        }
    )

    if current_gen is not None:
        dpath = current_gen / "daily_eps_snapshots" / "daily.jsonl"
        sources.append(
            {
                "name": f"current_generation_daily:{current_gen.name}",
                "kind": "current_generation_daily",
                "path": dpath,
                "paths": [dpath] if dpath.is_file() else [],
                "loader": "raw_jsonl",
                "trusted": True,
                "trustReason": "current_pointer",
                "runId": current_gen.name,
            }
        )

    for g in older_trusted:
        cdir = g / "history" / "eps_daily"
        sources.append(
            {
                "name": f"older_committed_generation_canonical:{g.name}",
                "kind": "older_committed_generation_canonical",
                "path": cdir,
                "paths": _canonical_month_paths(cdir),
                "loader": "canonical_jsonl",
                "trusted": True,
                "trustReason": "committed_lineage",
                "runId": g.name,
            }
        )
        dpath = g / "daily_eps_snapshots" / "daily.jsonl"
        sources.append(
            {
                "name": f"older_committed_generation_daily:{g.name}",
                "kind": "older_committed_generation_daily",
                "path": dpath,
                "paths": [dpath] if dpath.is_file() else [],
                "loader": "raw_jsonl",
                "trusted": True,
                "trustReason": "committed_lineage",
                "runId": g.name,
            }
        )

    for rel in PUBLIC_DERIVED_RELS:
        p = root / rel
        sources.append(
            {
                "name": f"public_derived:{rel.as_posix()}",
                "kind": "public_derived",
                "path": p,
                "paths": [p] if p.is_file() else [],
                "loader": "public_derived",
                "trusted": True,
                "trustReason": "reconstructable_only",
            }
        )

    for skipped in skipped_gens:
        sources.append(
            {
                "name": f"skipped_generation:{skipped['runId']}",
                "kind": "skipped_untrusted_generation",
                "path": root / GENERATIONS_REL / skipped["runId"],
                "paths": [],
                "loader": "skipped",
                "trusted": False,
                "trustReason": skipped["reason"],
                "runId": skipped["runId"],
            }
        )

    if source_order == "reverse":
        sources = list(reversed(sources))
    elif source_order not in {"priority", "reverse"}:
        raise MigrationError(f"unknown source_order: {source_order}")
    return sources


def _load_source_rows(src: dict) -> tuple[list[dict], list[dict], list[str]]:
    limitations: list[str] = []
    rows: list[dict] = []
    extra_rejected: list[dict] = []
    loader = src.get("loader")
    if loader == "skipped":
        limitations.append(
            f"generation {src.get('runId')} not imported ({src.get('trustReason')})"
        )
        return rows, extra_rejected, limitations
    paths: list[Path] = list(src.get("paths") or [])
    if loader == "public_derived":
        if not paths:
            limitations.append(_public_derived_limitations())
            limitations.append("file does not exist")
            return rows, extra_rejected, limitations
        for p in paths:
            r, rej, lim = load_public_derived_rows(p)
            rows.extend(r)
            extra_rejected.extend(rej)
            limitations.extend(lim)
        return rows, extra_rejected, limitations
    if not paths:
        if src.get("kind") == "existing_canonical":
            limitations.append("no live data/history/eps_daily/*.jsonl yet")
        elif "generation" in str(src.get("kind")):
            limitations.append("path not present on this generation")
        else:
            limitations.append("file does not exist")
        return rows, extra_rejected, limitations
    for p in paths:
        chunk, rej = _iter_raw_jsonl(p)
        rows.extend(chunk)
        extra_rejected.extend(rej)
    return rows, extra_rejected, limitations


def _per_ticker(rows: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        t = str(r.get("ticker") or "")
        if t:
            counts[t] += 1
    return dict(sorted(counts.items()))


def _date_range(rows: Iterable[dict]) -> tuple[str | None, str | None]:
    dates = [str(r.get("date")) for r in rows if r.get("date")]
    if not dates:
        return None, None
    return min(dates), max(dates)


def plan_migration(root: Path, *, source_order: str = "priority") -> dict:
    """Build a complete migration plan. Read-only. Never writes."""
    root = Path(root)
    sources = discover_sources(root, source_order=source_order)
    source_reports: list[dict] = []
    blocking: list[str] = []

    # identity -> list of {obs, source, fingerprint, used_fallback}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    raw_total = 0
    valid_total = 0
    rejected_total = 0
    fallback_total = 0
    rejected_reasons: dict[str, int] = defaultdict(int)

    for src in sources:
        rows, extra_rejected, limitations = _load_source_rows(src)
        report = _source_stats(src["name"], src["kind"], src.get("path"), rows, extra_rejected)
        report["trusted"] = bool(src.get("trusted", True))
        report["trustReason"] = src.get("trustReason")
        report["limitations"] = limitations
        report["priority"] = SOURCE_PRIORITY.get(src["kind"], 99)
        if src.get("runId"):
            report["runId"] = src["runId"]
        source_reports.append(report)

        if src.get("loader") == "skipped":
            continue

        raw_total += report["raw"]
        rejected_total += report["rejectedCount"]
        for k, n in (report.get("rejectedReasons") or {}).items():
            rejected_reasons[k] += n

        if src.get("kind") == "existing_canonical":
            # Strict: malformed canonical is a blocking validation error.
            for rej in extra_rejected:
                blocking.append(
                    f"existing canonical malformed: {rej.get('path')}:{rej.get('line')} {rej.get('detail')}"
                )

        for row in rows:
            obs, reason = classify_source_row(row)
            if obs is None:
                continue
            used_fallback = not row_had_update_time(row)
            if used_fallback:
                fallback_total += 1
            valid_total += 1
            ident = ceh.identity_key(obs)
            grouped[ident].append(
                {
                    "obs": obs,
                    "fingerprint": ceh.canonical_payload_fingerprint(obs),
                    "source": src["name"],
                    "kind": src["kind"],
                    "priority": SOURCE_PRIORITY.get(src["kind"], 99),
                    "usedFallback": used_fallback,
                }
            )

    try:
        live_existing = ceh.load_all_canonical_rows(root, strict=True)
    except ceh.CanonicalHistoryError as exc:
        blocking.append(f"existing canonical validation: {exc}")
        live_existing = []

    live_map: dict[tuple, dict] = {}
    for r in live_existing:
        live_map[ceh.identity_key(r)] = r

    # Start from live canonical so existing observations are never dropped.
    proposed_by_ident: dict[tuple, dict] = dict(live_map)
    already = 0
    exact_dupes = 0
    new_obs: list[dict] = []
    conflicts: list[dict] = []
    conflict_idents: set[tuple] = set()

    for ident, hits in grouped.items():
        fps = {h["fingerprint"] for h in hits}
        if ident in live_map:
            fps.add(ceh.canonical_payload_fingerprint(live_map[ident]))
        kinds = {h["kind"] for h in hits}
        if len(fps) > 1:
            conflict_idents.add(ident)
            conflicts.append(
                {
                    "identity": {
                        "ticker": ident[0],
                        "reportedFiscalPeriodEnding": ident[1],
                        "date": ident[2],
                        "updateTime": ident[3],
                    },
                    "payloads": sorted(fps),
                    "sources": sorted({h["source"] for h in hits}),
                    "kinds": sorted(kinds),
                }
            )
            continue
        keeper = min(hits, key=lambda h: (h["priority"], h["source"]))
        if ident in live_map:
            already += 1
            proposed_by_ident[ident] = live_map[ident]
        else:
            proposed_by_ident[ident] = keeper["obs"]
            new_obs.append(keeper["obs"])
        exact_dupes += max(0, len(hits) - 1)

    # Live rows never seen in this scan remain (existing canonical preserved).
    for ident, obs in live_map.items():
        if ident in conflict_idents:
            continue
        if ident not in grouped:
            already += 1
            proposed_by_ident[ident] = obs

    proposed = ceh.sort_canonical_rows(list(proposed_by_ident.values()))
    new_obs = ceh.sort_canonical_rows(new_obs)

    payloads: dict[str, str] = {}
    by_month: dict[str, list[dict]] = defaultdict(list)
    for obs in proposed:
        mk = ceh.month_key_from_date(obs["date"])
        if mk:
            by_month[mk].append(obs)
    for mk, month_rows in by_month.items():
        text = ceh.format_jsonl(ceh.sort_canonical_rows(month_rows))
        path = ceh.month_file_path(root, mk)
        current_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current_text != text:
            payloads[mk] = text

    earliest, latest = _date_range(proposed)
    cand_earliest, cand_latest = _date_range([h["obs"] for hits in grouped.values() for h in hits])

    ok = (len(conflicts) == 0) and (len(blocking) == 0)
    plan = {
        "ok": ok,
        "sourceOrder": source_order,
        "currentRunId": current_run_id(root),
        "sources": source_reports,
        "counts": {
            "sources": len(source_reports),
            "raw": raw_total,
            "validCandidates": valid_total,
            "alreadyCanonical": already,
            "exactDupesSkipped": exact_dupes,
            "new": len(new_obs),
            "conflicts": len(conflicts),
            "rejected": rejected_total,
            "missingUpdateTimeFallback": fallback_total,
            "blockingValidation": len(blocking),
            "earliest": earliest,
            "latest": latest,
            "candidateEarliest": cand_earliest,
            "candidateLatest": cand_latest,
            "perTicker": _per_ticker(proposed),
            "perTickerNew": _per_ticker(new_obs),
            "proposedRows": len(proposed),
        },
        "conflicts": conflicts,
        "blockingValidation": blocking,
        "rejectedReasons": dict(sorted(rejected_reasons.items())),
        "payloadMonths": sorted(payloads.keys()),
        "proposedIdentities": [
            {
                "ticker": r["ticker"],
                "reportedFiscalPeriodEnding": r["reportedFiscalPeriodEnding"],
                "date": r["date"],
                "updateTime": r["updateTime"],
                "consensus": r["consensus"],
                "analysts": r.get("analysts"),
            }
            for r in proposed
        ],
        "_payloads": payloads,
        "_proposed": proposed,
        "_new": new_obs,
        "limitations": _global_limitations(source_reports),
    }
    return plan


def _global_limitations(source_reports: list[dict]) -> list[str]:
    notes = [
        "Admission is PR #12 canonical_observation_from_row plus a mappedYear/slot identity guard.",
        "reportedFiscalLabel (Mon YYYY) maps to canonical reportedFiscalPeriodEnding via "
        "normalize_fiscal_period_label / fiscal_identity_key — the same PR #10/#12 identity. "
        "A calendar date such as 2027-01-31 is never invented from a month label.",
        "seed_from_revision_history rows are never imported (not quality-gate observations).",
        "Orphan / aborted generations are not imported.",
        "Partial history is reported honestly; missing periods are not synthesized.",
        "Public derived exports are used only when exact canonical fields are reconstructable.",
        "Migration never mutates data/CURRENT.json.",
    ]
    for s in source_reports:
        for lim in s.get("limitations") or []:
            notes.append(f"{s['name']}: {lim}")
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in notes:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def public_plan(plan: dict) -> dict:
    """JSON-serializable plan without private object fields."""
    skip = {"_payloads", "_proposed", "_new"}
    return {k: v for k, v in plan.items() if k not in skip}


def format_human_report(plan: dict, *, mode: str) -> str:
    c = plan.get("counts") or {}
    lines = [
        f"EPS history migration {mode}",
        f"  ok={plan.get('ok')} currentRunId={plan.get('currentRunId')}",
        f"  sources={c.get('sources')} raw={c.get('raw')} validCandidates={c.get('validCandidates')}",
        f"  alreadyCanonical={c.get('alreadyCanonical')} exactDupesSkipped={c.get('exactDupesSkipped')} "
        f"new={c.get('new')}",
        f"  conflicts={c.get('conflicts')} rejected={c.get('rejected')} "
        f"missingUpdateTimeFallback={c.get('missingUpdateTimeFallback')} "
        f"blockingValidation={c.get('blockingValidation')}",
        f"  earliest={c.get('earliest')} latest={c.get('latest')} proposedRows={c.get('proposedRows')}",
        f"  perTicker={c.get('perTicker')}",
        f"  perTickerNew={c.get('perTickerNew')}",
        f"  payloadMonths={plan.get('payloadMonths')}",
    ]
    lines.append("  sources:")
    for s in plan.get("sources") or []:
        lines.append(
            f"    - {s.get('name')} exists={s.get('exists')} trusted={s.get('trusted')} "
            f"raw={s.get('raw')} valid={s.get('validCandidates')} "
            f"uniq={s.get('uniqueIdentities')} dupes={s.get('duplicateIdentities')} "
            f"conflicts={s.get('conflictingIdentities')} rejected={s.get('rejectedCount')} "
            f"missingUT={s.get('missingUpdateTimeCount')} "
            f"invalidFiscal={s.get('invalidFiscalIdentityCount')} "
            f"invalidConsensus={s.get('invalidConsensusCount')} "
            f"range={s.get('earliest')}..{s.get('latest')} "
            f"trust={s.get('trustReason')}"
        )
        if s.get("rejectedReasons"):
            lines.append(f"      rejectedReasons={s.get('rejectedReasons')}")
        if s.get("limitations"):
            lines.append(f"      limitations={s.get('limitations')}")
    if plan.get("conflicts"):
        lines.append("  CONFLICTS (apply fail-closed; no auto-winner):")
        for conf in plan["conflicts"]:
            lines.append(f"    - {conf}")
    if plan.get("blockingValidation"):
        lines.append("  BLOCKING VALIDATION:")
        for b in plan["blockingValidation"]:
            lines.append(f"    - {b}")
    if plan.get("rejectedReasons"):
        lines.append(f"  rejectedReasons={plan.get('rejectedReasons')}")
    lines.append("  limitations:")
    for n in plan.get("limitations") or []:
        lines.append(f"    - {n}")
    return "\n".join(lines) + "\n"


@contextmanager
def pipeline_lock(root: Path, *, acquire: bool = True):
    """Hold ``data/.pipeline.lock`` unless ``PIPELINE_LOCK_HELD=1`` (nested ingest).

    ``--apply`` must acquire before the plan is built and hold through write.
    """
    if not acquire or os.environ.get("PIPELINE_LOCK_HELD") == "1":
        yield None
        return
    lock = GlobalPipelineLock(default_pipeline_lock_path(Path(root)), non_blocking=True)
    lock.__enter__()
    prev = os.environ.get("PIPELINE_LOCK_HELD")
    os.environ["PIPELINE_LOCK_HELD"] = "1"
    try:
        yield lock
    finally:
        if prev is None:
            os.environ.pop("PIPELINE_LOCK_HELD", None)
        else:
            os.environ["PIPELINE_LOCK_HELD"] = prev
        lock.__exit__(None, None, None)


def _durable_copyfile(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(dest, src.read_bytes())


def _maybe_fault_live_replace(index_1based: int) -> None:
    raw = os.environ.get(FAULT_INJECT_REPLACE_AFTER)
    if raw in (None, ""):
        return
    try:
        n = int(raw)
    except ValueError:
        return
    if index_1based == n:
        raise OSError(f"{FAULT_INJECT_REPLACE_AFTER}={n}")


def _restore_month_files(hist: Path, backup: Path, existed: dict[str, bool], month_keys: list[str]) -> None:
    """Restore every affected month from the pre-replace backup. Newly created months are removed."""
    for mk in month_keys:
        dest = hist / f"{mk}.jsonl"
        bak = backup / f"{mk}.jsonl"
        if existed.get(mk):
            if not bak.is_file():
                raise MigrationError(f"rollback missing backup for {mk}.jsonl")
            _durable_copyfile(bak, dest)
        elif dest.exists():
            dest.unlink()


def apply_migration(
    root: Path,
    plan: dict | None = None,
    *,
    source_order: str = "priority",
    acquire_lock: bool = True,
) -> dict:
    """Write proposed canonical files with all-or-nothing rollback.

    Acquires the global pipeline lock (unless already held) before planning
    when ``plan`` is omitted, and holds it through validation + write.
    Does not mutate CURRENT, runtime daily.jsonl, or generations.
    """
    root = Path(root)
    try:
        with pipeline_lock(root, acquire=acquire_lock):
            return _apply_migration_locked(root, plan, source_order=source_order)
    except PipelineBusy:
        return {
            "ok": False,
            "mode": "apply",
            "applied": False,
            "written": [],
            "error": RUN_IN_PROGRESS_MSG,
            "busy": True,
        }


def _apply_migration_locked(
    root: Path,
    plan: dict | None,
    *,
    source_order: str,
) -> dict:
    if plan is None:
        plan = plan_migration(root, source_order=source_order)
    result = public_plan(plan)
    result["mode"] = "apply"
    result["written"] = []
    result["applied"] = False
    result["rolledBack"] = False
    if plan.get("conflicts"):
        result["ok"] = False
        result["error"] = "conflicts>0: apply fail-closed, no mutation"
        return result
    if plan.get("blockingValidation"):
        result["ok"] = False
        result["error"] = "blocking validation: apply fail-closed, no mutation"
        return result

    payloads: dict[str, str] = dict(plan.get("_payloads") or {})
    if not payloads:
        result["ok"] = True
        result["applied"] = True
        result["noop"] = True
        return result

    hist = ceh.history_dir(root)
    hist.mkdir(parents=True, exist_ok=True)
    staging = hist / STAGING_DIRNAME
    backup = hist / BACKUP_DIRNAME
    for d in (staging, backup):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    month_keys = sorted(payloads)
    existed: dict[str, bool] = {}
    written: list[str] = []
    try:
        for mk in month_keys:
            atomic_write_text(staging / f"{mk}.jsonl", payloads[mk])
        # Snapshot originals BEFORE any live replace so a mid-loop failure can
        # restore every affected month (including deleting newly created files).
        for mk in month_keys:
            dest = hist / f"{mk}.jsonl"
            existed[mk] = dest.is_file()
            if dest.is_file():
                _durable_copyfile(dest, backup / f"{mk}.jsonl")
        for i, mk in enumerate(month_keys, start=1):
            _maybe_fault_live_replace(i)
            src = staging / f"{mk}.jsonl"
            dest = hist / f"{mk}.jsonl"
            os.replace(str(src), str(dest))
            written.append(dest.name)
    except Exception as exc:
        _restore_month_files(hist, backup, existed, month_keys)
        result["ok"] = False
        result["applied"] = False
        result["rolledBack"] = True
        result["error"] = f"apply failed; canonical months rolled back: {exc}"
        result["written"] = []
        return result
    finally:
        for d in (staging, backup):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    result["ok"] = True
    result["applied"] = True
    result["noop"] = False
    result["written"] = written
    return result


def _write_report_file(path: Path | None, plan_public: dict, human: str) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        atomic_write_text(path, json.dumps(plan_public, indent=2, ensure_ascii=False) + "\n")
    else:
        atomic_write_text(path, human + "\n---JSON---\n" + json.dumps(plan_public, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate recoverable EPS observations into Git canonical history")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true", help="Read-only audit of all historical sources")
    mode.add_argument("--apply", action="store_true", help="Atomically write canonical history (fail-closed on conflicts)")
    ap.add_argument("--root", type=Path, default=None, help="Project root (default: repo root)")
    ap.add_argument(
        "--source-order",
        choices=("priority", "reverse"),
        default="priority",
        help="Scan order only; proposed identities must not change",
    )
    ap.add_argument("--report", type=Path, default=None, help="Write machine-readable JSON (or mixed) report")
    ap.add_argument("--json", action="store_true", help="Print JSON plan/result to stdout")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else ROOT

    before = fingerprint_workspace(root) if args.audit else None
    current_before = None
    cur_path = root / CURRENT_REL
    if cur_path.is_file():
        current_before = cur_path.read_bytes()

    # Lock before the plan is built; hold through validation + apply/audit.
    try:
        with pipeline_lock(root, acquire=True):
            plan = plan_migration(root, source_order=args.source_order)
            if args.apply:
                result = apply_migration(
                    root, plan, source_order=args.source_order, acquire_lock=False
                )
                public = result
                human = format_human_report(plan, mode="apply")
                if result.get("error"):
                    human += f"ERROR: {result['error']}\n"
                if current_before is not None and cur_path.is_file() and cur_path.read_bytes() != current_before:
                    print("ERROR: CURRENT.json mutated during apply (forbidden)", file=sys.stderr)
                    return 1
                _write_report_file(args.report, public, human)
                if args.json:
                    print(json.dumps(public, indent=2, ensure_ascii=False))
                else:
                    print(human, end="")
                    print("---JSON---")
                    print(json.dumps(public, indent=2, ensure_ascii=False))
                return 0 if result.get("ok") else 1

            public = public_plan(plan)
            public["mode"] = "audit"
            human = format_human_report(plan, mode="audit")
            after = fingerprint_workspace(root)
            if before != after:
                print("ERROR: --audit mutated workspace files (forbidden)", file=sys.stderr)
                return 1
            if current_before is not None and cur_path.is_file() and cur_path.read_bytes() != current_before:
                print("ERROR: CURRENT.json mutated during audit (forbidden)", file=sys.stderr)
                return 1
            _write_report_file(args.report, public, human)
            if args.json:
                print(json.dumps(public, indent=2, ensure_ascii=False))
            else:
                print(human, end="")
                print("---JSON---")
                print(json.dumps(public, indent=2, ensure_ascii=False))
            return 0
    except PipelineBusy:
        print(RUN_IN_PROGRESS_MSG, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())

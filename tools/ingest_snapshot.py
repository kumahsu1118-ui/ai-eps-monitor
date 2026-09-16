#!/usr/bin/env python3
"""Single E2E ingest entrypoint (deterministic, transactional).

Fixed flow:
  Incoming raw → Quality Gate (per-ticker LKG)
  → STAGE under data/staging/<runId>/ (runStatus=staged)
  → revision/daily/alerts/web build
  → atomic COMMIT to validated persistent history (runStatus=committed)
    including canonical data/history/eps_daily month files in the generation
  → (optional) source-git persist of canonical history (no CURRENT rollback on push fail)
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from atomic_io import append_jsonl_atomic, atomic_write_json  # noqa: E402
import canonical_eps_history as ceh  # noqa: E402
import snapshot_quality as sq  # noqa: E402

INCOMING_DIR = ROOT / "data" / "incoming"
SNAP_DIR = ROOT / "data" / "snapshots"
QUARANTINE_DIR = SNAP_DIR / "quarantine"
STAGING_DIR = ROOT / "data" / "staging"
GENERATIONS_DIR = ROOT / "data" / "generations"
CURRENT_POINTER = ROOT / "data" / "CURRENT.json"
DAILY_DIR = ROOT / "data" / "daily_eps_snapshots"
HISTORY_DIR = ROOT / "data" / "history" / "eps_daily"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
COMPARISON_CHECKPOINT_PATH = ROOT / "data" / "comparison_checkpoint.json"
WEB_DATA = ROOT / "web" / "data"
REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
UNIVERSE_PATH = ROOT / "data" / "universe.json"
CANONICAL_GIT_STATE_NAME = "canonical_git_state.json"



def rebind_paths(root: Path) -> None:
    """Point module-level paths at an alternate project root (acceptance fixtures)."""
    global ROOT, INCOMING_DIR, SNAP_DIR, QUARANTINE_DIR, STAGING_DIR
    global GENERATIONS_DIR, CURRENT_POINTER, DAILY_DIR, HISTORY_DIR, ALERTS_PATH
    global COMPARISON_CHECKPOINT_PATH, WEB_DATA, REV_PATH, UNIVERSE_PATH, _here
    ROOT = Path(root)
    _here = ROOT / "tools"
    INCOMING_DIR = ROOT / "data" / "incoming"
    SNAP_DIR = ROOT / "data" / "snapshots"
    QUARANTINE_DIR = SNAP_DIR / "quarantine"
    STAGING_DIR = ROOT / "data" / "staging"
    GENERATIONS_DIR = ROOT / "data" / "generations"
    CURRENT_POINTER = ROOT / "data" / "CURRENT.json"
    DAILY_DIR = ROOT / "data" / "daily_eps_snapshots"
    HISTORY_DIR = ROOT / "data" / "history" / "eps_daily"
    ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
    COMPARISON_CHECKPOINT_PATH = ROOT / "data" / "comparison_checkpoint.json"
    WEB_DATA = ROOT / "web" / "data"
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


def new_run_id(snap: dict | None = None) -> str:
    """Unconditionally unique run identity.

    Never second-resolution wall-clock + short hash: identical same-second
    replays must not reuse stage/generation paths. Combines UTC microseconds,
    the full snapshot content hash, and a UUID4.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = content_hash_snapshot(snap) if isinstance(snap, dict) else hashlib.sha256(b"").hexdigest()
    return f"{ts}_{digest}_{uuid.uuid4().hex}"


def allocate_unique_run_id(snap: dict | None = None) -> str:
    """Allocate a runId that does not collide with staging or generations."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        rid = new_run_id(snap)
        if (STAGING_DIR / rid).exists() or (GENERATIONS_DIR / rid).exists():
            continue
        return rid
    raise RuntimeError("unable to allocate unique runId")


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
        if isinstance(prior_eps, dict):
            for slot, row in prior_eps.items():
                if not isinstance(row, dict):
                    continue
                fiscal = _fiscal_of_row(row)
                cons = _consensus_of_row(row)
                if fiscal:
                    prior_by_fiscal[fiscal] = (cons, row, slot)
                # Mapped slot is UI display only — NEVER a revision identity fallback

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
            # Identity: ticker + Reported Fiscal Period Ending ONLY
            prev_tuple = prior_by_fiscal.get(fiscal)
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


def _backup_path(path: Path) -> bytes | None:
    if path.exists():
        return path.read_bytes()
    return None


def _restore_backup(path: Path, data: bytes | None) -> None:
    if data is None:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def list_index_local_assets(index_html: Path) -> list[str]:
    """Parse index.html for local relative src=/href= assets (no http/https/data/)."""
    if not index_html.exists():
        return []
    body = index_html.read_text(encoding="utf-8")
    refs = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", body, flags=re.I)
    out = []
    for ref in refs:
        ref = ref.strip()
        if not ref or ref.startswith(("#", "data:", "mailto:")):
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref):
            continue  # absolute URI
        # strip query/hash for filesystem path
        path_part = ref.split("?", 1)[0].split("#", 1)[0]
        if path_part.startswith("./"):
            path_part = path_part[2:]
        if path_part.startswith("/"):
            path_part = path_part.lstrip("/")
        if path_part and path_part not in out:
            out.append(path_part)
    return out


def iter_frontend_static_files(web_root: Path) -> list[Path]:
    """Full frontend static dependency tree for versioning/publish."""
    root = Path(web_root)
    files: list[Path] = []
    seen: set[str] = set()
    index = root / "index.html"
    # Always include shell + every local asset referenced by index
    candidates = ["index.html"] + list_index_local_assets(index)
    # Also include entire vendor/ tree (future-proof)
    vendor = root / "vendor"
    if vendor.is_dir():
        for p in sorted(vendor.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(root)).replace("\\", "/")
                if rel not in seen:
                    candidates.append(rel)
    for rel in candidates:
        rel_n = rel.replace("\\", "/")
        if rel_n in seen:
            continue
        seen.add(rel_n)
        fp = root / rel_n
        if fp.is_file():
            files.append(fp)
    return files


def _canonical_static_bytes(fp: Path) -> bytes:
    """Asset bytes for appVersion; strip cache-bust ?v= from index.html so stamp order is stable."""
    data = fp.read_bytes()
    if fp.name == "index.html":
        try:
            text = data.decode("utf-8")
        except Exception:
            return data
        # Strip ?v=<hash> (and lone ?v=) from local asset refs — identity is content, not bust token
        text = re.sub(r"\?v=[0-9a-fA-F]*", "", text)
        return text.encode("utf-8")
    return data


def compute_app_version(web_root: Path | None = None) -> str:
    """appVersion = hash of full frontend static deps (index + app + styles + vendor/**).

    index.html is canonicalized (strip ?v=) so stamp_index_cache_bust does not change identity.
    Guarantee: meta.appVersion == compute_app_version(final published asset tree).
    """
    root = web_root or (ROOT / "web")
    h = hashlib.sha256()
    for fp in iter_frontend_static_files(root):
        rel = str(fp.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(_canonical_static_bytes(fp))
        h.update(b"|")
    return h.hexdigest()


def stamp_index_cache_bust(web_root: Path | None = None) -> str:
    """Rewrite local src/href in index.html with ?v=<contenthash>; return appVersion."""
    root = Path(web_root) if web_root else (ROOT / "web")
    index = root / "index.html"
    if not index.exists():
        return compute_app_version(root)
    body = index.read_text(encoding="utf-8")

    def _hash_for(rel: str) -> str:
        fp = root / rel
        if not fp.is_file():
            return "missing"
        return hashlib.sha256(fp.read_bytes()).hexdigest()[:12]

    def repl(m: re.Match) -> str:
        attr, quote, ref = m.group(1), m.group(2), m.group(3)
        raw = ref.strip()
        if not raw or raw.startswith(("#", "data:", "mailto:")):
            return m.group(0)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw):
            return m.group(0)
        path_part = raw.split("?", 1)[0].split("#", 1)[0]
        rel = path_part[2:] if path_part.startswith("./") else path_part.lstrip("/")
        hv = _hash_for(rel)
        prefix = "./" if path_part.startswith("./") else ("" if not path_part.startswith("/") else "/")
        new_ref = f"{prefix}{rel}?v={hv}"
        return f"{attr}={quote}{new_ref}{quote}"

    stamped = re.sub(
        r"(src|href)=([\"'])([^\"']+)\2",
        repl,
        body,
        flags=re.I,
    )
    if stamped != body:
        index.write_text(stamped, encoding="utf-8")
    return compute_app_version(root)




SCHEMA_VERSION = "1"


def compute_release_version(
    app_version: str,
    schema_version: str,
    data_version: str,
    refresh_version: str,
) -> str:
    raw = f"{app_version}|{schema_version}|{data_version}|{refresh_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def finalize_release_identity(web_root: Path | None = None) -> dict:
    """Stamp cache-bust THEN recompute/write final appVersion + releaseVersion into meta.

    Call after any index stamp so meta.appVersion == compute_app_version(final tree)
    and meta.releaseVersion == hash(final appVersion + schema + data + refresh).
    """
    root = Path(web_root) if web_root else (ROOT / "web")
    stamp_index_cache_bust(root)
    app_v = compute_app_version(root)
    meta_path = root / "data" / "meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except Exception:
            meta = {}
    schema = str(meta.get("schemaVersion") or SCHEMA_VERSION)
    data_v = str(meta.get("dataVersion") or "")
    refresh_v = str(meta.get("refreshVersion") or "")
    rel_v = compute_release_version(app_v, schema, data_v, refresh_v)
    meta["appVersion"] = app_v
    meta["releaseVersion"] = rel_v
    meta["schemaVersion"] = schema
    atomic_write_json(meta_path, meta)
    dash_path = root / "data" / "dashboard.json"
    if dash_path.exists():
        try:
            dash = json.loads(dash_path.read_text(encoding="utf-8")) or {}
            if isinstance(dash, dict):
                dm = dict(dash.get("meta") or {})
                dm["appVersion"] = app_v
                dm["releaseVersion"] = rel_v
                dm["schemaVersion"] = schema
                dash["meta"] = dm
                atomic_write_json(dash_path, dash)
        except Exception:
            pass
    return meta


class CommitAborted(RuntimeError):
    """Raised when commit_staged_run rolls back after a persistent write failure."""


PUBLISH_STATE_NAME = "publish_state.json"


def publish_state_path() -> Path:
    return ROOT / "data" / PUBLISH_STATE_NAME


def read_publish_state() -> dict:
    path = publish_state_path()
    if not path.exists():
        return {
            "publishStatus": "published",
            "lastPublishAttempt": None,
            "lastSuccessfulPublish": None,
            "pendingReleaseVersion": None,
        }
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def write_publish_state(state: dict) -> None:
    atomic_write_json(publish_state_path(), state)


def canonical_git_state_path() -> Path:
    return ROOT / "data" / CANONICAL_GIT_STATE_NAME


def read_canonical_git_state() -> dict:
    path = canonical_git_state_path()
    if not path.exists():
        return {"status": "clean", "lastAttempt": None, "lastSuccessful": None, "error": None}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def write_canonical_git_state(state: dict) -> None:
    atomic_write_json(canonical_git_state_path(), state)


def persist_canonical_history_to_source_git(*, push: bool = True) -> dict:
    """Commit/push data/history/eps_daily/ only. Never rolls back financial COMMIT.

    Source-repo git persist is separate from Pages publish (site-repo public assets).
    Failure → status pending/failed for retry; observation already in canonical files.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = read_canonical_git_state()
    state["lastAttempt"] = now
    if os.environ.get("SKIP_CANONICAL_GIT_PERSIST") == "1":
        return {"status": "skipped", "lastAttempt": now}
    git_dir = ROOT / ".git"
    if not git_dir.exists():
        state["status"] = "skipped"
        state["error"] = "not_a_git_repo"
        write_canonical_git_state(state)
        return state
    hist = HISTORY_DIR
    hist.mkdir(parents=True, exist_ok=True)
    rel = "data/history/eps_daily"
    try:
        add = subprocess.run(
            ["git", "add", "--", rel],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if add.returncode != 0:
            raise RuntimeError(add.stderr or add.stdout or f"git add rc={add.returncode}")
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", rel],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode == 0:
            state["status"] = "clean"
            state["error"] = None
            write_canonical_git_state(state)
            return state
        msg = f"canonical eps history {now}"
        commit = subprocess.run(
            [
                "git",
                "-c",
                "user.email=kumahsu1118-ui@users.noreply.github.com",
                "-c",
                "user.name=AI EPS Monitor",
                "commit",
                "-m",
                msg,
                "--",
                rel,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr or commit.stdout or f"git commit rc={commit.returncode}")
        if push and os.environ.get("SKIP_CANONICAL_GIT_PUSH") != "1":
            push_p = subprocess.run(
                ["git", "push"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            if push_p.returncode != 0:
                state["status"] = "pending"
                state["error"] = (push_p.stderr or push_p.stdout or f"git push rc={push_p.returncode}")[-500:]
                write_canonical_git_state(state)
                print(
                    "WARNING: canonical source-git push failed — financial commit intact; "
                    "retry will not duplicate observations",
                    file=sys.stderr,
                )
                return state
        state["status"] = "ok"
        state["lastSuccessful"] = now
        state["error"] = None
        write_canonical_git_state(state)
        return state
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        try:
            write_canonical_git_state(state)
        except Exception:
            pass
        print(
            f"WARNING: canonical source-git persist failed — financial commit intact ({exc})",
            file=sys.stderr,
        )
        return state


def retry_pending_canonical_git_if_needed() -> dict | None:
    """Retry source-git persist of canonical history after a prior push/commit failure."""
    state = read_canonical_git_state()
    if str(state.get("status") or "") not in {"pending", "failed"}:
        return None
    print(f"pending canonical source-git retry status={state.get('status')}")
    return persist_canonical_history_to_source_git()


def read_current_pointer() -> dict | None:
    if not CURRENT_POINTER.exists():
        return None
    try:
        obj = json.loads(CURRENT_POINTER.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def resolve_current_generation() -> Path | None:
    cur = read_current_pointer()
    if not cur:
        return None
    g = Path(str(cur.get("generation") or ""))
    if g.is_dir():
        return g
    rid = cur.get("runId")
    if rid:
        g2 = GENERATIONS_DIR / str(rid)
        if g2.is_dir():
            return g2
    return None


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    atomic_write_bytes = __import__("atomic_io", fromlist=["atomic_write_bytes"]).atomic_write_bytes
    atomic_write_bytes(dest, data)


def materialize_generation(gen_dir: Path) -> None:
    """Idempotent sync of a committed generation package → live paths.

    Safe to re-run after crash mid-materialize. Live paths are a cache of CURRENT.
    """
    import shutil

    gen_dir = Path(gen_dir)
    # Validated snapshot
    snap_src_dir = gen_dir / "snapshots"
    if snap_src_dir.is_dir():
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        for src in snap_src_dir.glob("*.json"):
            _copy_file(src, SNAP_DIR / src.name)
    # Revisions (full file)
    rev_src = gen_dir / "revisions" / "history.jsonl"
    if rev_src.exists():
        REV_PATH.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(rev_src, REV_PATH)
    # Daily (runtime cache — still copied from generation so un-backfilled
    # workspace history is not wiped by empty canonical; PR #13 backfill)
    daily_src = gen_dir / "daily_eps_snapshots"
    if daily_src.is_dir():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        for src in daily_src.rglob("*"):
            if src.is_file():
                rel = src.relative_to(daily_src)
                _copy_file(src, DAILY_DIR / rel)
    # Canonical Git-tracked EPS history (only months this generation updated)
    hist_src = gen_dir / "history" / "eps_daily"
    if hist_src.is_dir():
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        for src in hist_src.glob("*.jsonl"):
            if src.is_file():
                _copy_file(src, HISTORY_DIR / src.name)
    # Alerts
    alerts_src = gen_dir / "alerts" / "index.json"
    if alerts_src.exists():
        ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(alerts_src, ALERTS_PATH)
    # Checkpoint
    cp_src = gen_dir / "comparison_checkpoint.json"
    if cp_src.exists():
        _copy_file(cp_src, COMPARISON_CHECKPOINT_PATH)
    # Web data
    web_src = gen_dir / "web" / "data"
    if web_src.is_dir():
        WEB_DATA.mkdir(parents=True, exist_ok=True)
        for src in web_src.glob("*.json"):
            _copy_file(src, WEB_DATA / src.name)
    # Marker so ensure_live_matches_current can skip
    cur = read_current_pointer() or {}
    marker = ROOT / "data" / ".materialized_run_id"
    atomic_write_text = __import__("atomic_io", fromlist=["atomic_write_text"]).atomic_write_text
    atomic_write_text(marker, str(cur.get("runId") or gen_dir.name) + "\n")


def _required_live_artifact_pairs(gen_dir: Path) -> list[tuple[Path, Path]]:
    """Generation files that must exist in the live cache after materialize."""
    gen_dir = Path(gen_dir)
    pairs: list[tuple[Path, Path]] = []
    snap_src_dir = gen_dir / "snapshots"
    if snap_src_dir.is_dir():
        for src in sorted(snap_src_dir.glob("*.json")):
            pairs.append((src, SNAP_DIR / src.name))
    rev_src = gen_dir / "revisions" / "history.jsonl"
    if rev_src.exists():
        pairs.append((rev_src, REV_PATH))
    daily_src = gen_dir / "daily_eps_snapshots"
    if daily_src.is_dir():
        for src in sorted(p for p in daily_src.rglob("*") if p.is_file()):
            pairs.append((src, DAILY_DIR / src.relative_to(daily_src)))
    hist_src = gen_dir / "history" / "eps_daily"
    if hist_src.is_dir():
        for src in sorted(p for p in hist_src.glob("*.jsonl") if p.is_file()):
            pairs.append((src, HISTORY_DIR / src.name))
    alerts_src = gen_dir / "alerts" / "index.json"
    if alerts_src.exists():
        pairs.append((alerts_src, ALERTS_PATH))
    cp_src = gen_dir / "comparison_checkpoint.json"
    if cp_src.exists():
        pairs.append((cp_src, COMPARISON_CHECKPOINT_PATH))
    web_src = gen_dir / "web" / "data"
    if web_src.is_dir():
        for src in sorted(web_src.glob("*.json")):
            pairs.append((src, WEB_DATA / src.name))
    return pairs


def live_cache_matches_generation(gen_dir: Path, run_id: str) -> bool:
    """True only if marker AND required CURRENT artifacts exist and match identity."""
    rid = str(run_id or "").strip()
    if not rid:
        return False
    marker = ROOT / "data" / ".materialized_run_id"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != rid:
        return False
    pairs = _required_live_artifact_pairs(gen_dir)
    if not pairs:
        # Generation with no copyable artifacts: marker match is insufficient if
        # CURRENT web/data exists in gen after a successful export; otherwise OK.
        return True
    for src, dest in pairs:
        if not dest.exists():
            return False
        try:
            if dest.read_bytes() != src.read_bytes():
                return False
        except OSError:
            return False
    return True


def ensure_live_matches_current() -> None:
    """Rematerialize unless marker AND required CURRENT artifacts match identity."""
    cur = read_current_pointer()
    if not cur:
        return
    gen = resolve_current_generation()
    if not gen:
        return
    rid = str(cur.get("runId") or gen.name)
    if live_cache_matches_generation(gen, rid):
        return
    materialize_generation(gen)


def _build_revision_history_bytes(events: list[dict]) -> tuple[bytes, int]:
    """Return full history.jsonl bytes after appending events (deduped) + append count."""
    existing_rows = load_revision_history(REV_PATH)
    existing_ids = known_event_ids(existing_rows)
    to_write: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("eventId") or revision_event_id(ev)
        ev = dict(ev)
        ev["eventId"] = eid
        if eid in existing_ids:
            continue
        existing_ids.add(eid)
        to_write.append(ev)
    if REV_PATH.exists():
        buf = REV_PATH.read_text(encoding="utf-8")
        if buf and not buf.endswith("\n"):
            buf += "\n"
    else:
        buf = ""
    for row in to_write:
        buf += json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
    return buf.encode("utf-8"), len(to_write)


def commit_staged_run(
    stage_dir: Path,
    snap_norm: dict,
    events: list[dict],
    snap_utc: str,
    *,
    run_id: str | None = None,
) -> Path:
    """Crash-safe COMMIT: build generations/<runId>/ fully, then CURRENT.json sole commit.

    Live paths are materialized ONLY after CURRENT flips. SIGKILL before CURRENT leaves
    previous generation as the only reader-visible committed state.
    On exception before CURRENT: no live mutation; runStatus=aborted.
    After CURRENT successfully flipped: financial commit DONE — materialization failure
    yields runStatus=committed + materializationStatus=failed (never aborted; no CURRENT rollback).
    """
    import shutil
    from atomic_io import atomic_write_bytes, atomic_write_text

    rid = run_id or stage_dir.name
    gen_dir = GENERATIONS_DIR / rid
    validated_path: Path | None = None
    n_rev = 0
    current_flipped = False

    try:
        # Existing generation directories are immutable — never overwrite-in-place.
        if gen_dir.exists():
            raise CommitAborted(f"generation directory already exists (immutable): {rid}")
        gen_dir.mkdir(parents=True, exist_ok=False)
        # --- Full generation package (append-only; safe if CURRENT never flips) ---
        atomic_write_json(gen_dir / "snapshot.json", snap_norm)
        atomic_write_json(gen_dir / "proposed_revisions.json", {"events": events})

        stage_web = stage_dir / "web"
        if stage_web.exists():
            dest_web = gen_dir / "web"
            if dest_web.exists():
                shutil.rmtree(dest_web)
            shutil.copytree(stage_web, dest_web)

        pending_daily = stage_dir / "pending_daily_rows.json"
        pending_alerts = stage_dir / "pending_alerts.json"
        pending_checkpoint = stage_dir / "pending_comparison_checkpoint.json"
        pending_day_summary = stage_dir / "pending_day_summary.json"

        # Choose immutable validated path name (check live + gen collisions)
        validated_path = immutable_validated_path(snap_norm, snap_utc)
        gen_snap_dir = gen_dir / "snapshots"
        gen_snap_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(gen_snap_dir / validated_path.name, snap_norm)
        atomic_write_json(gen_snap_dir / "latest.json", snap_norm)

        # Manifest for generation (and later live)
        if os.environ.get("FAULT_INJECT_COMMIT_MANIFEST_FAIL") == "1":
            raise CommitAborted("FAULT_INJECT_COMMIT_MANIFEST_FAIL")
        files = []
        try:
            files = [p.name for p in sq.list_validated_snapshots(SNAP_DIR)]
        except Exception:
            files = sorted(
                p.name
                for p in SNAP_DIR.glob("20*.json")
                if p.name not in {"latest.json", "manifest.json"} and not p.name.startswith("raw_")
            )
        if validated_path.name not in files:
            files.append(validated_path.name)
            files = sorted(set(files))
        man_obj = {
            "latest": validated_path.name,
            "snapshot_utc": snap_utc,
            "files": files,
            "immutable": True,
        }
        atomic_write_json(gen_snap_dir / "manifest.json", man_obj)

        # Revisions — full file in generation
        if os.environ.get("FAULT_INJECT_COMMIT_REVISION_FAIL") == "1":
            raise CommitAborted("FAULT_INJECT_COMMIT_REVISION_FAIL")
        rev_bytes, n_rev = _build_revision_history_bytes(events)
        (gen_dir / "revisions").mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(gen_dir / "revisions" / "history.jsonl", rev_bytes)

        # Daily — start from live, append pending rows into generation copy
        daily_gen = gen_dir / "daily_eps_snapshots"
        daily_gen.mkdir(parents=True, exist_ok=True)
        live_jsonl = DAILY_DIR / "daily.jsonl"
        daily_buf = live_jsonl.read_text(encoding="utf-8") if live_jsonl.exists() else ""
        if daily_buf and not daily_buf.endswith("\n"):
            daily_buf += "\n"
        if pending_daily.exists():
            try:
                payload = json.loads(pending_daily.read_text(encoding="utf-8"))
                rows = payload.get("rows") or []
            except Exception:
                rows = []
            for row in rows:
                daily_buf += json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        atomic_write_text(daily_gen / "daily.jsonl", daily_buf)
        if pending_day_summary.exists():
            try:
                day = json.loads(pending_day_summary.read_text(encoding="utf-8"))
                day_name = day.get("date") or str(snap_utc)[:10]
                atomic_write_json(daily_gen / f"{day_name}.json", day)
            except Exception as day_exc:
                raise CommitAborted(f"day_summary:{day_exc}") from day_exc
        # Preserve other day JSON files from live
        if DAILY_DIR.exists():
            for src in DAILY_DIR.glob("*.json"):
                dest = daily_gen / src.name
                if not dest.exists():
                    _copy_file(src, dest)

        # Canonical Git-tracked history: only months that gain new observations.
        # Written into the generation package BEFORE CURRENT so a crash leaves
        # live data/history/eps_daily/ unchanged. Not a full-history copy.
        pending_rows: list[dict] = []
        if pending_daily.exists():
            try:
                pending_payload = json.loads(pending_daily.read_text(encoding="utf-8"))
                pending_rows = pending_payload.get("rows") or []
            except Exception:
                pending_rows = []
        month_payloads = ceh.build_generation_month_payloads(ROOT, pending_rows)
        if month_payloads:
            ceh.write_month_payloads(gen_dir / "history" / "eps_daily", month_payloads)

        # Alerts
        if pending_alerts.exists():
            try:
                alerts_obj = json.loads(pending_alerts.read_text(encoding="utf-8"))
            except Exception as aexc:
                raise CommitAborted(f"alerts_payload:{aexc}") from aexc
            (gen_dir / "alerts").mkdir(parents=True, exist_ok=True)
            atomic_write_json(gen_dir / "alerts" / "index.json", alerts_obj)
        elif ALERTS_PATH.exists():
            (gen_dir / "alerts").mkdir(parents=True, exist_ok=True)
            _copy_file(ALERTS_PATH, gen_dir / "alerts" / "index.json")

        # Checkpoint
        if pending_checkpoint.exists():
            try:
                cp = json.loads(pending_checkpoint.read_text(encoding="utf-8"))
            except Exception as cexc:
                raise CommitAborted(f"checkpoint:{cexc}") from cexc
            atomic_write_json(gen_dir / "comparison_checkpoint.json", cp)
        elif COMPARISON_CHECKPOINT_PATH.exists():
            _copy_file(COMPARISON_CHECKPOINT_PATH, gen_dir / "comparison_checkpoint.json")

        # Web data already copied from stage_web; if missing, copy live
        if not (gen_dir / "web" / "data").exists() and WEB_DATA.exists():
            shutil.copytree(WEB_DATA, gen_dir / "web" / "data")

        atomic_write_json(
            gen_dir / "generation_meta.json",
            {
                "runId": rid,
                "snapshot_utc": snap_utc,
                "validatedName": validated_path.name,
                "revisionEventsAppended": n_rev,
                "schemaVersion": SCHEMA_VERSION,
            },
        )

        # Fault after generation package complete but before CURRENT pointer
        if os.environ.get("FAULT_INJECT_COMMIT_FAIL") == "1":
            raise CommitAborted("FAULT_INJECT_COMMIT_FAIL")
        if os.environ.get("FAULT_INJECT_SIGKILL_BEFORE_CURRENT") == "1":
            os.kill(os.getpid(), 9)  # SIGKILL — no rollback, CURRENT unchanged

        # Sole commit point: atomic CURRENT.json pointer
        current_payload = {
            "runId": rid,
            "generation": str(gen_dir),
            "validatedPath": str(validated_path),
            "validatedName": validated_path.name,
            "snapshot_utc": snap_utc,
            "committedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schemaVersion": SCHEMA_VERSION,
            "crashAtomic": True,
        }
        atomic_write_json(CURRENT_POINTER, current_payload)
        current_flipped = True

        # Materialize live cache from committed generation.
        # Post-CURRENT failure: financial commit is DONE — never mark aborted / never rollback CURRENT.
        mat_status = "success"
        mat_err = None
        try:
            if os.environ.get("FAULT_INJECT_MATERIALIZE_FAIL") == "1":
                raise OSError("FAULT_INJECT_MATERIALIZE_FAIL")
            materialize_generation(gen_dir)
        except Exception as mex:
            mat_status = "failed"
            mat_err = f"{type(mex).__name__}: {mex}"
            print(
                f"WARNING: post-CURRENT materialization failed — runStatus=committed "
                f"materializationStatus=failed ({mat_err})",
                file=sys.stderr,
            )
        # Source-git persist of canonical files is after financial COMMIT.
        # Push failure must not rollback CURRENT; retry is idempotent.
        try:
            if mat_status == "success":
                persist_canonical_history_to_source_git()
            else:
                st = read_canonical_git_state()
                st["status"] = "pending"
                st["error"] = "materialize_failed"
                st["lastAttempt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                write_canonical_git_state(st)
        except Exception as gexc:
            print(
                f"WARNING: canonical source-git persist error — financial commit intact ({gexc})",
                file=sys.stderr,
            )

        _write_run_status(
            stage_dir,
            "committed",
            {
                "validatedPath": str(validated_path),
                "revisionEventsAppended": n_rev,
                "generation": str(gen_dir),
                "committedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "materializationStatus": mat_status,
                "materializationError": mat_err,
            },
        )
        # Persist materialization status on generation meta
        try:
            gmeta_path = gen_dir / "generation_meta.json"
            gmeta = {}
            if gmeta_path.exists():
                gmeta = json.loads(gmeta_path.read_text(encoding="utf-8")) or {}
            gmeta["materializationStatus"] = mat_status
            gmeta["materializationError"] = mat_err
            atomic_write_json(gmeta_path, gmeta)
        except Exception:
            pass
        return validated_path
    except CommitAborted:
        abort_staged_run(stage_dir, "commit_aborted_before_CURRENT")
        raise
    except Exception as exc:
        if type(exc).__name__ == "CommitAborted" or isinstance(exc, CommitAborted):
            abort_staged_run(stage_dir, f"commit_aborted:{exc}")
            raise
        # If CURRENT already flipped, never abort/rollback — treat as committed + mat failure
        if current_flipped:
            mat_err = f"{type(exc).__name__}: {exc}"
            _write_run_status(
                stage_dir,
                "committed",
                {
                    "validatedPath": str(validated_path) if validated_path else None,
                    "revisionEventsAppended": n_rev,
                    "generation": str(gen_dir),
                    "materializationStatus": "failed",
                    "materializationError": mat_err,
                },
            )
            print(
                f"WARNING: post-CURRENT error — committed, no abort ({mat_err})",
                file=sys.stderr,
            )
            return validated_path
        abort_staged_run(stage_dir, f"commit_error:{type(exc).__name__}:{exc}")
        print(f"ERROR: commit aborted before CURRENT — live state untouched ({exc})", file=sys.stderr)
        raise CommitAborted(str(exc)) from exc


def abort_staged_run(stage_dir: Path, reason: str) -> None:
    _write_run_status(stage_dir, "aborted", {"abortReason": reason})

def publish_prebuilt_site(*, release_version: str | None = None) -> int:
    """Publish already-exported web/ without recomputing EPS/history/alerts.

    Financial commit success + GitHub push fail must NOT rollback financial state.
    Persists publishStatus pending/published/failed for scheduler retry.
    Prefer CURRENT generation web/ as source of truth when available.
    """
    ensure_live_matches_current()
    gen = resolve_current_generation()
    if gen is not None:
        gen_web = gen / "web" / "data"
        if gen_web.is_dir():
            # Prefer publish from committed generation package web/
            try:
                materialize_generation(gen)
            except Exception as mex:
                print(f"WARNING: rematerialize before publish: {mex}", file=sys.stderr)
    # Stamp then finalize release identity so meta matches final asset tree
    finalize_release_identity(ROOT / "web")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path = WEB_DATA / "meta.json"
    rv = release_version
    if rv is None and meta_path.exists():
        try:
            rv = (json.loads(meta_path.read_text(encoding="utf-8")) or {}).get("releaseVersion")
        except Exception:
            rv = None
    state = read_publish_state()
    state["publishStatus"] = "pending"
    state["lastPublishAttempt"] = now
    state["pendingReleaseVersion"] = rv
    write_publish_state(state)

    env = os.environ.copy()
    env["PIPELINE_LOCK_HELD"] = env.get("PIPELINE_LOCK_HELD", "1")
    env["SKIP_EXPORT"] = "1"
    env["PUBLISH_PREBUILT"] = "1"
    env["AI_EPS_ROOT"] = str(ROOT)
    rc = subprocess.call(
        ["bash", str(_here / "publish_github_pages.sh")],
        cwd=str(ROOT),
        env=env,
    )
    state = read_publish_state()
    state["lastPublishAttempt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if rc == 0:
        state["publishStatus"] = "published"
        state["lastSuccessfulPublish"] = state["lastPublishAttempt"]
        state["pendingReleaseVersion"] = None
    else:
        # Do NOT rollback financial state — leave pending/failed for retry
        state["publishStatus"] = "failed" if rc not in (0,) else "pending"
        # keep pendingReleaseVersion for retry
        state["pendingReleaseVersion"] = rv
    write_publish_state(state)
    return rc


def retry_pending_publish_if_needed() -> int | None:
    """If publishStatus is pending/failed, retry publish before new collection.

    MUST reconcile live cache to CURRENT before publish (else stale web publish).
    Prefer publish directly from CURRENT generation web/.
    Returns publish rc, or None if no pending publish.
    """
    # Reconcile FIRST — before any publish of live/web cache
    ensure_live_matches_current()
    state = read_publish_state()
    status = str(state.get("publishStatus") or "")
    if status not in {"pending", "failed"}:
        return None
    if not state.get("pendingReleaseVersion") and status != "pending":
        # failed without pending version still retry once if web looks publishable
        pass
    print(f"pending publish retry status={status} release={state.get('pendingReleaseVersion')}")
    return publish_prebuilt_site(release_version=state.get("pendingReleaseVersion"))


def ingest_and_build(
    incoming_path: Path,
    *,
    expected_tickers: list[str] | None = None,
    run_export: bool = True,
    run_publish: bool = False,
    validate_only: bool = False,
    backfill: bool = False,
) -> dict:
    """Process one incoming raw JSON through transactional staged→commit pipeline."""
    ensure_live_matches_current()
    expected = expected_tickers or load_watchlist()
    raw = json.loads(incoming_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"Incoming snapshot not an object: {incoming_path}")

    snap_utc = raw.get("snapshot_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = allocate_unique_run_id(raw)
    stage_dir = STAGING_DIR / run_id
    stage_dir.mkdir(parents=True, exist_ok=False)

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
        backfill=backfill,
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
        qpath = sq.immutable_quarantine_path(QUARANTINE_DIR, tentative_name, snap_norm)
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

    if validate_only:
        # Gate + stage only — MUST NOT commit persistent state
        _write_run_status(stage_dir, "validated_only", {"note": "no commit"})
        result["runStatus"] = "validated_only"
        result["ok"] = True
        result["validatedPath"] = None
        print(f"VALIDATE-ONLY OK status={gate.get('status')} stage={stage_dir} (no commit)")
        return result

    if run_export:
        env = os.environ.copy()
        env["PIPELINE_LOCK_HELD"] = env.get("PIPELINE_LOCK_HELD", "1")
        env["INGEST_GATED_SNAPSHOT"] = str(stage_snap)
        env["INGEST_SNAPSHOT_UTC"] = snap_utc
        env["INGEST_REVISIONS_DONE"] = "1"
        env["INGEST_COLLECTION_RUN_ID"] = run_id
        env["INGEST_STAGE_DIR"] = str(stage_dir)
        # Pure-build: no formal persistent DB mutation before COMMIT
        stage_web = stage_dir / "web"
        stage_web.mkdir(parents=True, exist_ok=True)
        rc = subprocess.call(
            [
                sys.executable,
                str(_here / "export_web_data.py"),
                "--no-persistent-mutation",
                "--output-root",
                str(stage_web),
                "--stage-dir",
                str(stage_dir),
            ],
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
                f"(validated/revisions/incoming/daily/alerts/web untouched)",
                file=sys.stderr,
            )
            return result

    # COMMIT after successful export (or when export skipped after gate)
    try:
        validated_path = commit_staged_run(
            stage_dir, snap_norm, events, snap_utc, run_id=run_id
        )
    except CommitAborted as ca:
        result["runStatus"] = "aborted"
        result["ok"] = False
        result["validatedPath"] = None
        result["commitError"] = str(ca)
        print(
            f"ERROR: commit aborted/rolled back — persistent state unchanged ({ca})",
            file=sys.stderr,
        )
        return result
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
    ap.add_argument(
        "--validate-only",
        action="store_true",
        help="Gate + stage only; does NOT commit persistent financial state",
    )
    ap.add_argument(
        "--no-export",
        action="store_true",
        help="DEPRECATED unsafe — use --validate-only (refuses to commit without export)",
    )
    ap.add_argument(
        "--publish",
        action="store_true",
        help="After single export+commit, publish_prebuilt_site (no second export)",
    )
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="Allow historical/old snapshot timestamps (explicit backfill mode)",
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
        # Scheduler: pending publish must retry before new collection
        try:
            retry_pending_publish_if_needed()
        except Exception as pub_exc:
            print(f"WARNING: pending publish retry error: {pub_exc}", file=sys.stderr)
        try:
            retry_pending_canonical_git_if_needed()
        except Exception as git_exc:
            print(f"WARNING: pending canonical source-git retry error: {git_exc}", file=sys.stderr)
        if args.no_export and not args.validate_only:
            print(
                "ERROR: --no-export is banned for production ingest "
                "(unsafe no-export commit). Use --validate-only (no commit).",
                file=sys.stderr,
            )
            return 2
        kwargs = dict(
            run_export=not args.validate_only,
            run_publish=args.publish and not args.validate_only,
            validate_only=args.validate_only,
            backfill=args.backfill,
        )
        if args.incoming:
            result = ingest_and_build(Path(args.incoming), **kwargs)
        else:
            result = ingest_latest_incoming(**kwargs)
        return 0 if result.get("ok") else 1
    finally:
        if lock_cm is not None:
            lock_cm.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())

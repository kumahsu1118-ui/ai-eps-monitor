#!/usr/bin/env python3
"""Fail-closed GitHub Pages publisher with remote durability.

Publisher may succeed only when:
  * CURRENT generation is complete and verifiable
  * the release is built on latest origin/main
  * app / data / generation / release metadata are consistent
  * the release is committed AND pushed AND remotely verified

Any required-step failure → non-zero exit, clear ERROR, no .data-version
advance, no false NO_CHANGES / publish-success marker.

Does not recompute financial history (ingest is the sole writer).
Publish stamps (meta/dashboard sitePublished, canonicalHeadSha, markers,
.data-version) are produced in a disposable staging tree first. Live files
are copied back only after remote verification succeeds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import collection_freshness as cf  # noqa: E402
import ingest_snapshot as ing  # noqa: E402
from atomic_io import atomic_write_json, atomic_write_text  # noqa: E402

SCHEMA_VERSION = ing.SCHEMA_VERSION
REQUIRED_WEB_JSON = (
    "meta.json",
    "companies.json",
    "valuation.json",
    "revisions.json",
    "eps_history.json",
    "earnings.json",
    "watchlist.json",
    "alerts.json",
    "dashboard.json",
)
REQUIRED_GENERATION_RELPATHS = ("generation_meta.json",) + tuple(
    f"web/data/{name}" for name in REQUIRED_WEB_JSON
)
RELEASE_META_KEYS = (
    "generationRunId",
    "canonicalHeadSha",
    "appVersion",
    "dataVersion",
    "refreshVersion",
    "releaseVersion",
    "schemaVersion",
    "lastSuccessfulCollection",
)
META_DASH_SYNC_KEYS = (
    "sitePublished",
    "sitePublishedDisplay",
    "dataVersion",
    "refreshVersion",
    "buildId",
    "lastSuccessfulCollection",
    "lastSuccessfulCollectionDisplay",
    "consensusDataAsOf",
    "consensusDataAsOfDisplay",
    "collectionStatus",
    "collectionStatusLabel",
    "alertEngineStatus",
    "qualityGate",
    "appVersion",
    "releaseVersion",
    "schemaVersion",
    "generationRunId",
    "canonicalHeadSha",
)
IDENTITY_MATCH_KEYS = (
    "generationRunId",
    "appVersion",
    "dataVersion",
    "refreshVersion",
    "releaseVersion",
    "schemaVersion",
    "lastSuccessfulCollection",
)
GIT_AUTHOR_NAME = "AI EPS Monitor"
GIT_AUTHOR_EMAIL = "kumahsu1118-ui@users.noreply.github.com"
DEFAULT_RETRY_MAX = 3
CANONICAL_HISTORY_REL = "data/history/eps_daily"


class PublishError(RuntimeError):
    """Fail-closed publisher error (printed as ERROR: ...)."""


def _root() -> Path:
    return Path(os.environ.get("AI_EPS_ROOT") or ing.ROOT).resolve()


def _site_repo(root: Path) -> Path:
    override = os.environ.get("PUBLISH_SITE_REPO")
    return Path(override).resolve() if override else (root / "site-repo")


def _retry_max() -> int:
    raw = str(os.environ.get("PUBLISH_RETRY_MAX") or DEFAULT_RETRY_MAX).strip()
    try:
        n = int(raw)
    except ValueError as exc:
        raise PublishError(f"PUBLISH_RETRY_MAX is not an integer: {raw}") from exc
    if n < 1:
        raise PublishError("PUBLISH_RETRY_MAX must be >= 1")
    return n


def _publish_branch() -> str:
    return str(os.environ.get("PUBLISH_BRANCH") or "main").strip() or "main"


def _err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)


def _allow_local() -> bool:
    """Test-only local overlay. Formal publisher never uses this implicitly."""
    return os.environ.get("PUBLISH_ALLOW_LOCAL") == "1"


def _load_json_object(path: Path, *, label: str) -> dict:
    if not path.is_file():
        raise PublishError(f"{label} missing: {path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishError(f"{label} corrupt / schema invalid: {exc}") from exc
    if not isinstance(obj, dict):
        raise PublishError(f"{label} corrupt / schema invalid: not a JSON object")
    return obj


def _nonempty(value: Any) -> str:
    return str(value or "").strip()


def load_current_pointer_strict(root: Path) -> dict:
    """CURRENT.json must exist, be a JSON object, and carry a generation id."""
    path = root / "data" / "CURRENT.json"
    if not path.is_file():
        raise PublishError("CURRENT.json missing")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishError(f"CURRENT.json corrupt / schema invalid: {exc}") from exc
    if not isinstance(obj, dict):
        raise PublishError("CURRENT.json corrupt / schema invalid: not a JSON object")
    run_id = _nonempty(obj.get("runId") or obj.get("run_id"))
    if not run_id:
        raise PublishError("CURRENT.json missing generation ID (runId)")
    obj["runId"] = run_id
    return obj


def resolve_generation_dir(root: Path, current: dict) -> Path:
    """Generation path must resolve to <root>/data/generations/<runId> only."""
    run_id = _nonempty(current.get("runId"))
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise PublishError(f"CURRENT runId is not a confined generation id: {run_id!r}")
    gen_root = (root / "data" / "generations").resolve()
    expected = (gen_root / run_id).resolve()
    try:
        expected.relative_to(gen_root)
    except ValueError as exc:
        raise PublishError(
            f"generation path escapes expected root {gen_root}: {expected}"
        ) from exc
    raw = _nonempty(current.get("generation"))
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (root / candidate)
        try:
            candidate = candidate.resolve()
        except OSError as exc:
            raise PublishError(f"generation path unreadable: {raw}") from exc
        if candidate != expected:
            raise PublishError(
                "generation path confined to data/generations/<runId> — "
                f"got {candidate} expected {expected}"
            )
    if not expected.is_dir():
        raise PublishError(
            f"dangling CURRENT: generation missing for runId={run_id}"
        )
    return expected


def _generation_missing_files(gen_dir: Path) -> list[str]:
    missing: list[str] = []
    for rel in REQUIRED_GENERATION_RELPATHS:
        if not (gen_dir / rel).is_file():
            missing.append(rel)
    return missing


def _quality_gate_ok(meta: dict) -> tuple[bool, str]:
    qg = meta.get("qualityGate") if isinstance(meta.get("qualityGate"), dict) else {}
    if qg.get("publishable") is True:
        return True, str(qg.get("status") or "ok")
    return False, (
        f"qualityGate.publishable!=true status={qg.get('status')} "
        f"reason={qg.get('reason')}"
    )


def _build_id_of(obj: dict) -> str:
    return _nonempty(obj.get("buildId") or obj.get("_buildId"))


_TICKER_MAP_FILES = frozenset({"companies.json", "eps_history.json", "earnings.json"})
_COLLECTION_FILES: dict[str, tuple[str, type]] = {
    "valuation.json": ("rows", list),
    "revisions.json": ("revisions", list),
    "watchlist.json": ("tickers", list),
    "alerts.json": ("activeAlerts", list),
}
_TICKER_RESERVED = frozenset({"_buildId", "buildId"})


def _validate_ticker_map(name: str, obj: dict, *, label: str) -> None:
    tickers = [k for k in obj.keys() if k not in _TICKER_RESERVED]
    if not tickers:
        raise PublishError(
            f"{label} {name} schema invalid: empty object / missing required ticker collections"
        )
    for key in tickers:
        if not isinstance(obj.get(key), dict):
            raise PublishError(
                f"{label} {name} schema invalid: ticker {key!r} must be an object"
            )


def _validate_web_json_file_schema(name: str, obj: dict, *, label: str) -> None:
    """Per-file schema: required collections/fields, not merely 'is object'."""
    if name == "meta.json":
        qg = obj.get("qualityGate")
        if not isinstance(qg, dict):
            raise PublishError(f"{label} meta.json qualityGate missing or not an object")
        for key in (
            "schemaVersion",
            "dataVersion",
            "refreshVersion",
            "lastSuccessfulCollection",
            "generationRunId",
            "buildId",
        ):
            if not _nonempty(obj.get(key)):
                raise PublishError(f"{label} meta.json missing required field {key}")
        return
    if name in _TICKER_MAP_FILES:
        _validate_ticker_map(name, obj, label=label)
        return
    if name in _COLLECTION_FILES:
        field, typ = _COLLECTION_FILES[name]
        val = obj.get(field)
        if not isinstance(val, typ):
            raise PublishError(
                f"{label} {name} schema invalid: missing required collection {field} "
                f"(empty object or wrong type)"
            )
        return
    if name == "dashboard.json":
        dash_meta = obj.get("meta")
        if not isinstance(dash_meta, dict):
            raise PublishError(f"{label} dashboard.json.meta missing or not an object")
        companies = obj.get("companies")
        if not isinstance(companies, dict):
            raise PublishError(
                f"{label} dashboard.json schema invalid: missing required collection companies"
            )
        return
    raise PublishError(f"{label} {name} schema invalid: unknown required web json")


def validate_required_web_json(data_dir: Path, *, label: str) -> dict:
    """Parse every REQUIRED_WEB_JSON file; per-file schema + consistent build identity.

    Empty objects, wrong types, missing collections/fields, and missing/inconsistent
    buildId fail closed before any remote mutation.
    """
    loaded: dict[str, dict] = {}
    for name in REQUIRED_WEB_JSON:
        loaded[name] = _load_json_object(data_dir / name, label=f"{label} {name}")
        _validate_web_json_file_schema(name, loaded[name], label=label)
    meta = loaded["meta.json"]
    run_id = _nonempty(meta.get("generationRunId") or meta.get("runId"))
    if not run_id:
        raise PublishError(f"{label} meta.json missing generationRunId")
    meta_build = _nonempty(meta.get("buildId"))
    if not meta_build:
        raise PublishError(f"{label} meta.json missing required field buildId")
    dash = loaded["dashboard.json"]
    dash_meta = dash.get("meta")
    if not isinstance(dash_meta, dict):
        raise PublishError(f"{label} dashboard.json.meta missing or not an object")
    for key in (
        "schemaVersion",
        "dataVersion",
        "refreshVersion",
        "lastSuccessfulCollection",
        "generationRunId",
        "appVersion",
        "releaseVersion",
        "buildId",
        "canonicalHeadSha",
        "sitePublished",
    ):
        if key in meta and key in dash_meta:
            if str(meta.get(key) or "") != str(dash_meta.get(key) or ""):
                raise PublishError(
                    f"{label} meta/dashboard consistency failed for {key}: "
                    f"meta={meta.get(key)!r} dashboard.meta={dash_meta.get(key)!r}"
                )
    dash_run = _nonempty(dash_meta.get("generationRunId") or dash_meta.get("runId"))
    if dash_run and dash_run != run_id:
        raise PublishError(
            f"{label} meta/dashboard generationRunId mismatch meta={run_id} dashboard={dash_run}"
        )
    dash_build = _build_id_of(dash) or _build_id_of(dash_meta)
    if not dash_build:
        raise PublishError(f"{label} dashboard.json missing required field buildId")
    if dash_build != meta_build:
        raise PublishError(
            f"{label} build-identity mismatch meta.buildId={meta_build} "
            f"dashboard.buildId={dash_build}"
        )
    for name, obj in loaded.items():
        file_build = _build_id_of(obj)
        if not file_build:
            raise PublishError(f"{label} {name} missing required field buildId")
        if file_build != meta_build:
            raise PublishError(
                f"{label} {name} build-identity mismatch "
                f"file={file_build} meta={meta_build}"
            )
    return loaded


def require_identity_consistency(current: dict, gmeta: dict, gen_web_meta: dict) -> str:
    """CURRENT / generation_meta / generation web meta: runId+schemaVersion+timestamp."""
    rid = _nonempty(current.get("runId"))
    g_rid = _nonempty(gmeta.get("runId"))
    w_rid = _nonempty(gen_web_meta.get("generationRunId") or gen_web_meta.get("runId"))
    if not rid or not g_rid or not w_rid:
        raise PublishError(
            "missing runId: CURRENT/generation_meta/generation web meta must all present it"
        )
    if g_rid != rid or w_rid != rid:
        raise PublishError(
            f"inconsistent generationRunId CURRENT={rid} generation_meta={g_rid} "
            f"generation web meta={w_rid}"
        )
    schemas = (
        ("CURRENT", current.get("schemaVersion")),
        ("generation_meta", gmeta.get("schemaVersion")),
        ("generation web meta", gen_web_meta.get("schemaVersion")),
    )
    for label, raw in schemas:
        val = _nonempty(raw)
        if not val:
            raise PublishError(f"missing schemaVersion in {label}")
        if val != str(SCHEMA_VERSION):
            raise PublishError(
                f"inconsistent schemaVersion {label}={val} expected={SCHEMA_VERSION}"
            )
    ts_specs = (
        ("CURRENT.snapshot_utc", current.get("snapshot_utc")),
        (
            "generation_meta collection timestamp",
            gmeta.get("lastSuccessfulCollection") or gmeta.get("snapshot_utc"),
        ),
        (
            "generation web meta lastSuccessfulCollection",
            gen_web_meta.get("lastSuccessfulCollection"),
        ),
    )
    parsed: list[datetime] = []
    for label, raw in ts_specs:
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            raise PublishError(f"missing collection timestamp: {label}")
        parsed.append(parse_collection_timestamp(raw, label=label))
    a, b, c = parsed
    def _utc(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _utc(a) != _utc(b) or _utc(a) != _utc(c):
        raise PublishError(
            "inconsistent collection timestamp "
            f"CURRENT={_utc(a)} generation_meta={_utc(b)} generation web meta={_utc(c)}"
        )
    return _utc(a)


def require_current_generation(root: Path) -> tuple[dict, Path, dict, dict, str]:
    """Fail-closed CURRENT + generation package + quality gate + identity.

    Returns (current, gen_dir, generation_meta, generation_web_meta, collection_iso).
    """
    current = load_current_pointer_strict(root)
    gen_dir = resolve_generation_dir(root, current)
    missing = _generation_missing_files(gen_dir)
    if missing:
        raise PublishError(
            f"generation missing required files for runId={current['runId']}: {missing}"
        )
    gmeta = _load_json_object(gen_dir / "generation_meta.json", label="generation_meta.json")
    run_status = str(gmeta.get("runStatus") or gmeta.get("status") or "committed").lower()
    if run_status in {"aborted", "abort", "failed"}:
        raise PublishError(
            f"generation failed quality gate / runStatus={run_status} runId={current['runId']}"
        )
    if str(gmeta.get("abortReason") or "").strip():
        raise PublishError(
            f"generation aborted abortReason={gmeta.get('abortReason')} runId={current['runId']}"
        )
    validate_required_web_json(gen_dir / "web" / "data", label="generation")
    gen_meta = _load_json_object(
        gen_dir / "web" / "data" / "meta.json", label="generation web/data/meta.json"
    )
    ok, reason = _quality_gate_ok(gen_meta)
    if not ok:
        raise PublishError(f"generation failed quality gate: {reason}")
    collection_iso = require_identity_consistency(current, gmeta, gen_meta)
    return current, gen_dir, gmeta, gen_meta, collection_iso


def rematerialize_into_staging(staging: Path, current: dict, gen_dir: Path) -> None:
    """Sync CURRENT generation web payload into disposable staging. Never writes live."""
    if os.environ.get("FAULT_INJECT_PUBLISH_REMATERIALIZE") == "1":
        raise PublishError("rematerialization failure (FAULT_INJECT_PUBLISH_REMATERIALIZE)")
    web_src = gen_dir / "web" / "data"
    web_dst = staging / "web" / "data"
    web_dst.mkdir(parents=True, exist_ok=True)
    if not web_src.is_dir():
        raise PublishError(f"rematerialization failure: generation web/data missing {web_src}")
    for src in web_src.glob("*.json"):
        if src.is_file():
            shutil.copy2(src, web_dst / src.name)
    for src in web_src.glob("*.json"):
        dest = web_dst / src.name
        if not dest.is_file():
            raise PublishError(f"rematerialization failure: staging missing {src.name}")
        try:
            if dest.read_bytes() != src.read_bytes():
                raise PublishError(
                    f"rematerialization failure: staging {src.name} does not match generation"
                )
        except PublishError:
            raise
        except OSError as exc:
            raise PublishError(f"rematerialization failure: {exc}") from exc
    rid = str(current.get("runId") or gen_dir.name)
    marker = staging / "data" / ".materialized_run_id"
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, rid + "\n")
    staging_meta = _load_json_object(
        staging / "web" / "data" / "meta.json", label="staging web/data/meta.json"
    )
    ok, reason = _quality_gate_ok(staging_meta)
    if not ok:
        raise PublishError(f"staging meta failed quality gate: {reason}")
    validate_required_web_json(staging / "web" / "data", label="staging")
    gen_meta = _load_json_object(
        gen_dir / "web" / "data" / "meta.json", label="generation web/data/meta.json"
    )
    for key in ("dataVersion", "refreshVersion", "schemaVersion"):
        gv = str(gen_meta.get(key) or "")
        lv = str(staging_meta.get(key) or "")
        if gv and lv and gv != lv:
            raise PublishError(
                f"inconsistent {key}: generation={gv[:16]} staging={lv[:16]}"
            )


def rematerialize_current(root: Path, current: dict, gen_dir: Path) -> None:
    """Deprecated live rematerialize. Publish uses rematerialize_into_staging only."""
    raise PublishError(
        "live rematerialize_current is banned — rematerialize/build must happen in staging"
    )


def parse_collection_timestamp(value: Any, *, label: str) -> datetime:
    """Fail closed: refuse to guess when a timestamp cannot be parsed."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PublishError(f"malformed timestamp: {label} is missing")
    raw = str(value).strip()
    parsed = cf.parse_iso_dt(raw)
    if parsed is None:
        raise PublishError(f"malformed timestamp: {label}={raw!r}")
    return parsed


def pending_collection_timestamp(current: dict, gen_meta: dict, live_meta: dict) -> datetime:
    for label, value in (
        ("CURRENT.snapshot_utc", current.get("snapshot_utc")),
        ("generation lastSuccessfulCollection", gen_meta.get("lastSuccessfulCollection")),
        ("live lastSuccessfulCollection", live_meta.get("lastSuccessfulCollection")),
    ):
        if value is None or (isinstance(value, str) and not str(value).strip()):
            continue
        return parse_collection_timestamp(value, label=label)
    raise PublishError("malformed timestamp: no collection timestamp in CURRENT/generation/live meta")


def collection_timestamp_iso(current: dict, gen_meta: dict, live_meta: dict) -> str:
    dt = pending_collection_timestamp(current, gen_meta, live_meta)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compare_pending_vs_remote_timestamp(
    pending: datetime,
    remote_raw: Any,
    *,
    remote_present: bool,
) -> None:
    """Never overwrite newer published data with older. Unparseable → fail closed."""
    if not remote_present:
        return
    if remote_raw is None or (isinstance(remote_raw, str) and not str(remote_raw).strip()):
        raise PublishError("malformed timestamp: remote lastSuccessfulCollection is missing")
    remote_dt = parse_collection_timestamp(remote_raw, label="remote lastSuccessfulCollection")
    if pending < remote_dt:
        raise PublishError(
            "pending collection timestamp older than remote / currently published "
            f"pending={pending.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"remote={remote_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            "— refusing stale-data regression"
        )


def compute_payload_hash(web_root: Path) -> str:
    """Content hash: full static tree + dataVersion + refreshVersion (not sitePublished)."""
    web = Path(web_root)
    h = hashlib.sha256()

    def feed_bytes(b: bytes) -> None:
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)

    for p in ing.iter_frontend_static_files(web):
        feed_bytes(str(p.relative_to(web)).encode("utf-8"))
        feed_bytes(p.read_bytes())
    meta_path = web / "data" / "meta.json"
    meta = _load_json_object(meta_path, label="web/data/meta.json")
    feed_bytes(str(meta.get("dataVersion") or "").encode("utf-8"))
    feed_bytes(b"|")
    feed_bytes(str(meta.get("refreshVersion") or "").encode("utf-8"))
    return h.hexdigest()


def _stamp_site_published(meta: dict) -> dict:
    now = datetime.now(timezone(timedelta(hours=8)))
    utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    display = f"{months[now.month - 1]} {now.day}, {now.year} {now.hour:02d}:{now.minute:02d} Taipei Time"
    meta["sitePublished"] = utc
    meta["sitePublishedDisplay"] = display
    meta.pop("latestSuccessfulRefresh", None)
    meta.pop("siteRepoCommit", None)
    return meta


def stamp_release_metadata(
    tree_root: Path,
    *,
    generation_run_id: str,
    canonical_head_sha: str,
    collection_iso: str,
    payload_hash: str,
    stamp_published: bool,
    write_marker: bool = False,
) -> dict:
    """Write traceable release fields into a tree's meta.json and dashboard.json.meta.

    Default does not write live ``data/.last-publish-web`` (that marker is applied
    only after remote verification, from the staging tree).
    """
    web = tree_root / "web"
    meta_path = web / "data" / "meta.json"
    meta = _load_json_object(meta_path, label="web/data/meta.json")
    meta["generationRunId"] = generation_run_id
    meta["canonicalHeadSha"] = canonical_head_sha
    if collection_iso and not meta.get("lastSuccessfulCollection"):
        meta["lastSuccessfulCollection"] = collection_iso
    if stamp_published:
        _stamp_site_published(meta)
    if not meta.get("dataVersion") and payload_hash:
        meta["dataVersion"] = payload_hash
        meta["buildId"] = payload_hash
    atomic_write_json(meta_path, meta)
    dash_path = web / "data" / "dashboard.json"
    if dash_path.is_file():
        dash = _load_json_object(dash_path, label="web/data/dashboard.json")
        dash_meta = dict(dash.get("meta") or {})
        for k in META_DASH_SYNC_KEYS:
            if k in meta:
                dash_meta[k] = meta[k]
        dash["meta"] = dash_meta
        if meta.get("buildId"):
            dash["buildId"] = meta["buildId"]
        atomic_write_json(dash_path, dash)
    if write_marker:
        marker = tree_root / "data" / ".last-publish-web"
        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(marker, str((tree_root / "web").resolve()) + "\n")
    return meta


def expected_release_identity(meta: dict, current: dict, collection_iso: str) -> dict:
    return {
        "generationRunId": str(meta.get("generationRunId") or current["runId"]),
        "canonicalHeadSha": str(meta.get("canonicalHeadSha") or ""),
        "appVersion": str(meta.get("appVersion") or ""),
        "dataVersion": str(meta.get("dataVersion") or ""),
        "refreshVersion": str(meta.get("refreshVersion") or ""),
        "releaseVersion": str(meta.get("releaseVersion") or ""),
        "schemaVersion": str(meta.get("schemaVersion") or ""),
        "lastSuccessfulCollection": str(meta.get("lastSuccessfulCollection") or collection_iso),
    }


def _identity_matches(remote_meta: dict, expected: dict, *, require_canonical: bool) -> bool:
    for key in IDENTITY_MATCH_KEYS:
        if str(remote_meta.get(key) or "") != str(expected.get(key) or ""):
            return False
    if require_canonical:
        if str(remote_meta.get("canonicalHeadSha") or "") != str(expected.get("canonicalHeadSha") or ""):
            return False
    return True


def overlay_public_tree(web: Path, dest: Path) -> None:
    """Copy full public asset tree into dest (Pages root). Never touches dest/.git."""
    dest.mkdir(parents=True, exist_ok=True)
    for p in ing.iter_frontend_static_files(web):
        rel = p.relative_to(web)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    for name in ("index.html", "app.js", "styles.css"):
        src = web / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    vendor_src = web / "vendor"
    if vendor_src.is_dir():
        for src in vendor_src.rglob("*"):
            if src.is_file():
                target = dest / "vendor" / src.relative_to(vendor_src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
    data_src = web / "data"
    data_dst = dest / "data"
    data_dst.mkdir(parents=True, exist_ok=True)
    if data_src.is_dir():
        for src in data_src.rglob("*"):
            if src.is_file():
                target = data_dst / src.relative_to(data_src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
    # Keep committed web/ in lockstep with Pages root (app + data consistency).
    web_dest = dest / "web"
    web_dest.mkdir(parents=True, exist_ok=True)
    for p in ing.iter_frontend_static_files(web):
        rel = p.relative_to(web)
        target = web_dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    web_data_dst = web_dest / "data"
    web_data_dst.mkdir(parents=True, exist_ok=True)
    if data_src.is_dir():
        for src in data_src.rglob("*"):
            if src.is_file():
                target = web_data_dst / src.relative_to(data_src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
    shutil.copy2(dest / "index.html", dest / "404.html")
    (dest / ".nojekyll").write_text("", encoding="utf-8")
    for junk in ("ORIGIN.txt", "overview-verify.png"):
        p = dest / junk
        if p.exists():
            p.unlink()
    missing = [
        rel for rel in ing.list_index_local_assets(web / "index.html") if not (dest / rel).is_file()
    ]
    if missing:
        raise PublishError(f"clean publish missing index assets: {missing}")


def _git(
    cwd: Path,
    args: list[str],
    *,
    check: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        raise PublishError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:800]}"
        )
    return proc


def _is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _has_origin_remote(git_base: Path) -> bool:
    proc = _git(git_base, ["remote", "get-url", "origin"])
    if proc.returncode == 0 and _nonempty(proc.stdout):
        return True
    if _nonempty(os.environ.get("PUBLISH_REMOTE")):
        return True
    return False


def resolve_git_base(root: Path, site: Path) -> Path | None:
    """Git directory used for fetch/worktree. Prefer source clone, else site-repo."""
    if _is_git_repo(root):
        return root
    if _is_git_repo(site):
        return site
    return None


def fetch_origin(git_base: Path) -> None:
    remote = os.environ.get("PUBLISH_REMOTE")
    if remote:
        proc = _git(git_base, ["remote", "get-url", "origin"])
        if proc.returncode != 0:
            _git(git_base, ["remote", "add", "origin", remote], check=True)
        elif (proc.stdout or "").strip() != remote:
            _git(git_base, ["remote", "set-url", "origin", remote], check=True)
    proc = _git(git_base, ["fetch", "origin", "--prune"])
    if proc.returncode != 0:
        raise PublishError(
            f"git fetch/sync remote failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:800]}"
        )


def origin_head_sha(git_base: Path) -> str:
    branch = _publish_branch()
    proc = _git(git_base, ["rev-parse", f"origin/{branch}"])
    if proc.returncode != 0:
        proc = _git(git_base, ["rev-parse", f"refs/remotes/origin/{branch}"])
    if proc.returncode != 0:
        raise PublishError(
            f"origin/{branch} missing after fetch — cannot publish on latest remote main"
        )
    sha = (proc.stdout or "").strip()
    if len(sha) < 7:
        raise PublishError(f"origin/{branch} SHA unreadable")
    return sha


def show_commit_file(git_base: Path, sha: str, relpath: str) -> str | None:
    proc = _git(git_base, ["show", f"{sha}:{relpath}"])
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def show_origin_file(git_base: Path, relpath: str) -> str | None:
    branch = _publish_branch()
    return show_commit_file(git_base, f"origin/{branch}", relpath)


def remote_published_meta(git_base: Path) -> tuple[dict | None, str | None]:
    raw = show_origin_file(git_base, "data/meta.json")
    version = show_origin_file(git_base, ".data-version")
    version = (version or "").strip() or None
    if raw is None:
        return None, version
    try:
        obj = json.loads(raw)
    except Exception as exc:
        raise PublishError(f"malformed timestamp / remote meta.json corrupt: {exc}") from exc
    if not isinstance(obj, dict):
        raise PublishError("remote data/meta.json is not a JSON object")
    return obj, version


def generation_canonical_months(gen_dir: Path) -> dict[str, str]:
    d = gen_dir / "history" / "eps_daily"
    if not d.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(d.glob("*.jsonl")):
        if p.is_file():
            out[p.name] = p.read_text(encoding="utf-8")
    return out


def prove_canonical_head_sha(git_base: Path, gen_dir: Path) -> str:
    """canonicalHeadSha is a commit that already contains CURRENT canonical history.

    Latest origin/main is used only after that proof. Missing observations → fail closed.
    """
    sha = origin_head_sha(git_base)
    months = generation_canonical_months(gen_dir)
    for name, content in months.items():
        rel = f"{CANONICAL_HISTORY_REL}/{name}"
        remote = show_commit_file(git_base, sha, rel)
        if remote is None:
            raise PublishError(
                f"remote does not contain CURRENT canonical observations {rel} "
                f"at {sha} — refusing publish"
            )
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line not in remote:
                raise PublishError(
                    f"remote missing CURRENT canonical observation in {rel} "
                    f"at {sha} — refusing publish"
                )
    return sha


def read_canonical_git_state(root: Path) -> dict:
    path = root / "data" / "canonical_git_state.json"
    if not path.is_file():
        return {"status": "clean", "lastAttempt": None, "lastSuccessful": None, "error": None}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception as exc:
        raise PublishError(f"canonical_git_state.json corrupt: {exc}") from exc


def _canonical_history_matches_origin(git_base: Path, branch: str) -> None:
    """Refuse dirty live history that is not already on origin.

    Persist leaves source HEAD behind on purpose. A fully clean history path
    (even if HEAD lags origin) is allowed — publisher rebuilds from origin.
    Untracked or modified jsonl files are OK only when their bytes match origin.
    """
    porcelain = _git(git_base, ["status", "--porcelain", "--", CANONICAL_HISTORY_REL])
    if not _nonempty(porcelain.stdout):
        return
    origin_ref = f"origin/{branch}"
    ls = _git(git_base, ["ls-tree", "-r", "--full-name", origin_ref, "--", CANONICAL_HISTORY_REL])
    origin_blobs: dict[str, str] = {}
    for line in (ls.stdout or "").splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and path.endswith(".jsonl"):
            origin_blobs[str(Path(path).name)] = parts[2]
    live_dir = git_base / CANONICAL_HISTORY_REL
    live_blobs: dict[str, str] = {}
    if live_dir.is_dir():
        for src in sorted(live_dir.glob("*.jsonl")):
            if not src.is_file():
                continue
            hashed = _git(git_base, ["hash-object", str(src)])
            sha = (hashed.stdout or "").strip()
            if hashed.returncode != 0 or not sha:
                raise PublishError(
                    f"canonical working-tree hash failed for {src.name} — refusing publish"
                )
            live_blobs[src.name] = sha
    if live_blobs != origin_blobs:
        raise PublishError(
            "canonical working-tree has a diff under data/history/eps_daily — refusing publish"
        )


def require_canonical_git_ready(root: Path, git_base: Path | None, gen_dir: Path) -> str:
    """Refuse pending/failed canonical git, unpushed commits, dirty tree, missing remote obs."""
    state = read_canonical_git_state(root)
    status = str(state.get("status") or "").strip().lower()
    if status in {"pending", "failed"}:
        raise PublishError(
            f"canonical git state {status} — refusing publish until source-git persist succeeds"
        )
    if git_base is None:
        return ""
    branch = _publish_branch()
    ahead = _git(git_base, ["rev-list", "--count", f"origin/{branch}..HEAD"])
    try:
        n_ahead = int((ahead.stdout or "0").strip() or 0) if ahead.returncode == 0 else 0
    except ValueError:
        n_ahead = 1
    if n_ahead > 0:
        names = _git(
            git_base,
            [
                "log",
                "--name-only",
                "--pretty=format:",
                f"origin/{branch}..HEAD",
                "--",
                CANONICAL_HISTORY_REL,
            ],
        )
        if names.returncode == 0 and _nonempty(names.stdout):
            raise PublishError(
                "unpushed canonical commit under data/history/eps_daily — refusing publish"
            )
    _canonical_history_matches_origin(git_base, branch)
    return prove_canonical_head_sha(git_base, gen_dir)


def _cleanup_worktree(git_base: Path, wt: Path) -> None:
    if wt.exists():
        _git(git_base, ["worktree", "remove", "--force", str(wt)])
        shutil.rmtree(wt, ignore_errors=True)
    _git(git_base, ["worktree", "prune"])


def _nothing_to_commit(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}".lower()
    return (
        "nothing to commit" in blob
        or "no changes added to commit" in blob
        or "working tree clean" in blob
    )


def _is_non_fast_forward(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}".lower()
    return (
        "non-fast-forward" in blob
        or "fetch first" in blob
        or "updates were rejected because the remote contains work" in blob
        or "[rejected]" in blob and "fetch first" in blob
    )


def commit_and_push_worktree(
    git_base: Path,
    wt: Path,
    *,
    payload_hash: str,
    expected: dict,
    base_sha: str,
) -> str:
    """Commit overlay in disposable worktree and push to origin/branch. Return new SHA."""
    branch = _publish_branch()
    _git(wt, ["add", "-A"], check=True)
    cached = _git(wt, ["diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        fetch_origin(git_base)
        remote_meta, remote_ver = remote_published_meta(git_base)
        if remote_meta is None or (remote_ver or "").strip() != payload_hash:
            raise PublishError(
                "git commit produced nothing to commit but remote does not already "
                "carry this payload — .data-version NOT updated"
            )
        if not _identity_matches(remote_meta, expected, require_canonical=False):
            raise PublishError(
                "nothing to commit but remote release metadata does not match "
                "expectation — .data-version NOT updated"
            )
        return origin_head_sha(git_base)
    msg = f"Update dashboard data {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"
    commit = _git(
        wt,
        [
            "-c",
            f"user.email={GIT_AUTHOR_EMAIL}",
            "-c",
            f"user.name={GIT_AUTHOR_NAME}",
            "commit",
            "-m",
            msg,
        ],
    )
    if commit.returncode != 0:
        combined = f"{commit.stderr or ''}\n{commit.stdout or ''}"
        if _nothing_to_commit(commit.stderr or "", commit.stdout or ""):
            head = _git(wt, ["rev-parse", "HEAD"], check=True)
            return (head.stdout or "").strip()
        raise PublishError(
            f"git commit failed (rc={commit.returncode}) — .data-version NOT updated; "
            f"{combined.strip()[:800]}"
        )
    new_sha = (_git(wt, ["rev-parse", "HEAD"], check=True).stdout or "").strip()
    push = _git(wt, ["push", "origin", f"HEAD:{branch}"])
    if push.returncode != 0:
        combined = f"{push.stderr or ''}\n{push.stdout or ''}"
        remote_moved = False
        try:
            fetch_origin(git_base)
            remote_moved = origin_head_sha(git_base) != base_sha
        except PublishError:
            remote_moved = False
        err = PublishError(
            f"git push failed (rc={push.returncode}) — .data-version NOT updated; "
            f"{combined.strip()[:800]}"
        )
        err.non_fast_forward = _is_non_fast_forward(push.stderr or "", push.stdout or "") or remote_moved  # type: ignore[attr-defined]
        raise err
    if os.environ.get("FAULT_INJECT_REMOTE_VERIFY_MISS") == "1":
        raise PublishError(
            f"remote verification failed: target commit {new_sha} not found on remote "
            "(FAULT_INJECT_REMOTE_VERIFY_MISS) — .data-version NOT updated"
        )
    fetch_origin(git_base)
    remote_sha = origin_head_sha(git_base)
    if remote_sha != new_sha:
        raise PublishError(
            f"remote verification failed: target commit {new_sha} not found on remote "
            f"(origin/{branch}={remote_sha}) — .data-version NOT updated"
        )
    remote_meta, remote_ver = remote_published_meta(git_base)
    if remote_meta is None:
        raise PublishError(
            "remote verification failed: data/meta.json missing on remote after push"
        )
    if not _identity_matches(remote_meta, expected, require_canonical=True):
        raise PublishError(
            "remote commit release metadata does not match expectation exactly "
            f"expected={ {k: expected.get(k) for k in RELEASE_META_KEYS} } "
            f"remote generationRunId={remote_meta.get('generationRunId')} "
            f"canonicalHeadSha={remote_meta.get('canonicalHeadSha')} "
            f"schemaVersion={remote_meta.get('schemaVersion')} "
            f"releaseVersion={str(remote_meta.get('releaseVersion') or '')[:16]}"
        )
    if (remote_ver or "").strip() != payload_hash:
        raise PublishError(
            f"remote .data-version does not match payload hash "
            f"remote={remote_ver!r} expected={payload_hash[:16]}"
        )
    return new_sha


def _copy_staged_web_to_live(staging: Path, live_root: Path) -> None:
    """Apply verified staging stamps onto live web/data + marker. Not used on failure."""
    src_data = staging / "web" / "data"
    dst_data = live_root / "web" / "data"
    dst_data.mkdir(parents=True, exist_ok=True)
    if src_data.is_dir():
        for src in src_data.rglob("*"):
            if src.is_file():
                target = dst_data / src.relative_to(src_data)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
    for name in ("index.html", "app.js", "styles.css"):
        src = staging / "web" / name
        if src.is_file():
            shutil.copy2(src, live_root / "web" / name)
    marker_src = staging / "data" / ".last-publish-web"
    live_root_data = live_root / "data"
    live_root_data.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        live_root_data / ".last-publish-web",
        str((live_root / "web").resolve()) + "\n",
    )
    mat_src = staging / "data" / ".materialized_run_id"
    if mat_src.is_file():
        shutil.copy2(mat_src, live_root_data / ".materialized_run_id")
    if marker_src.is_file():
        pass


def make_staging_tree(root: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="ai-eps-publish-stage-"))
    shutil.copytree(root / "web", staging / "web", dirs_exist_ok=True)
    (staging / "data").mkdir(parents=True, exist_ok=True)
    return staging


def prepare_staging_release(
    staging: Path,
    *,
    generation_run_id: str,
    canonical_head_sha: str,
    collection_iso: str,
    stamp_published: bool,
) -> tuple[dict, str, dict]:
    """Finalize identity + stamp on staging only. Returns (meta, payload_hash, expected)."""
    try:
        ing.finalize_release_identity(staging / "web")
    except Exception as exc:
        raise PublishError(f"release identity finalization failed: {exc}") from exc
    meta = stamp_release_metadata(
        staging,
        generation_run_id=generation_run_id,
        canonical_head_sha=canonical_head_sha,
        collection_iso=collection_iso,
        payload_hash="",
        stamp_published=stamp_published,
        write_marker=False,
    )
    try:
        ing.finalize_release_identity(staging / "web")
    except Exception as exc:
        raise PublishError(f"release identity finalization failed: {exc}") from exc
    meta = _load_json_object(staging / "web" / "data" / "meta.json", label="staging web/data/meta.json")
    validate_required_web_json(staging / "web" / "data", label="staging")
    payload_hash = compute_payload_hash(staging / "web")
    expected = expected_release_identity(meta, {"runId": generation_run_id}, collection_iso)
    expected["canonicalHeadSha"] = canonical_head_sha
    if not _nonempty(expected.get("schemaVersion")):
        raise PublishError("staging meta.json missing schemaVersion after finalize")
    return meta, payload_hash, expected


def publish_via_worktree(
    root: Path,
    git_base: Path,
    site: Path,
    staging: Path,
    *,
    gen_dir: Path,
    generation_run_id: str,
    collection_iso: str,
    payload_hash: str,
    expected: dict,
) -> str:
    """Disposable worktree from latest origin/main + bounded fetch-and-rebuild retry."""
    max_attempts = _retry_max()
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        fetch_origin(git_base)
        base_sha = prove_canonical_head_sha(git_base, gen_dir)
        expected = dict(expected)
        expected["canonicalHeadSha"] = base_sha
        stamp_release_metadata(
            staging,
            generation_run_id=generation_run_id,
            canonical_head_sha=base_sha,
            collection_iso=collection_iso,
            payload_hash=payload_hash,
            stamp_published=True,
            write_marker=False,
        )
        live_meta = _load_json_object(
            staging / "web" / "data" / "meta.json", label="staging web/data/meta.json"
        )
        expected = expected_release_identity(
            live_meta, {"runId": generation_run_id}, collection_iso
        )
        expected["canonicalHeadSha"] = base_sha
        validate_required_web_json(staging / "web" / "data", label="staging")
        remote_meta, _remote_ver = remote_published_meta(git_base)
        pending = parse_collection_timestamp(
            expected["lastSuccessfulCollection"], label="pending lastSuccessfulCollection"
        )
        if remote_meta is not None:
            compare_pending_vs_remote_timestamp(
                pending,
                remote_meta.get("lastSuccessfulCollection"),
                remote_present=True,
            )
        wt = Path(tempfile.mkdtemp(prefix="ai-eps-publish-wt-"))
        shutil.rmtree(wt, ignore_errors=True)
        add = _git(git_base, ["worktree", "add", "--detach", str(wt), base_sha])
        if add.returncode != 0:
            last_err = PublishError(
                f"git worktree add failed (rc={add.returncode}): "
                f"{(add.stderr or add.stdout or '').strip()[:800]}"
            )
            continue
        try:
            overlay_public_tree(staging / "web", wt)
            atomic_write_text(wt / ".data-version", payload_hash + "\n")
            sha = commit_and_push_worktree(
                git_base,
                wt,
                payload_hash=payload_hash,
                expected=expected,
                base_sha=base_sha,
            )
            overlay_public_tree(staging / "web", site)
            atomic_write_text(site / ".data-version", payload_hash + "\n")
            _copy_staged_web_to_live(staging, root)
            print(f"PUSHED hash={payload_hash[:12]} commit={sha[:12]} attempt={attempt}")
            return sha
        except PublishError as exc:
            last_err = exc
            nff = bool(getattr(exc, "non_fast_forward", False))
            _cleanup_worktree(git_base, wt)
            if nff and attempt < max_attempts:
                print(
                    f"non-fast-forward on attempt {attempt}/{max_attempts} — "
                    "fetch-and-rebuild from newest remote HEAD (no force-push)",
                    file=sys.stderr,
                )
                continue
            if nff and attempt >= max_attempts:
                raise PublishError(
                    f"retry budget exhausted ({max_attempts}) after non-fast-forward; "
                    "remote commits preserved (no force-push); .data-version NOT updated"
                ) from exc
            raise
        finally:
            _cleanup_worktree(git_base, wt)
    raise PublishError(
        f"retry budget exhausted ({max_attempts}); last error: {last_err}; "
        ".data-version NOT updated"
    )


def publish_local(staging: Path, root: Path, site: Path, *, payload_hash: str) -> None:
    """Test-only local overlay. Formal publisher never takes this path implicitly."""
    overlay_public_tree(staging / "web", site)
    atomic_write_text(site / ".data-version", payload_hash + "\n")
    _copy_staged_web_to_live(staging, root)
    print(f"PUBLISH_LOCAL hash={payload_hash[:12]} (PUBLISH_ALLOW_LOCAL=1)")


def remote_is_same_release(
    git_base: Path,
    *,
    payload_hash: str,
    expected: dict,
) -> bool:
    remote_meta, remote_ver = remote_published_meta(git_base)
    if remote_meta is None:
        return False
    if (remote_ver or "").strip() != payload_hash:
        return False
    return _identity_matches(remote_meta, expected, require_canonical=False)


def preflight(root: Path) -> int:
    """Read-only CURRENT + generation + canonical git checks. No materialize, no live writes."""
    ing.rebind_paths(root)
    current, gen_dir, _gmeta, gen_web_meta, _iso = require_current_generation(root)
    pending_collection_timestamp(current, gen_web_meta, gen_web_meta)
    site = _site_repo(root)
    git_base = resolve_git_base(root, site)
    if git_base is not None and _has_origin_remote(git_base):
        fetch_origin(git_base)
        origin_head_sha(git_base)
        require_canonical_git_ready(root, git_base, gen_dir)
    elif git_base is None and not _allow_local():
        raise PublishError(
            "no Git repository/remote — formal publisher refuses "
            "(PUBLISH_ALLOW_LOCAL is test-only)"
        )
    print("PREFLIGHT_OK")
    return 0


def publish(root: Path) -> int:
    ing.rebind_paths(root)
    if os.environ.get("ALLOW_PUBLISH_EXPORT") == "1":
        raise PublishError("ALLOW_PUBLISH_EXPORT is banned — publish must not recompute")
    print("publish-only: no export / no history mutation (ingest_snapshot.py is sole writer)")

    current, gen_dir, _gmeta, gen_web_meta, collection_iso = require_current_generation(root)

    site = _site_repo(root)
    git_base = resolve_git_base(root, site)
    has_remote = git_base is not None and _has_origin_remote(git_base)

    staging = make_staging_tree(root)
    try:
        rematerialize_into_staging(staging, current, gen_dir)
        staging_meta = _load_json_object(
            staging / "web" / "data" / "meta.json", label="staging web/data/meta.json"
        )
        ok, reason = _quality_gate_ok(staging_meta)
        if not ok:
            raise PublishError(f"{reason} — abort publish (no git push)")
        print(
            f"qualityGate OK status={(staging_meta.get('qualityGate') or {}).get('status')} "
            "publishable=true"
        )

        if git_base is None or not has_remote:
            if not _allow_local():
                raise PublishError(
                    "no Git repository/remote — formal publisher refuses "
                    "(PUBLISH_ALLOW_LOCAL is test-only)"
                )
            _meta, payload_hash, expected = prepare_staging_release(
                staging,
                generation_run_id=current["runId"],
                canonical_head_sha="local",
                collection_iso=collection_iso,
                stamp_published=False,
            )
            site_meta_path = site / "data" / "meta.json"
            if site_meta_path.is_file():
                site_meta = _load_json_object(site_meta_path, label="site-repo data/meta.json")
                compare_pending_vs_remote_timestamp(
                    parse_collection_timestamp(collection_iso, label="pending lastSuccessfulCollection"),
                    site_meta.get("lastSuccessfulCollection"),
                    remote_present=True,
                )
            prev = ""
            version_file = site / ".data-version"
            if version_file.is_file():
                prev = version_file.read_text(encoding="utf-8").strip()
            if prev and prev == payload_hash:
                print("NO_CHANGES")
                return 0
            stamp_release_metadata(
                staging,
                generation_run_id=current["runId"],
                canonical_head_sha="local",
                collection_iso=collection_iso,
                payload_hash=payload_hash,
                stamp_published=True,
                write_marker=False,
            )
            print("stamped sitePublished (meta.json + dashboard.json.meta synced)")
            publish_local(staging, root, site, payload_hash=payload_hash)
            return 0

        fetch_origin(git_base)
        require_canonical_git_ready(root, git_base, gen_dir)
        remote_meta, _remote_ver = remote_published_meta(git_base)
        pending_dt = parse_collection_timestamp(
            collection_iso, label="pending lastSuccessfulCollection"
        )
        if remote_meta is not None:
            compare_pending_vs_remote_timestamp(
                pending_dt,
                remote_meta.get("lastSuccessfulCollection"),
                remote_present=True,
            )

        proven_sha = prove_canonical_head_sha(git_base, gen_dir)
        _meta, payload_hash, expected = prepare_staging_release(
            staging,
            generation_run_id=current["runId"],
            canonical_head_sha=proven_sha,
            collection_iso=collection_iso,
            stamp_published=False,
        )
        if remote_is_same_release(git_base, payload_hash=payload_hash, expected=expected):
            print("NO_CHANGES")
            return 0
        publish_via_worktree(
            root,
            git_base,
            site,
            staging,
            gen_dir=gen_dir,
            generation_run_id=current["runId"],
            collection_iso=collection_iso,
            payload_hash=payload_hash,
            expected=expected,
        )
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed Pages publisher")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate CURRENT + remote fetch without commit/push/materialize",
    )
    args = parser.parse_args(argv)
    root = _root()
    try:
        if args.preflight:
            return preflight(root)
        return publish(root)
    except PublishError as exc:
        _err(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _err(f"publisher crashed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

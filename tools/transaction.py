#!/usr/bin/env python3
"""True transaction boundary: stage → COMMIT (CURRENT) → materialize live trees.

The commit point is the atomic CURRENT pointer under data/generations/CURRENT.
After CURRENT flips, the generation is committed. Materialization (promote onto
live canonical trees) may fail: runStatus stays committed and
materializationStatus is failed/pending — never aborted. Retry materializes
from CURRENT; it does not roll the pointer back.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json, atomic_write_text
except Exception:  # pragma: no cover
    def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding=encoding)

    def atomic_write_json(path: Path, obj, *, lock_path: Path | None = None) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

CANONICAL_TREES = (
    "data/snapshots",
    "data/revisions",
    "data/daily_eps_snapshots",
    "data/alerts",
    "web",
)
INPUT_TREES = (
    "data/drivers",
    "data/earnings",
)
INPUT_FILES = (
    "data/universe.json",
    "data/comparison_checkpoint.json",
    "VERSION",
)

MATERIALIZATION_OK = "ok"
MATERIALIZATION_PENDING = "pending"
MATERIALIZATION_FAILED = "failed"
MATERIALIZATION_STATUSES = frozenset({
    MATERIALIZATION_OK,
    MATERIALIZATION_PENDING,
    MATERIALIZATION_FAILED,
})


class TransactionError(RuntimeError):
    pass


class MaterializationError(TransactionError):
    """CURRENT already flipped; live promote failed. Not an abort."""

    def __init__(self, gen_id: str, status: str, error: str | None = None):
        self.generation_id = gen_id
        self.materializationStatus = status
        self.materializationError = error
        super().__init__(
            f"materialization {status} for generation {gen_id}"
            + (f": {error}" if error else "")
        )


def generations_dir(root: Path) -> Path:
    return Path(root) / "data" / "generations"


def current_pointer_path(root: Path) -> Path:
    return generations_dir(root) / "CURRENT"


def read_current(root: Path) -> str | None:
    p = current_pointer_path(root)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def new_generation_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generation_root(root: Path, gen_id: str) -> Path:
    return generations_dir(root) / gen_id


def materialization_path(root: Path, gen_id: str | None = None) -> Path:
    gid = gen_id or read_current(root) or "unknown"
    return generation_root(root, gid) / "materialization.json"


def read_materialization(root: Path, gen_id: str | None = None) -> dict:
    p = materialization_path(root, gen_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_materialization_status(
    root: Path,
    gen_id: str,
    status: str,
    *,
    error: str | None = None,
    extra: dict | None = None,
) -> dict:
    if status not in MATERIALIZATION_STATUSES:
        raise TransactionError(f"invalid materializationStatus={status!r}")
    payload = {
        "generationId": gen_id,
        "materializationStatus": status,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "committed": True,
    }
    if error:
        payload["materializationError"] = str(error)
    if extra:
        payload.update(extra)
    dest = materialization_path(root, gen_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(dest, payload)
    # Convenience pointer for operators / publish
    atomic_write_json(generations_dir(root) / "MATERIALIZATION.json", payload)
    return payload


def _copy_entry(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        if dest.suffix:
            return
        dest.mkdir(parents=True, exist_ok=True)
        return
    if src.is_file():
        shutil.copy2(src, dest)
        return
    if dest.exists() and dest.is_dir():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def overlay_tree(src: Path, dest: Path) -> None:
    """Copy src files onto dest (create dest). Does not delete extra dest files."""
    if not src.exists():
        return
    if src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return
    dest.mkdir(parents=True, exist_ok=True)
    for p in src.rglob("*"):
        if p.is_file():
            target = dest / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def prepare_work_root(root: Path, stage_dir: Path) -> Path:
    """Build a mini project tree under stage_dir/work with copies of live trees."""
    root = Path(root)
    work = Path(stage_dir) / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "data" / "snapshots").mkdir(parents=True, exist_ok=True)
    for rel in CANONICAL_TREES + INPUT_TREES:
        _copy_entry(root / rel, work / rel)
    for rel in INPUT_FILES:
        src = root / rel
        if src.exists():
            _copy_entry(src, work / rel)
    # Tools are invoked from the real repo; work root only needs data/web.
    return work


def write_current_pointer(root: Path, gen_id: str) -> None:
    """Atomic CURRENT update — this is the commit point."""
    atomic_write_text(current_pointer_path(root), gen_id + "\n")


def snapshot_generation(root: Path, work: Path, gen_id: str) -> Path:
    gen = generation_root(root, gen_id)
    gen.mkdir(parents=True, exist_ok=True)
    for rel in CANONICAL_TREES:
        _copy_entry(Path(work) / rel, gen / rel)
    return gen


def promote_generation(root: Path, gen_id: str) -> None:
    gen = generation_root(root, gen_id)
    if not gen.exists():
        raise TransactionError(f"generation {gen_id} does not exist")
    for rel in CANONICAL_TREES:
        overlay_tree(gen / rel, Path(root) / rel)


def canonical_fingerprint(root: Path) -> dict[str, str]:
    """sha256 of every file under the five canonical trees (stable relative paths)."""
    out: dict[str, str] = {}
    root = Path(root)
    for rel in CANONICAL_TREES:
        base = root / rel
        if not base.exists():
            out[rel] = "ABSENT"
            continue
        h = hashlib.sha256()
        if base.is_file():
            h.update(base.read_bytes())
            out[rel] = h.hexdigest()
            continue
        files = sorted(p for p in base.rglob("*") if p.is_file())
        for p in files:
            h.update(str(p.relative_to(base)).encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        out[rel] = h.hexdigest()
    return out


def ensure_live_matches_current(root: Path, *, repair: bool = False) -> dict:
    """Compare live canonical trees to the CURRENT generation snapshot.

    Used before pending publish. On mismatch, prefer CURRENT generation web/
    (see resolve_publish_web_dir). Optional repair re-promotes CURRENT.
    """
    root = Path(root)
    gen_id = read_current(root)
    if not gen_id:
        return {"ok": True, "reason": "no_current", "matched": True, "current": None}
    gen = generation_root(root, gen_id)
    if not gen.exists():
        return {
            "ok": False,
            "reason": "missing_generation",
            "matched": False,
            "current": gen_id,
        }
    live = canonical_fingerprint(root)
    expected = canonical_fingerprint(gen)
    matched = live == expected
    if not matched and repair:
        promote_generation(root, gen_id)
        live = canonical_fingerprint(root)
        matched = live == expected
    return {
        "ok": matched,
        "reason": "match" if matched else "mismatch",
        "matched": matched,
        "current": gen_id,
        "live": live,
        "expected": expected,
    }


def resolve_publish_web_dir(root: Path) -> Path:
    """Prefer CURRENT generation web/; fall back to live web/."""
    root = Path(root)
    gen_id = read_current(root)
    if gen_id:
        gen_web = generation_root(root, gen_id) / "web"
        if gen_web.exists() and (
            (gen_web / "data" / "meta.json").exists() or (gen_web / "index.html").exists()
        ):
            return gen_web
    return root / "web"


def materialize_from_current(root: Path) -> dict:
    """Retry live promote from the committed CURRENT generation. Never aborts CURRENT."""
    root = Path(root)
    gen_id = read_current(root)
    if not gen_id:
        raise TransactionError("no CURRENT generation to materialize")
    write_materialization_status(root, gen_id, MATERIALIZATION_PENDING)
    try:
        promote_generation(root, gen_id)
    except Exception as exc:
        payload = write_materialization_status(
            root, gen_id, MATERIALIZATION_FAILED, error=str(exc)
        )
        payload["committed"] = True
        payload["runStatus"] = "committed"
        return payload
    payload = write_materialization_status(root, gen_id, MATERIALIZATION_OK)
    payload["committed"] = True
    payload["runStatus"] = "committed"
    return payload


def commit_work(
    root: Path,
    work: Path,
    *,
    gen_id: str | None = None,
    fail_inject: str | None = None,
) -> dict:
    """Snapshot generation, flip CURRENT (commit), then materialize live trees.

    Failure *before* CURRENT raises TransactionError (not committed).
    Failure *after* CURRENT returns committed=True with materializationStatus
    failed/pending — never aborted, never rolls CURRENT back.
    """
    root = Path(root)
    work = Path(work)
    gen_id = gen_id or new_generation_id()
    fault = fail_inject or os.environ.get("FAULT_INJECT_COMMIT") or ""

    snapshot_generation(root, work, gen_id)

    if fault in {"before_pointer", "crash_before_pointer", "crash"}:
        raise TransactionError("FAULT_INJECT_COMMIT=before_pointer")

    write_current_pointer(root, gen_id)
    write_materialization_status(root, gen_id, MATERIALIZATION_PENDING)

    if fault in {"after_pointer", "crash_after_pointer"}:
        payload = write_materialization_status(
            root,
            gen_id,
            MATERIALIZATION_PENDING if fault == "crash_after_pointer" else MATERIALIZATION_FAILED,
            error=f"FAULT_INJECT_COMMIT={fault}",
        )
        payload["committed"] = True
        payload["runStatus"] = "committed"
        payload["generationId"] = gen_id
        return payload

    try:
        if fault == "promote":
            raise TransactionError("FAULT_INJECT_COMMIT=promote")
        promote_generation(root, gen_id)
    except Exception as exc:
        payload = write_materialization_status(
            root, gen_id, MATERIALIZATION_FAILED, error=str(exc)
        )
        payload["committed"] = True
        payload["runStatus"] = "committed"
        payload["generationId"] = gen_id
        return payload

    payload = write_materialization_status(root, gen_id, MATERIALIZATION_OK)
    payload["committed"] = True
    payload["runStatus"] = "committed"
    payload["generationId"] = gen_id
    return payload

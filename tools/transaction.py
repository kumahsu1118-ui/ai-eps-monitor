#!/usr/bin/env python3
"""True transaction boundary: stage → COMMIT (generations + CURRENT) or rollback.

Canonical trees (snapshot / revisions / daily / alerts / web) are copied into a
work root. All pipeline writes happen there. COMMIT copies the work trees into
data/generations/<id>/, atomically updates CURRENT, then promotes onto the live
paths. Failure before CURRENT leaves live data untouched. Failure after CURRENT
rolls CURRENT back and restores the previous generation.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_text
except Exception:  # pragma: no cover
    def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding=encoding)

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


class TransactionError(RuntimeError):
    pass


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
    return work


def write_current_pointer(root: Path, gen_id: str) -> None:
    atomic_write_text(current_pointer_path(root), gen_id + "\n")


def snapshot_generation(root: Path, work: Path, gen_id: str) -> Path:
    gen = generations_dir(root) / gen_id
    gen.mkdir(parents=True, exist_ok=True)
    for rel in CANONICAL_TREES:
        _copy_entry(Path(work) / rel, gen / rel)
    return gen


def promote_generation(root: Path, gen_id: str) -> None:
    gen = generations_dir(root) / gen_id
    if not gen.exists():
        raise TransactionError(f"generation {gen_id} does not exist")
    for rel in CANONICAL_TREES:
        overlay_tree(gen / rel, Path(root) / rel)


def rollback_to(root: Path, prev_id: str | None) -> None:
    """Restore CURRENT + canonical trees from prev_id (or leave CURRENT absent)."""
    if prev_id:
        write_current_pointer(root, prev_id)
        promote_generation(root, prev_id)
    else:
        p = current_pointer_path(root)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def commit_work(
    root: Path,
    work: Path,
    *,
    gen_id: str | None = None,
    fail_inject: str | None = None,
) -> str:
    """Write generation, atomically point CURRENT, promote. Rollback on failure."""
    root = Path(root)
    work = Path(work)
    gen_id = gen_id or new_generation_id()
    prev = read_current(root)
    fault = fail_inject or os.environ.get("FAULT_INJECT_COMMIT") or ""

    snapshot_generation(root, work, gen_id)

    if fault == "before_pointer":
        raise TransactionError("FAULT_INJECT_COMMIT=before_pointer")

    write_current_pointer(root, gen_id)

    if fault == "after_pointer":
        try:
            raise TransactionError("FAULT_INJECT_COMMIT=after_pointer")
        except TransactionError:
            rollback_to(root, prev)
            raise

    try:
        if fault == "promote":
            raise TransactionError("FAULT_INJECT_COMMIT=promote")
        promote_generation(root, gen_id)
    except Exception:
        rollback_to(root, prev)
        raise
    return gen_id


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

#!/usr/bin/env python3
"""Process lock (flock) + atomic JSON / JSONL writes for ai-eps-monitor.

All JSON serialization uses allow_nan=False. NaN / Inf are rejected everywhere.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

NONFINITE_STRINGS = {
    "nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity",
    "nan()", "inf()",
}


class NonFiniteNumberError(ValueError):
    """NaN or Infinity is not allowed in persisted JSON or numeric fields."""


def is_nonfinite_number(x) -> bool:
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, (int, float)):
        try:
            return math.isnan(float(x)) or math.isinf(float(x))
        except Exception:
            return False
    if isinstance(x, str):
        return x.strip().lower() in NONFINITE_STRINGS
    return False


def reject_nonfinite(x, field: str = "value") -> None:
    if is_nonfinite_number(x):
        raise NonFiniteNumberError(f"{field} is not finite: {x!r}")


def assert_finite_numbers(obj, path: str = "$") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_finite_numbers(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_finite_numbers(v, f"{path}[{i}]")
    else:
        reject_nonfinite(obj, field=path)


def dumps_json(obj, *, indent=2, sort_keys=False, separators=None, ensure_ascii=False) -> str:
    """json.dumps with allow_nan=False after walking for NaN/Inf."""
    assert_finite_numbers(obj)
    kwargs = {
        "ensure_ascii": ensure_ascii,
        "allow_nan": False,
        "sort_keys": sort_keys,
    }
    if indent is not None:
        kwargs["indent"] = indent
    if separators is not None:
        kwargs["separators"] = separators
    return json.dumps(obj, **kwargs)


class ProcessLock:
    """Advisory exclusive flock around a lockfile."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh and fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            if self._fh:
                self._fh.close()
                self._fh = None
        return False


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def _default_lock_path(path: Path) -> Path:
    """Keep lockfiles out of public web/data (use sibling .locks dir)."""
    path = Path(path)
    # Prefer project data/.locks when under .../web/data or .../data
    for parent in [path.parent, *path.parents]:
        if parent.name == "data" and (parent.parent / "tools").exists():
            locks = parent / ".locks"
            locks.mkdir(parents=True, exist_ok=True)
            return locks / (path.name + ".lock")
        if parent.name == "web" and (parent / "data").exists():
            # web/data → ROOT/data/.locks
            root_data = parent.parent / "data" / ".locks"
            root_data.mkdir(parents=True, exist_ok=True)
            return root_data / (path.name + ".lock")
    locks = path.parent / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    return locks / (path.name + ".lock")


def atomic_write_json(path: Path, obj, *, lock_path: Path | None = None) -> None:
    """JSON write via temp + atomic rename; optional process lock. allow_nan=False."""
    payload = dumps_json(obj, indent=2, ensure_ascii=False) + "\n"
    lock = Path(lock_path) if lock_path else _default_lock_path(Path(path))
    with ProcessLock(lock):
        atomic_write_text(path, payload)


def append_jsonl_atomic(path: Path, rows: list[dict], *, lock_path: Path | None = None) -> None:
    """Single-writer append to daily.jsonl under flock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(lock_path) if lock_path else _default_lock_path(path)
    with ProcessLock(lock):
        # Read-modify via temp rewrite of full file for true single-writer safety when appending batch
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
        buf = existing
        if buf and not buf.endswith("\n"):
            buf += "\n"
        for row in rows:
            buf += dumps_json(row, indent=None, ensure_ascii=False) + "\n"
        atomic_write_text(path, buf)


PIPELINE_LOCK_NAME = ".pipeline.lock"
RUN_IN_PROGRESS_MSG = "RUN ALREADY IN PROGRESS"


class GlobalPipelineLock:
    """Exclusive flock for Quality Gate → persist → alerts → export → publish.

    non_blocking=True: raise PipelineBusy (caller prints RUN ALREADY IN PROGRESS).
    non_blocking=False: wait for lock.
    """

    def __init__(self, lock_path: Path, *, non_blocking: bool = True):
        self.lock_path = Path(lock_path)
        self.non_blocking = non_blocking
        self._fh = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            flags = fcntl.LOCK_EX
            if self.non_blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(self._fh.fileno(), flags)
            except BlockingIOError as exc:
                self._fh.close()
                self._fh = None
                raise PipelineBusy(RUN_IN_PROGRESS_MSG) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()}\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh and fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            if self._fh:
                self._fh.close()
                self._fh = None
        return False


class PipelineBusy(RuntimeError):
    """Raised when global pipeline lock is held by another process."""


def default_pipeline_lock_path(root: Path) -> Path:
    return Path(root) / "data" / PIPELINE_LOCK_NAME


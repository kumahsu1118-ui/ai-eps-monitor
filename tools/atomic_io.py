#!/usr/bin/env python3
"""Exclusive flock + same-directory temp write + atomic rename.

JSON / snapshot persistence goes through these helpers so a crash mid-write
cannot leave a truncated public file. Pair writes keep meta.json and
dashboard.json from diverging on sitePublished / buildId.

JSONL append is rewrite+replace (fail-closed). No `open(..., "a")` fallback.
Global pipeline mutex: data/.pipeline.lock
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path


class JsonlAtomicError(RuntimeError):
    """JSONL atomic write failed — pipeline must abort (no fallback)."""


class PipelineLockedError(RuntimeError):
    """Another pipeline run holds data/.pipeline.lock."""


def _fsync_dir(dirpath: Path) -> None:
    fd = os.open(str(dirpath), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    """Write `data` to `path` using flock + fsync + os.replace. No non-atomic fallback."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_p = _lock_path(path)
    lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    tmp_name = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(path))
            tmp_name = None
            _fsync_dir(path.parent)
        except Exception:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path | str, obj, *, indent: int = 2) -> Path:
    return atomic_write_text(
        path, json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"
    )


def atomic_write_json_pair(
    path_a: Path | str,
    obj_a,
    path_b: Path | str,
    obj_b,
) -> tuple[Path, Path]:
    """Atomically persist two JSON files under one lock (same parent preferred).

    Both temps are fsynced before either os.replace. Used so sitePublished on
    meta.json and dashboard.json cannot diverge.
    """
    path_a = Path(path_a)
    path_b = Path(path_b)
    path_a.parent.mkdir(parents=True, exist_ok=True)
    path_b.parent.mkdir(parents=True, exist_ok=True)
    lock_p = _lock_path(path_a)
    lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    tmp_a = tmp_b = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        payload_a = (json.dumps(obj_a, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        payload_b = (json.dumps(obj_b, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        fd_a, tmp_a = tempfile.mkstemp(dir=str(path_a.parent), prefix=f".{path_a.name}.", suffix=".tmp")
        with os.fdopen(fd_a, "wb") as fh:
            fh.write(payload_a)
            fh.flush()
            os.fsync(fh.fileno())

        fd_b, tmp_b = tempfile.mkstemp(dir=str(path_b.parent), prefix=f".{path_b.name}.", suffix=".tmp")
        with os.fdopen(fd_b, "wb") as fh:
            fh.write(payload_b)
            fh.flush()
            os.fsync(fh.fileno())

        os.replace(tmp_a, str(path_a))
        tmp_a = None
        os.replace(tmp_b, str(path_b))
        tmp_b = None
        _fsync_dir(path_a.parent)
        if path_b.parent != path_a.parent:
            _fsync_dir(path_b.parent)
    except Exception:
        for tmp in (tmp_a, tmp_b):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path_a, path_b


def locked_append_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    """Atomically append by rewrite+replace under flock. Fail-closed, no `open('a')` fallback."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_p = _lock_path(path)
    lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    tmp_name = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = path.read_bytes() if path.exists() else b""
        payload = existing + text.encode(encoding)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(path))
            tmp_name = None
            _fsync_dir(path.parent)
        except Exception as exc:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise JsonlAtomicError(f"atomic jsonl append failed for {path}: {exc}") from exc
    except JsonlAtomicError:
        raise
    except Exception as exc:
        raise JsonlAtomicError(f"atomic jsonl append failed for {path}: {exc}") from exc
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path


def atomic_append_jsonl(path: Path | str, rows: list[dict], encoding: str = "utf-8") -> Path:
    """Append JSON objects as JSONL via atomic rewrite. Fail-closed, no fallback."""
    if not rows:
        return Path(path)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    return locked_append_text(path, text, encoding=encoding)


class PipelineLock:
    """Process-wide exclusive lock at data/.pipeline.lock (non-blocking)."""

    def __init__(self, lock_path: Path | str):
        self.path = Path(lock_path)
        self._fd: int | None = None

    def acquire(self, *, blocking: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise PipelineLockedError(f"pipeline already running ({self.path})") from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "PipelineLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def pipeline_lock_path(root: Path | str) -> Path:
    return Path(root) / "data" / ".pipeline.lock"


def acquire_global_pipeline_lock(root: Path | str, *, blocking: bool = False) -> PipelineLock:
    lock = PipelineLock(pipeline_lock_path(root))
    lock.acquire(blocking=blocking)
    return lock

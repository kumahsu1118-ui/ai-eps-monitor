#!/usr/bin/env python3
"""Exclusive flock + same-directory temp write + atomic rename.

Fail-closed: callers must NOT fall back to Path.write_text / non-atomic
truncating writes. If this module cannot complete a write, it raises and
the destination file is left untouched (or absent).
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path


class AtomicWriteError(OSError):
    """Raised when an atomic write cannot be completed. No non-atomic fallback."""


def _fsync_dir(dirpath: Path) -> None:
    fd = os.open(str(dirpath), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    """Write `data` to `path` using flock + fsync + os.replace.

    On any failure the original `path` is left unchanged (not truncated).
    There is no non-atomic fallback.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_p = _lock_path(path)
        lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    except Exception as exc:
        raise AtomicWriteError(f"atomic write setup failed for {path}: {exc}") from exc
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
        except Exception as exc:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                tmp_name = None
            raise AtomicWriteError(f"atomic write failed for {path}: {exc}") from exc
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def locked_append_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    """Append under an exclusive flock (jsonl / log). Fail-closed: raise on error."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_p = _lock_path(path)
        lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    except Exception as exc:
        raise AtomicWriteError(f"locked append setup failed for {path}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with path.open("a", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(path.parent)
    except Exception as exc:
        raise AtomicWriteError(f"locked append failed for {path}: {exc}") from exc
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path

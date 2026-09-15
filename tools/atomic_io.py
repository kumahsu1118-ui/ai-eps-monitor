#!/usr/bin/env python3
"""Exclusive flock + same-directory temp write + atomic rename.

All JSON / snapshot persistence in this project should go through these
helpers so a crash mid-write cannot leave a truncated public file.
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path


def _fsync_dir(dirpath: Path) -> None:
    fd = os.open(str(dirpath), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    """Write `data` to `path` using flock + fsync + os.replace."""
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


def locked_append_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    """Append under an exclusive flock (jsonl / log). Not a rename; flock serializes writers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_p = _lock_path(path)
    lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with path.open("a", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(path.parent)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return path

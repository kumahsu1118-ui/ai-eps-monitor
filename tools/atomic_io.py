#!/usr/bin/env python3
"""Exclusive flock + same-directory temp write + atomic rename.

JSON / snapshot persistence goes through these helpers so a crash mid-write
cannot leave a truncated public file. Pair writes keep meta.json and
dashboard.json from diverging on sitePublished / buildId.
"""
from __future__ import annotations

import fcntl
import json
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
    """Append under an exclusive flock (jsonl). No open(..., 'a') without lock."""
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

#!/usr/bin/env python3
"""Resolve the project root.

AI_EPS_ROOT always wins (staging / generations work roots). AIEPS_ROOT is
accepted as an alias so older callers keep working.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT_ENV_KEYS = ("AI_EPS_ROOT", "AIEPS_ROOT")


def env_root() -> Path | None:
    for key in ROOT_ENV_KEYS:
        val = os.environ.get(key)
        if val and str(val).strip():
            return Path(val).resolve()
    return None


def detect_root(start: Path | str | None = None) -> Path:
    env = env_root()
    if env is not None:
        return env
    cand = Path(start).resolve() if start else Path(__file__).resolve().parent
    for _ in range(8):
        has_tools_web = (cand / "tools").exists() and (cand / "web").exists()
        has_snapshots = (cand / "data" / "snapshots").exists()
        if has_snapshots or has_tools_web:
            if (cand / "data").exists() or (cand / "web").exists():
                return cand
        if cand.parent == cand:
            break
        cand = cand.parent
    here = Path(__file__).resolve().parent
    return here.parent


def export_root_env(root: Path) -> dict[str, str]:
    """Env mapping so child processes resolve the same root."""
    resolved = str(Path(root).resolve())
    return {"AI_EPS_ROOT": resolved, "AIEPS_ROOT": resolved}

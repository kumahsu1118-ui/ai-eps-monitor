#!/usr/bin/env python3
"""Resolve the project root. AIEPS_ROOT always wins (staging / generations work roots)."""
from __future__ import annotations

import os
from pathlib import Path


def detect_root(start: Path | str | None = None) -> Path:
    env = os.environ.get("AIEPS_ROOT")
    if env:
        return Path(env).resolve()
    cand = Path(start).resolve() if start else Path(__file__).resolve().parent
    for _ in range(8):
        if (cand / "data" / "snapshots").exists() or (cand / "tools").exists() and (cand / "web").exists():
            if (cand / "data").exists() or (cand / "web").exists():
                return cand
        if cand.parent == cand:
            break
        cand = cand.parent
    here = Path(__file__).resolve().parent
    return here.parent

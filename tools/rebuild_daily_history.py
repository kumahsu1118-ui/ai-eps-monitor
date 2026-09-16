#!/usr/bin/env python3
"""Rebuild runtime daily.jsonl from Git-tracked canonical EPS history.

Disaster-recovery / fresh-clone entrypoint. Equivalent to:

  python3 tools/canonical_eps_history.py --materialize

Does not invent observations. Fail-closed on malformed canonical rows
(existing runtime daily.jsonl is left unchanged). Does not mutate CURRENT.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import canonical_eps_history as ceh  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild data/daily_eps_snapshots/daily.jsonl from data/history/eps_daily (fail-closed)"
    )
    ap.add_argument("--root", type=Path, default=None, help="Project root (default: repo root)")
    ap.add_argument(
        "--lenient",
        action="store_true",
        help="Inspection/recovery only: skip malformed canonical rows instead of failing",
    )
    args = ap.parse_args(argv)
    argv_out = ["--materialize"]
    if args.root is not None:
        argv_out.extend(["--root", str(args.root)])
    if args.lenient:
        argv_out.append("--lenient")
    return ceh.main(argv_out)


if __name__ == "__main__":
    sys.exit(main())

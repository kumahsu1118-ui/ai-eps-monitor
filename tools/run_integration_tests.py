#!/usr/bin/env python3
"""Integration acceptance suite: subprocess / crash / publish / heavy ingest paths."""
from __future__ import annotations
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from run_acceptance_tests import main

if __name__ == "__main__":
    sys.exit(main(suite="integration", integration_timeout_s=180.0))

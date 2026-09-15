#!/usr/bin/env python3
"""Shim: canonical exporter lives at tools/export_web_data.py."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools" / "export_web_data.py"), run_name="__main__")

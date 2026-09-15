#!/usr/bin/env bash
# Publish public dashboard files for GitHub Pages.
# This repository serves Pages from the repo root. Source of truth is web/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$ROOT/tools/sync_pages_root.sh"

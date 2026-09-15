#!/usr/bin/env bash
# Sync already-exported web/ public files → site-repo → git push.
# Does NOT recompute: copies prebuilt web/ only (no exporter, no alert rebuild, no ingest).
# .data-version is written ONLY after a successful push (retry still pushes).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/home/box/.local/bin:$PATH"

python3 "$ROOT/tools/publish_prebuilt.py" "$ROOT"

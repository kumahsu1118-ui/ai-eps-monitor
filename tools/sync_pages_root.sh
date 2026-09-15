#!/usr/bin/env bash
# Copy exported web/ public files to the GitHub Pages root (this repo).
# Does NOT delete pipeline data/snapshots, data/revisions, etc.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/web"

python3 "$ROOT/tools/build_alerts.py"
python3 "$ROOT/tools/export_web_data.py"

mkdir -p "$ROOT/data"
cp -a "$WEB/index.html" "$WEB/styles.css" "$WEB/app.js" "$ROOT/"
cp -a "$WEB/index.html" "$ROOT/404.html"
touch "$ROOT/.nojekyll"

# Public JSON only — do not clobber pipeline subdirectories
for f in companies.json valuation.json revisions.json eps_history.json earnings.json alerts.json watchlist.json meta.json; do
  if [[ -f "$WEB/data/$f" ]]; then
    cp -a "$WEB/data/$f" "$ROOT/data/$f"
  fi
done

HASH=$(python3 - <<PY
import json
from pathlib import Path
meta = json.loads(Path("$ROOT/web/data/meta.json").read_text(encoding="utf-8"))
print(meta.get("dataVersion") or "")
PY
)
echo "$HASH" > "$ROOT/.data-version"
echo "Synced web/ → repo root for GitHub Pages (dataVersion=${HASH:0:12})"

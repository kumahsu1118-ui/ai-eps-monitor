#!/usr/bin/env bash
# Copy public SPA + JSON from web/ → repository root (GitHub Pages source).
# Does NOT delete pipeline dirs under data/ (snapshots, revisions, drivers, …).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${AI_EPS_ROOT:-$ROOT}"
WEB="$ROOT/web"
mkdir -p "$ROOT/data"
cp -a "$WEB/index.html" "$ROOT/index.html"
cp -a "$WEB/index.html" "$ROOT/404.html"
cp -a "$WEB/app.js" "$ROOT/app.js"
cp -a "$WEB/styles.css" "$ROOT/styles.css"
touch "$ROOT/.nojekyll"
if [[ -d "$WEB/data" ]]; then
  for f in "$WEB/data"/*.json; do
    [[ -f "$f" ]] || continue
    cp -a "$f" "$ROOT/data/$(basename "$f")"
  done
fi
echo "Synced web/ → $ROOT (Pages root)"

#!/usr/bin/env bash
# Copy web/ public files to repo root for GitHub Pages.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cp -a "$ROOT/web/index.html" "$ROOT/web/app.js" "$ROOT/web/styles.css" "$ROOT/"
cp -a "$ROOT/web/index.html" "$ROOT/404.html"
mkdir -p "$ROOT/data"
cp -a "$ROOT/web/data/"*.json "$ROOT/data/"
touch "$ROOT/.nojekyll"
echo "Synced web/ → repo root (Pages)."

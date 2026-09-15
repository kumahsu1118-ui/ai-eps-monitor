#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/ai-eps-monitor-review.zip}"
STAGE=$(mktemp -d)
DEST="$STAGE/ai-eps-monitor-review"
mkdir -p "$DEST"/{web/data,tools,data/{earnings,drivers,alerts,daily_eps_snapshots,revisions,snapshots},tests/fixtures,docs}

cp -a "$ROOT"/web/index.html "$ROOT"/web/app.js "$ROOT"/web/styles.css "$DEST/web/"
cp -a "$ROOT"/web/data/*.json "$DEST/web/data/" 2>/dev/null || true

for f in export_web_data.py build_alerts.py run_acceptance_tests.py publish_github_pages.sh sync_pages_root.sh freshness.py sa_parser.py; do
  cp -a "$ROOT/tools/$f" "$DEST/tools/" 2>/dev/null || true
done

cp -a "$ROOT"/data/earnings/*.json "$DEST/data/earnings/" 2>/dev/null || true
cp -a "$ROOT"/data/drivers/*.json "$DEST/data/drivers/" 2>/dev/null || true
cp -a "$ROOT"/data/alerts/*.json "$DEST/data/alerts/" 2>/dev/null || true
cp -a "$ROOT"/data/daily_eps_snapshots/*.jsonl "$DEST/data/daily_eps_snapshots/" 2>/dev/null || true
cp -a "$ROOT"/data/revisions/history.jsonl "$DEST/data/revisions/" 2>/dev/null || true
cp -a "$ROOT"/data/universe.json "$DEST/data/" 2>/dev/null || true
cp -a "$ROOT"/data/snapshots/*.json "$DEST/data/snapshots/" 2>/dev/null || true
rm -f "$DEST"/data/snapshots/raw_*.json "$DEST"/data/earnings/_raw_*.json

if [[ -d "$ROOT/tests/fixtures" ]]; then
  cp -a "$ROOT/tests/fixtures/." "$DEST/tests/fixtures/"
fi

for f in README.md TEST_RESULTS.md; do
  [[ -f "$ROOT/$f" ]] && cp -a "$ROOT/$f" "$DEST/"
done
cp -a "$ROOT"/docs/*.md "$DEST/docs/" 2>/dev/null || true

find "$DEST" -iname '*cookie*' -o -iname '*credential*' -o -iname '*session*' -o -iname '.env*' | while read -r p; do rm -rf "$p"; done
find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" ai-eps-monitor-review )
echo "Wrote $OUT ($(wc -c < "$OUT") bytes)"

#!/usr/bin/env bash
set -euo pipefail
ROOT=/workspace/ai-eps-monitor
OUT=/workspace/ai-eps-monitor-review.zip

# --- Same-build review package gate (Round 3) ---
META_WEB="$ROOT/web/data/meta.json"
META_SHOT="$ROOT/review-pack/meta-at-screenshot.json"
LIVE_META="$ROOT/review-pack/LIVE_META_FROM_BROWSER.txt"
if [[ ! -f "$META_SHOT" ]]; then
  echo "ERROR: missing $META_SHOT — capture screenshots AFTER final export/publish for THIS round" >&2
  exit 1
fi
if [[ ! -f "$LIVE_META" ]]; then
  echo "ERROR: missing $LIVE_META — record live browser meta for THIS round" >&2
  exit 1
fi
if [[ ! -f "$META_WEB" ]]; then
  echo "ERROR: missing $META_WEB — run export first" >&2
  exit 1
fi
python3 - <<'PYGATE'
import json, re, sys
from pathlib import Path
ROOT = Path("/workspace/ai-eps-monitor")
web = json.loads((ROOT / "web/data/meta.json").read_text(encoding="utf-8"))
shot = json.loads((ROOT / "review-pack/meta-at-screenshot.json").read_text(encoding="utf-8"))
live = (ROOT / "review-pack/LIVE_META_FROM_BROWSER.txt").read_text(encoding="utf-8")
dv = web.get("dataVersion")
sdv = shot.get("dataVersion")
m = re.search(r"dataVersion\s*=\s*([0-9a-fA-F]+)", live)
ldv = m.group(1) if m else None
if not dv or not sdv or not ldv:
    print("ERROR: dataVersion missing in web/shot/live meta", file=sys.stderr)
    sys.exit(1)
if dv != sdv or dv != ldv:
    print(f"ERROR: dataVersion mismatch web={dv[:16]}… shot={sdv[:16]}… live={ldv[:16]}…", file=sys.stderr)
    sys.exit(1)
print(f"Same-build gate OK dataVersion={dv[:16]}…")
PYGATE


# --- different_detail_screenshot gate (Final Reliability) ---
# 06-nvda-earnings-latest.png and 07-avgo-earnings-latest.png must be REAL distinct
# company earnings detail pages — identical SHA256 → review build FAIL.
python3 - <<'PYSHOT'
import hashlib, sys
from pathlib import Path
ROOT = Path("/workspace/ai-eps-monitor")
shots = ROOT / "review-pack/screenshots"
p4 = shots / "04-nvda-company-latest.png"
p5 = shots / "05-avgo-company-latest.png"
p6 = shots / "06-nvda-earnings-latest.png"
p7 = shots / "07-avgo-earnings-latest.png"
for p in (p4, p5, p6, p7):
    if not p.exists():
        print(f"ERROR: missing {p.name}", file=sys.stderr)
        sys.exit(1)
h4 = hashlib.sha256(p4.read_bytes()).hexdigest()
h5 = hashlib.sha256(p5.read_bytes()).hexdigest()
h6 = hashlib.sha256(p6.read_bytes()).hexdigest()
h7 = hashlib.sha256(p7.read_bytes()).hexdigest()
ok = True
if h4 == h6:
    print(f"ERROR: company_vs_earnings — 04==06 identical SHA {h4}", file=sys.stderr)
    ok = False
if h5 == h7:
    print(f"ERROR: company_vs_earnings — 05==07 identical SHA {h5}", file=sys.stderr)
    ok = False
if h6 == h7:
    print(f"ERROR: different_detail_screenshot — 06==07 identical SHA {h6}", file=sys.stderr)
    ok = False
if not ok:
    print("Capture REAL #/company/TICKER vs #/earnings/TICKER Earnings Detail (not renamed company shot).", file=sys.stderr)
    sys.exit(1)
print(f"company_vs_earnings_route_screenshot OK 04!=06 05!=07 06!=07")
print(f"  nvda_co={h4[:12]} nvda_earn={h6[:12]} avgo_co={h5[:12]} avgo_earn={h7[:12]}")
PYSHOT


STAGE=$(mktemp -d)
DEST="$STAGE/ai-eps-monitor-review"
mkdir -p "$DEST"/{web/data,tools,fixtures/parser,fixtures/data,data/{earnings,drivers,alerts,daily_eps_snapshots,revisions,snapshots},review-pack/screenshots,dashboard}

# Core web
cp -a "$ROOT"/web/index.html "$ROOT"/web/app.js "$ROOT"/web/styles.css "$DEST/web/"
cp -a "$ROOT"/web/data/*.json "$DEST/web/data/" 2>/dev/null || true

# Tools (no secrets)
for f in export_web_data.py build_alerts.py run_acceptance_tests.py sa_parser.py atomic_io.py snapshot_quality.py publish_github_pages.sh build_review_zip.sh generate_readme_review.py; do
  cp -a "$ROOT/tools/$f" "$DEST/tools/" 2>/dev/null || true
done

# Fixtures (self-contained tests)
if [[ -d "$ROOT/fixtures" ]]; then
  cp -a "$ROOT/fixtures/." "$DEST/fixtures/" 2>/dev/null || true
fi

# Persistent data needed for audit / tests
cp -a "$ROOT"/data/universe.json "$DEST/data/" 2>/dev/null || true
cp -a "$ROOT"/data/earnings/*.json "$DEST/data/earnings/" 2>/dev/null || true
cp -a "$ROOT"/data/drivers/*.json "$DEST/data/drivers/" 2>/dev/null || true
cp -a "$ROOT"/data/alerts/*.json "$DEST/data/alerts/" 2>/dev/null || true
cp -a "$ROOT"/data/daily_eps_snapshots/*.jsonl "$DEST/data/daily_eps_snapshots/" 2>/dev/null || true
cp -a "$ROOT"/data/revisions/history.jsonl "$DEST/data/revisions/" 2>/dev/null || true
# latest snapshot (sanitized production copy)
if ls "$ROOT"/data/snapshots/*.json >/dev/null 2>&1; then
  cp -a "$ROOT"/data/snapshots/*.json "$DEST/data/snapshots/" 2>/dev/null || true
fi

# Docs
for f in README_REVIEW.md TEST_RESULTS_FAILCLOSED_SIGNAL.md TEST_RESULTS_FINAL_RELIABILITY.md TEST_RESULTS_ROUND3.md CALENDAR_MAPPING_AUDIT.md UPDATE_PIPELINE.md README.md; do
  [[ -f "$ROOT/$f" ]] && cp -a "$ROOT/$f" "$DEST/"
done

# Screenshots + live meta note
cp -a "$ROOT"/review-pack/screenshots/*-latest.png "$DEST/review-pack/screenshots/" 2>/dev/null || true
[[ -f "$ROOT/review-pack/LIVE_META_FROM_BROWSER.txt" ]] && cp -a "$ROOT/review-pack/LIVE_META_FROM_BROWSER.txt" "$DEST/review-pack/"
[[ -f "$ROOT/review-pack/meta-at-screenshot.json" ]] && cp -a "$ROOT/review-pack/meta-at-screenshot.json" "$DEST/review-pack/"

( cd "$DEST/review-pack/screenshots" && sha256sum *-latest.png ) > "$DEST/review-pack/SCREENSHOT_SHA256.txt" 2>/dev/null || true

# Strip secrets
find "$DEST" \( -iname '*cookie*' -o -iname '*credential*' -o -iname '*session*' -o -iname '.env*' \) -print0 2>/dev/null | xargs -0r rm -rf

rm -f "$OUT"
if command -v zip >/dev/null 2>&1; then
  ( cd "$STAGE" && zip -qr "$OUT" ai-eps-monitor-review )
else
  python3 - <<PYZIP
import zipfile
from pathlib import Path
stage = Path("$STAGE")
out = Path("$OUT")
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for p in (stage / "ai-eps-monitor-review").rglob("*"):
        if p.is_file():
            zf.write(p, p.relative_to(stage).as_posix())
print("zipfile wrote", out)
PYZIP
fi
echo "Wrote $OUT ($(wc -c < "$OUT") bytes)"
cp -f "$OUT" "$ROOT/ai-eps-monitor-review.zip" 2>/dev/null || true

#!/usr/bin/env bash
# Sync exported web/ public files → site-repo → git push
# NO_CHANGES (exit 0) when public payload hash unchanged vs site-repo/.data-version
set -euo pipefail
ROOT=/workspace/ai-eps-monitor
WEB="$ROOT/web"
REPO="$ROOT/site-repo"
export PATH="/home/box/.local/bin:$PATH"

python3 "$ROOT/tools/build_alerts.py"
python3 "$ROOT/tools/export_web_data.py"

# Compute content hash of public payload (excludes meta publish-only stamps by hashing data files + app assets)
HASH=$(python3 - <<'PY'
import hashlib, json
from pathlib import Path
ROOT = Path("/workspace/ai-eps-monitor")
WEB = ROOT / "web"
h = hashlib.sha256()

def feed_bytes(b: bytes):
    h.update(len(b).to_bytes(8, "big"))
    h.update(b)

def feed_file(p: Path):
    feed_bytes(p.read_bytes())

# App shell
for name in ("index.html", "app.js", "styles.css"):
    feed_file(WEB / name)

# Prefer exporter dataVersion (canonical payload hash excluding publish stamps).
# Fall back to hashing data files with volatile fields stripped.
meta_path = WEB / "data" / "meta.json"
meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
if meta.get("dataVersion"):
    feed_bytes(str(meta["dataVersion"]).encode("utf-8"))
else:
    data_names = [
        "companies.json",
        "valuation.json",
        "revisions.json",
        "eps_history.json",
        "earnings.json",
        "watchlist.json",
    ]
    for name in data_names:
        p = WEB / "data" / name
        if p.exists():
            feed_file(p)
    # alerts: active list + alertEngineStatus (ignore lastEvaluated timestamps)
    ap = WEB / "data" / "alerts.json"
    if ap.exists():
        alerts = json.loads(ap.read_text(encoding="utf-8"))
        feed_bytes(json.dumps({
            "activeAlerts": alerts.get("activeAlerts") or alerts.get("alerts") or [],
            "alertEngineStatus": alerts.get("alertEngineStatus"),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8"))

print(h.hexdigest())
PY
)

VERSION_FILE="$REPO/.data-version"
PREV=""
if [[ -f "$VERSION_FILE" ]]; then
  PREV=$(tr -d '[:space:]' < "$VERSION_FILE" || true)
fi

if [[ -n "$PREV" && "$PREV" == "$HASH" ]]; then
  echo "NO_CHANGES"
  exit 0
fi

# Payload changed — stamp sitePublished atomically onto meta.json AND dashboard.json
python3 - <<PY
import sys
from pathlib import Path
ROOT = Path("/workspace/ai-eps-monitor")
if not (ROOT / "tools" / "publish_metadata.py").exists():
    ROOT = Path(__file__).resolve().parent.parent if False else Path("$ROOT")
sys.path.insert(0, str(ROOT / "tools"))
from publish_metadata import stamp_site_published
result = stamp_site_published(ROOT / "web" / "data")
print("stamped sitePublished", result.get("sitePublishedDisplay"), "buildId", result.get("buildId"))
PY

mkdir -p "$REPO/data"
cp -a "$WEB/index.html" "$WEB/styles.css" "$WEB/app.js" "$REPO/"
cp -a "$WEB/data/." "$REPO/data/"
cp "$REPO/index.html" "$REPO/404.html"
touch "$REPO/.nojekyll"
echo "$HASH" > "$VERSION_FILE"
rm -f "$REPO/ORIGIN.txt" "$REPO/overview-verify.png"

cd "$REPO"
gh auth setup-git >/dev/null
git add -A
if git diff --cached --quiet; then
  echo "NO_CHANGES"
  exit 0
fi
git -c user.email="kumahsu1118-ui@users.noreply.github.com" -c user.name="AI EPS Monitor" commit -m "Update dashboard data $(date -u +%Y-%m-%dT%H:%MZ)"
git push origin main
echo "PUSHED hash=${HASH:0:12}"
gh api "repos/kumahsu1118-ui/ai-eps-monitor/pages" -q .html_url 2>/dev/null || true

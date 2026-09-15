#!/usr/bin/env bash
# Sync exported web/ public files → site-repo → git push
# NO_CHANGES (exit 0) when public payload hash unchanged vs site-repo/.data-version
#
# Pipeline order (do NOT run build_alerts before the quality gate):
#   Collection → Quality Gate → publishable → persist → Alert Engine → Export → Publish
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/web"
REPO="$ROOT/site-repo"
export PATH="/home/box/.local/bin:${PATH:-}"

# Integrity pipeline: lock → mixed-build check → quality gate → persist/daily →
# Alert Engine → export. Rejected snapshots never mutate Alert DB.
python3 "$ROOT/tools/pipeline.py"

# Compute content hash of public payload (excludes meta publish-only stamps by hashing data files + app assets)
HASH=$(python3 - <<PY
import hashlib, json
from pathlib import Path
ROOT = Path("$ROOT")
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

# Payload changed — stamp sitePublished + ensure dataVersion/buildId in meta, then commit
python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
meta_path = Path("$ROOT") / "web" / "data" / "meta.json"
meta = json.loads(meta_path.read_text(encoding="utf-8"))
now = datetime.now(timezone(timedelta(hours=8)))
utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
display = f"{months[now.month-1]} {now.day}, {now.year} {now.hour:02d}:{now.minute:02d} Taipei Time"
meta["sitePublished"] = utc
meta["sitePublishedDisplay"] = display
# Do not write latestSuccessfulRefresh as a primary field
meta.pop("latestSuccessfulRefresh", None)
meta.pop("siteRepoCommit", None)
if not meta.get("dataVersion"):
    meta["dataVersion"] = "$HASH"
    meta["buildId"] = "$HASH"
meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("stamped sitePublished", display)
PY

mkdir -p "$REPO/data"
cp -a "$WEB/index.html" "$WEB/styles.css" "$WEB/app.js" "$REPO/"
cp -a "$WEB/data/." "$REPO/data/"
# Never publish pipeline lockfiles
rm -f "$REPO/data/.pipeline.lock" "$REPO/data/"*.lock "$REPO/"*.lock 2>/dev/null || true
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

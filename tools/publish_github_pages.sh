#!/usr/bin/env bash
# Sync exported web/ public files → site-repo → git push
# Second line of defense: abort if qualityGate.publishable != true
# NO_CHANGES (exit 0) when public payload hash (dataVersion+refreshVersion) unchanged
set -euo pipefail
ROOT=/workspace/ai-eps-monitor
LOCK="$ROOT/data/.pipeline.lock"
mkdir -p "$ROOT/data"
# Hold exclusive flock for Quality Gate → export → publish (fd 9)
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "RUN ALREADY IN PROGRESS" >&2
  exit 2
fi
export PIPELINE_LOCK_HELD=1
WEB="$ROOT/web"
REPO="$ROOT/site-repo"
export PATH="/home/box/.local/bin:$PATH"

# Alert evaluation is post-gate only via export_web_data.run_build_alerts()
set +e
python3 "$ROOT/tools/export_web_data.py"
EXP_RC=$?
set -e
if [[ "$EXP_RC" -ne 0 ]]; then
  echo "ERROR: export_web_data.py exited $EXP_RC — abort publish (fail-closed)" >&2
  exit "$EXP_RC"
fi

# Second line of defense: qualityGate.publishable must be true
python3 - <<'PYGATE'
import json, sys
from pathlib import Path
meta_path = Path("/workspace/ai-eps-monitor/web/data/meta.json")
if not meta_path.exists():
    print("ERROR: meta.json missing after export — abort publish", file=sys.stderr)
    sys.exit(1)
meta = json.loads(meta_path.read_text(encoding="utf-8"))
qg = meta.get("qualityGate") or {}
if qg.get("publishable") is not True:
    print(
        f"ERROR: qualityGate.publishable!=true status={qg.get('status')} "
        f"reason={qg.get('reason')} — abort publish (no git push)",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"qualityGate OK status={qg.get('status')} publishable=true")
PYGATE

# Compute content hash: dataVersion + refreshVersion (metadata-only commit OK)
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

meta_path = WEB / "data" / "meta.json"
meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
# Prefer dataVersion + refreshVersion so collection heartbeat can publish
# even when substantive EPS payload is unchanged.
if meta.get("dataVersion") or meta.get("refreshVersion"):
    feed_bytes(str(meta.get("dataVersion") or "").encode("utf-8"))
    feed_bytes(b"|")
    feed_bytes(str(meta.get("refreshVersion") or "").encode("utf-8"))
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
meta_path = Path("/workspace/ai-eps-monitor/web/data/meta.json")
meta = json.loads(meta_path.read_text(encoding="utf-8"))
now = datetime.now(timezone(timedelta(hours=8)))
utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
display = f"{months[now.month-1]} {now.day}, {now.year} {now.hour:02d}:{now.minute:02d} Taipei Time"
meta["sitePublished"] = utc
meta["sitePublishedDisplay"] = display
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

#!/usr/bin/env bash
# Sync exported web/ public files → site-repo → git push
# Second line of defense: abort if qualityGate.publishable != true
# NO_CHANGES (exit 0) when public payload hash (dataVersion+refreshVersion) unchanged
#
# PIPELINE_LOCK_HELD=1: parent already holds data/.pipeline.lock — skip re-flock.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AIEPS_ROOT="$ROOT"
LOCK="$ROOT/data/.pipeline.lock"
mkdir -p "$ROOT/data"

VALIDATE_ONLY=0
for arg in "$@"; do
  if [[ "$arg" == "--validate-only" ]]; then
    VALIDATE_ONLY=1
  fi
done

if [[ "${PIPELINE_LOCK_HELD:-0}" == "1" ]]; then
  echo "skip re-flock (PIPELINE_LOCK_HELD=1)"
else
  # Hold exclusive flock for Quality Gate → export → publish (fd 9)
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "RUN ALREADY IN PROGRESS" >&2
    exit 2
  fi
  export PIPELINE_LOCK_HELD=1
fi
WEB="$ROOT/web"
REPO="$ROOT/site-repo"
export PATH="/home/box/.local/bin:${PATH:-}"

# Alert evaluation is post-gate only via export_web_data.run_build_alerts()
# SKIP_EXPORT=1 / PUBLISH_PREBUILT=1: publish already-exported web/ (ingest --publish)
if [[ "${SKIP_EXPORT:-}" == "1" || "${PUBLISH_PREBUILT:-}" == "1" ]]; then
  echo "publish_prebuilt_site: skipping export (prebuilt web/)"
else
  set +e
  python3 "$ROOT/tools/export_web_data.py"
  EXP_RC=$?
  set -e
  if [[ "$EXP_RC" -ne 0 ]]; then
    echo "ERROR: export_web_data.py exited $EXP_RC — abort publish (fail-closed)" >&2
    exit "$EXP_RC"
  fi
fi

# Second line of defense: qualityGate.publishable must be true
python3 - <<PYGATE
import json, os, sys
from pathlib import Path
meta_path = Path(os.environ["AIEPS_ROOT"]) / "web" / "data" / "meta.json"
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

if [[ "${VALIDATE_ONLY:-0}" == "1" ]]; then
  echo "VALIDATE_ONLY — no git commit / push"
  exit 0
fi
if [[ "${SKIP_GIT_PUSH:-}" == "1" ]]; then
  echo "SKIP_GIT_PUSH=1 — publish checks passed, not committing"
  exit 0
fi
HASH=$(python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
ROOT = Path(os.environ["AIEPS_ROOT"])
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

# Payload changed — stamp sitePublished + atomically sync meta.json AND dashboard.json.meta
EXPORT_HASH="$HASH" python3 - <<'PYSTAMP'
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(os.environ["AIEPS_ROOT"]) / "tools"))
from atomic_io import atomic_write_json

root = Path(os.environ["AIEPS_ROOT"])
meta_path = root / "web" / "data" / "meta.json"
dash_path = root / "web" / "data" / "dashboard.json"
meta = json.loads(meta_path.read_text(encoding="utf-8"))
now = datetime.now(timezone(timedelta(hours=8)))
utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
display = f"{months[now.month-1]} {now.day}, {now.year} {now.hour:02d}:{now.minute:02d} Taipei Time"
meta["sitePublished"] = utc
meta["sitePublishedDisplay"] = display
meta.pop("latestSuccessfulRefresh", None)
meta.pop("siteRepoCommit", None)
hash_val = os.environ.get("EXPORT_HASH") or ""
if not meta.get("dataVersion") and hash_val:
    meta["dataVersion"] = hash_val
    meta["buildId"] = hash_val
# Atomic sync: meta.json AND dashboard.json.meta must match (frontend prefers dashboard)
atomic_write_json(meta_path, meta)
if dash_path.exists():
    dash = json.loads(dash_path.read_text(encoding="utf-8"))
    if not isinstance(dash, dict):
        dash = {}
    dash_meta = dict(dash.get("meta") or {})
    for k in (
        "sitePublished", "sitePublishedDisplay", "dataVersion", "refreshVersion",
        "buildId", "lastSuccessfulCollection", "lastSuccessfulCollectionDisplay",
        "consensusDataAsOf", "consensusDataAsOfDisplay", "collectionStatus",
        "collectionStatusLabel", "alertEngineStatus", "qualityGate",
    ):
        if k in meta:
            dash_meta[k] = meta[k]
    dash["meta"] = dash_meta
    if meta.get("buildId"):
        dash["buildId"] = meta["buildId"]
    atomic_write_json(dash_path, dash)
print("stamped sitePublished", display, "(meta.json + dashboard.json.meta synced)")
PYSTAMP

mkdir -p "$REPO/data"
cp -a "$WEB/index.html" "$WEB/styles.css" "$WEB/app.js" "$REPO/"
cp -a "$WEB/data/." "$REPO/data/"
cp "$REPO/index.html" "$REPO/404.html"
touch "$REPO/.nojekyll"
rm -f "$REPO/ORIGIN.txt" "$REPO/overview-verify.png"
# Do NOT write .data-version before push (push-fail retry must still push)

cd "$REPO"
gh auth setup-git >/dev/null
git add -A
NEED_COMMIT=1
if git diff --cached --quiet; then
  NEED_COMMIT=0
fi

# Recovery: if local HEAD is ahead of origin/main, we still need to push
AHEAD=0
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
    # Count commits not in origin
    AHEAD_N=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    if [[ "${AHEAD_N:-0}" -gt 0 ]]; then
      AHEAD=1
    fi
  fi
fi

if [[ "$NEED_COMMIT" -eq 0 && "$AHEAD" -eq 0 ]]; then
  # Truly nothing to publish — only then treat as NO_CHANGES
  # Still allow when HASH differs from VERSION_FILE but working tree empty after prior failed push? handled by AHEAD
  if [[ -n "$PREV" && "$PREV" == "$HASH" ]]; then
    echo "NO_CHANGES"
    exit 0
  fi
  # HASH changed (e.g. sitePublished stamp) but git sees no diff — force-update a stamp file
  echo "$HASH" > "$REPO/.publish-hash-stamp"
  git add -A
  if git diff --cached --quiet; then
    echo "NO_CHANGES"
    exit 0
  fi
  NEED_COMMIT=1
fi

if [[ "$NEED_COMMIT" -eq 1 ]]; then
  git -c user.email="kumahsu1118-ui@users.noreply.github.com" -c user.name="AI EPS Monitor" commit -m "Update dashboard data $(date -u +%Y-%m-%dT%H:%MZ)" || true
fi

set +e
git push origin main
PUSH_RC=$?
set -e
if [[ "$PUSH_RC" -ne 0 ]]; then
  echo "ERROR: git push failed (rc=$PUSH_RC) — .data-version NOT updated; retry will still push" >&2
  exit "$PUSH_RC"
fi
# Update .data-version only AFTER successful push
echo "$HASH" > "$VERSION_FILE"
echo "PUSHED hash=${HASH:0:12}"
gh api "repos/kumahsu1118-ui/ai-eps-monitor/pages" -q .html_url 2>/dev/null || true

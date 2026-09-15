#!/usr/bin/env bash
# Sync FULL public web/ asset tree → site-repo → git push
# Second line of defense: abort if qualityGate.publishable != true
# NO_CHANGES (exit 0) when public payload hash unchanged
# ROOT via AI_EPS_ROOT or script-relative project root (never hard-require /workspace/...)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AI_EPS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOCK="$ROOT/data/.pipeline.lock"
mkdir -p "$ROOT/data"
# Nested pipeline: if parent ingest already holds data/.pipeline.lock, do NOT re-acquire.
if [[ "${PIPELINE_LOCK_HELD:-}" == "1" ]]; then
  echo "pipeline lock already held by parent — skip re-acquire"
else
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "RUN ALREADY IN PROGRESS" >&2
    exit 2
  fi
  export PIPELINE_LOCK_HELD=1
fi
WEB="$ROOT/web"
REPO="$ROOT/site-repo"
export PATH="/home/box/.local/bin:$PATH"
export AI_EPS_ROOT="$ROOT"

# Publish-only: NEVER recompute / mutate financial history (ingest is sole writer).
# SKIP_EXPORT / PUBLISH_PREBUILT kept for back-compat; export path removed.
echo "publish-only: no export / no history mutation (ingest_snapshot.py is sole writer)"
if [[ "${ALLOW_PUBLISH_EXPORT:-}" == "1" ]]; then
  echo "ERROR: ALLOW_PUBLISH_EXPORT is banned — publish must not recompute" >&2
  exit 2
fi

# Stamp cache-bust THEN finalize appVersion/releaseVersion so meta matches final asset tree
python3 - <<PYSTAMP
import sys
from pathlib import Path
sys.path.insert(0, "${ROOT}/tools")
from ingest_snapshot import finalize_release_identity, ensure_live_matches_current, rebind_paths
root = Path("${ROOT}")
rebind_paths(root)
ensure_live_matches_current()
meta = finalize_release_identity(root / "web")
print("cache-bust + release identity finalized appVersion=", (meta or {}).get("appVersion", "")[:16])
PYSTAMP

# Second line of defense: qualityGate.publishable must be true
python3 - <<PYGATE
import json, sys
from pathlib import Path
meta_path = Path("${ROOT}") / "web" / "data" / "meta.json"
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

# Compute content hash: full static tree (incl vendor) + dataVersion + refreshVersion
HASH=$(AI_EPS_ROOT="$ROOT" python3 - <<'PY'
import hashlib, json, os, re, sys
from pathlib import Path
ROOT = Path(os.environ["AI_EPS_ROOT"])
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT / "tools"))
from ingest_snapshot import iter_frontend_static_files, list_index_local_assets

h = hashlib.sha256()

def feed_bytes(b: bytes):
    h.update(len(b).to_bytes(8, "big"))
    h.update(b)

def feed_file(p: Path):
    feed_bytes(p.read_bytes())

# Full frontend static deps including vendor/
for p in iter_frontend_static_files(WEB):
    feed_bytes(str(p.relative_to(WEB)).encode("utf-8"))
    feed_file(p)

meta_path = WEB / "data" / "meta.json"
meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
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
EXPORT_HASH="$HASH" AI_EPS_ROOT="$ROOT" python3 - <<'PYSTAMP'
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
ROOT = Path(os.environ["AI_EPS_ROOT"])
sys.path.insert(0, str(ROOT / "tools"))
from atomic_io import atomic_write_json

meta_path = ROOT / "web" / "data" / "meta.json"
dash_path = ROOT / "web" / "data" / "dashboard.json"
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
        "appVersion", "releaseVersion", "schemaVersion",
    ):
        if k in meta:
            dash_meta[k] = meta[k]
    dash["meta"] = dash_meta
    if meta.get("buildId"):
        dash["buildId"] = meta["buildId"]
    atomic_write_json(dash_path, dash)
marker = ROOT / "data" / ".last-publish-web"
marker.write_text(str((ROOT / "web").resolve()) + "\n", encoding="utf-8")
print("stamped sitePublished", display, "(meta.json + dashboard.json.meta synced)")
PYSTAMP

# Sync FULL public asset tree: index/app/styles/vendor/**/data — never touch .git
mkdir -p "$REPO/data" "$REPO/vendor"
AI_EPS_ROOT="$ROOT" python3 - <<'PYCOPY'
import os, shutil, sys
from pathlib import Path
ROOT = Path(os.environ["AI_EPS_ROOT"])
WEB, REPO = ROOT / "web", ROOT / "site-repo"
sys.path.insert(0, str(ROOT / "tools"))
from ingest_snapshot import iter_frontend_static_files, list_index_local_assets
REPO.mkdir(parents=True, exist_ok=True)
# Copy every frontend static dep (index, app, styles, vendor/**, future assets)
for p in iter_frontend_static_files(WEB):
    rel = p.relative_to(WEB)
    dest = REPO / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)
# Explicit shell copies (belt-and-suspenders)
for name in ("index.html", "app.js", "styles.css"):
    src = WEB / name
    if src.exists():
        shutil.copy2(src, REPO / name)
# vendor tree
vendor_src = WEB / "vendor"
if vendor_src.is_dir():
    for src in vendor_src.rglob("*"):
        if src.is_file():
            dest = REPO / "vendor" / src.relative_to(vendor_src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
# data tree
data_src, data_dst = WEB / "data", REPO / "data"
data_dst.mkdir(parents=True, exist_ok=True)
if data_src.is_dir():
    for src in data_src.rglob("*"):
        if src.is_file():
            dest = data_dst / src.relative_to(data_src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
missing = [rel for rel in list_index_local_assets(WEB / "index.html") if not (REPO / rel).is_file()]
if missing:
    raise SystemExit(f"ERROR: clean publish missing index assets: {missing}")
print("copied full static tree + data")
PYCOPY

# Final assert: every index local asset present in REPO
AI_EPS_ROOT="$ROOT" python3 - <<'PYASSERT'
import os, sys
from pathlib import Path
ROOT = Path(os.environ["AI_EPS_ROOT"])
sys.path.insert(0, str(ROOT / "tools"))
from ingest_snapshot import list_index_local_assets
REPO = ROOT / "site-repo"
missing = [rel for rel in list_index_local_assets(ROOT / "web" / "index.html") if not (REPO / rel).is_file()]
# Also accept query-stripped paths from stamped index in REPO
if missing:
    # try parsing REPO index
    missing2 = [rel for rel in list_index_local_assets(REPO / "index.html") if not (REPO / rel).is_file()]
    if missing2:
        print(f"ERROR: clean publish missing index assets: {missing2}", file=sys.stderr)
        sys.exit(1)
print("clean_publish_contains_all_index_assets OK")
PYASSERT

cp "$REPO/index.html" "$REPO/404.html"
touch "$REPO/.nojekyll"
rm -f "$REPO/ORIGIN.txt" "$REPO/overview-verify.png"

cd "$REPO"
# Skip git push when site-repo is not a git repo (fixture / clean review)
if [[ ! -d "$REPO/.git" ]]; then
  echo "$HASH" > "$VERSION_FILE"
  echo "PUBLISH_LOCAL hash=${HASH:0:12} (no .git — skipped push)"
  exit 0
fi

gh auth setup-git >/dev/null 2>&1 || true
git add -A
NEED_COMMIT=1
if git diff --cached --quiet; then
  NEED_COMMIT=0
fi

AHEAD=0
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
    AHEAD_N=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    if [[ "${AHEAD_N:-0}" -gt 0 ]]; then
      AHEAD=1
    fi
  fi
fi

if [[ "$NEED_COMMIT" -eq 0 && "$AHEAD" -eq 0 ]]; then
  if [[ -n "$PREV" && "$PREV" == "$HASH" ]]; then
    echo "NO_CHANGES"
    exit 0
  fi
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
echo "$HASH" > "$VERSION_FILE"
echo "PUSHED hash=${HASH:0:12}"
gh api "repos/kumahsu1118-ui/ai-eps-monitor/pages" -q .html_url 2>/dev/null || true

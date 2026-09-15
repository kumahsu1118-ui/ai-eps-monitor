#!/usr/bin/env bash
# Publish-only GitHub Pages sync (ingest is the sole pipeline writer).
#
# Default: publish already-exported CURRENT generation web/; do not re-export
# or mutate snapshots/revisions/daily/alerts. Pass --legacy-mutate to re-export.
#
# AI_EPS_ROOT / AIEPS_ROOT: configurable project root (fixture / work root).
# PIPELINE_LOCK_HELD=1: parent already holds data/.pipeline.lock — skip re-flock.
# Pending publish: data/.pending-publish retries after a failed push, always
# from CURRENT generation web/ after ensure_live_matches_current.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${AI_EPS_ROOT:-}" ]]; then
  ROOT="$AI_EPS_ROOT"
elif [[ -n "${AIEPS_ROOT:-}" ]]; then
  ROOT="$AIEPS_ROOT"
else
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
ROOT="$(cd "$ROOT" && pwd)"
export AI_EPS_ROOT="$ROOT"
export AIEPS_ROOT="$ROOT"

LOCK="$ROOT/data/.pipeline.lock"
PENDING="$ROOT/data/.pending-publish"
mkdir -p "$ROOT/data"

LEGACY_MUTATE=0
VALIDATE_ONLY=0
for arg in "$@"; do
  if [[ "$arg" == "--legacy-mutate" ]]; then
    LEGACY_MUTATE=1
  fi
  if [[ "$arg" == "--validate-only" ]]; then
    VALIDATE_ONLY=1
  fi
done
if [[ "${LEGACY_MUTATE_ENV:-}" == "1" ]]; then
  LEGACY_MUTATE=1
fi

if [[ "${PIPELINE_LOCK_HELD:-0}" == "1" ]]; then
  echo "skip re-flock (PIPELINE_LOCK_HELD=1)"
else
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "RUN ALREADY IN PROGRESS" >&2
    exit 2
  fi
  export PIPELINE_LOCK_HELD=1
fi

export PATH="/home/box/.local/bin:${PATH:-}"
REPO="${SITE_REPO:-$ROOT/site-repo}"

# Publish-only unless --legacy-mutate explicitly re-enables export.
if [[ "$LEGACY_MUTATE" -eq 1 && "${SKIP_EXPORT:-}" != "1" && "${PUBLISH_PREBUILT:-}" != "1" ]]; then
  set +e
  python3 "$ROOT/tools/export_web_data.py" --legacy-mutate
  EXP_RC=$?
  set -e
  if [[ "$EXP_RC" -ne 0 ]]; then
    echo "ERROR: export_web_data.py exited $EXP_RC — abort publish (fail-closed)" >&2
    exit "$EXP_RC"
  fi
else
  echo "publish-only: skipping export (pass --legacy-mutate to re-export)"
  export SKIP_EXPORT=1
  export PUBLISH_PREBUILT=1
fi

# Prefer CURRENT generation web/; ensure live matches CURRENT before pending retry.
eval "$(python3 - <<'PYWEB'
import os, sys
from pathlib import Path
root = Path(os.environ["AI_EPS_ROOT"])
sys.path.insert(0, str(root / "tools"))
from transaction import ensure_live_matches_current, resolve_publish_web_dir, read_current
from atomic_io import has_pending_publish
pending = has_pending_publish(root)
status = ensure_live_matches_current(root, repair=False)
web = resolve_publish_web_dir(root)
current = read_current(root) or ""
print(f"export PUBLISH_WEB={web}")
print(f"export PUBLISH_CURRENT={current}")
print(f"export LIVE_MATCHES_CURRENT={'1' if status.get('matched') else '0'}")
print(f"export PENDING_PUBLISH={'1' if pending else '0'}")
if pending:
    print("pending publish retry: using CURRENT generation web/", file=sys.stderr)
print(f"publish web={web} current={current or 'none'} live_matches={status.get('matched')} pending={pending}", file=sys.stderr)
PYWEB
)"

WEB="${PUBLISH_WEB:-$ROOT/web}"
export PUBLISH_WEB="$WEB"

python3 - <<'PYGATE'
import json, os, sys
from pathlib import Path
web = Path(os.environ.get("PUBLISH_WEB") or (Path(os.environ["AI_EPS_ROOT"]) / "web"))
meta_path = web / "data" / "meta.json"
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
print(f"qualityGate OK status={qg.get('status')} publishable=true web={web}")
PYGATE

if [[ "${VALIDATE_ONLY:-0}" == "1" ]]; then
  echo "VALIDATE_ONLY — no git commit / push"
  exit 0
fi

HASH=$(python3 - <<'PY'
import os, sys
from pathlib import Path
web = Path(os.environ.get("PUBLISH_WEB") or (Path(os.environ["AI_EPS_ROOT"]) / "web"))
sys.path.insert(0, str(Path(os.environ["AI_EPS_ROOT"]) / "tools"))
from static_publish import compute_publish_hash
print(compute_publish_hash(web))
PY
)
export EXPORT_HASH="$HASH"

VERSION_FILE="$REPO/.data-version"
PREV=""
if [[ -f "$VERSION_FILE" ]]; then
  PREV=$(tr -d '[:space:]' < "$VERSION_FILE" || true)
fi

PENDING_EXISTS=0
if [[ -f "$PENDING" ]]; then
  PENDING_EXISTS=1
  echo "pending publish retry: $PENDING (CURRENT gen web=$WEB)"
fi

if [[ -n "$PREV" && "$PREV" == "$HASH" && "$PENDING_EXISTS" -eq 0 && "${FORCE_PUBLISH:-0}" != "1" ]]; then
  echo "NO_CHANGES"
  exit 0
fi

# Stamp sitePublished on the *publish source* (CURRENT gen web when present).
EXPORT_HASH="$HASH" python3 - <<'PYSTAMP'
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(os.environ["AI_EPS_ROOT"]) / "tools"))
from atomic_io import atomic_write_json
from static_publish import compute_app_version, compute_release_version

web = Path(os.environ.get("PUBLISH_WEB") or (Path(os.environ["AI_EPS_ROOT"]) / "web"))
meta_path = web / "data" / "meta.json"
dash_path = web / "data" / "dashboard.json"
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
app_v = compute_app_version(web)
rel_v = compute_release_version(
    web,
    app_version=app_v,
    data_version=meta.get("dataVersion"),
    refresh_version=meta.get("refreshVersion"),
)
meta["appVersion"] = app_v
meta["releaseVersion"] = rel_v
meta["schemaVersion"] = meta.get("schemaVersion") or "1"
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
marker = Path(os.environ["AI_EPS_ROOT"]) / "data" / ".last-publish-web"
marker.write_text(str(web.resolve()) + "\n", encoding="utf-8")
print("stamped sitePublished", display, "appVersion", app_v[:12], "web", web)
PYSTAMP

python3 - <<'PYCOPY'
import os, sys
from pathlib import Path
root = Path(os.environ["AI_EPS_ROOT"])
sys.path.insert(0, str(root / "tools"))
from static_publish import copy_static_tree, parse_index_assets
web = Path(os.environ.get("PUBLISH_WEB") or (root / "web"))
repo = Path(os.environ.get("SITE_REPO") or (root / "site-repo"))
copied = copy_static_tree(web, repo)
index = (web / "index.html").read_text(encoding="utf-8") if (web / "index.html").exists() else ""
assets = parse_index_assets(index)
missing = [a for a in assets if not (repo / a).exists()]
if missing:
    print("ERROR: published tree missing index assets:", missing, file=sys.stderr)
    sys.exit(1)
print("copied static tree files=", len(copied), "indexAssets=", assets, "from", web)
PYCOPY

if [[ "${FAULT_INJECT_PUSH:-}" == "1" ]]; then
  python3 - <<'PYPEND'
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["AI_EPS_ROOT"]) / "tools"))
from atomic_io import write_pending_publish
from transaction import read_current
write_pending_publish(Path(os.environ["AI_EPS_ROOT"]), {
    "reason": "FAULT_INJECT_PUSH",
    "hash": os.environ.get("EXPORT_HASH"),
    "current": read_current(Path(os.environ["AI_EPS_ROOT"])),
    "web": os.environ.get("PUBLISH_WEB"),
})
print("ERROR: FAULT_INJECT_PUSH=1 — pending publish written", file=sys.stderr)
sys.exit(1)
PYPEND
fi

if [[ "${SKIP_GIT_PUSH:-}" == "1" ]]; then
  rm -f "$PENDING"
  printf '%s\n' "$HASH" > "$VERSION_FILE"
  echo "SKIP_GIT_PUSH=1 — static tree published locally hash=${HASH:0:12} web=$WEB"
  exit 0
fi

cd "$REPO"
gh auth setup-git >/dev/null
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

if [[ "$NEED_COMMIT" -eq 0 && "$AHEAD" -eq 0 && "$PENDING_EXISTS" -eq 0 ]]; then
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
  PUSH_RC="$PUSH_RC" python3 - <<'PYPEND'
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["AI_EPS_ROOT"]) / "tools"))
from atomic_io import write_pending_publish
from transaction import read_current
write_pending_publish(Path(os.environ["AI_EPS_ROOT"]), {
    "reason": "git_push_failed",
    "rc": int(os.environ.get("PUSH_RC") or 1),
    "current": read_current(Path(os.environ["AI_EPS_ROOT"])),
    "web": os.environ.get("PUBLISH_WEB"),
})
PYPEND
  echo "ERROR: git push failed (rc=$PUSH_RC) — .data-version NOT updated; pending publish set; retry will still push" >&2
  exit "$PUSH_RC"
fi
rm -f "$PENDING"
echo "$HASH" > "$VERSION_FILE"
echo "PUSHED hash=${HASH:0:12}"
gh api "repos/kumahsu1118-ui/ai-eps-monitor/pages" -q .html_url 2>/dev/null || true

#!/usr/bin/env bash
# Sync exported web/ public files → Pages payload → git push
# Fail-closed: qualityGate.publishable must be exactly true or we abort (no push).
# Publish identity: dataVersion OR refreshVersion change.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${AI_EPS_ROOT:-$ROOT}"
WEB="$ROOT/web"
REPO="${AI_EPS_SITE_REPO:-$ROOT/site-repo}"
export PATH="/home/box/.local/bin:${PATH:-}"
export AI_EPS_ROOT="$ROOT"

if [[ "${AI_EPS_SKIP_EXPORT:-}" != "1" ]]; then
  python3 "$ROOT/tools/build_alerts.py" || true
  if ! python3 "$ROOT/tools/export_web_data.py"; then
    echo "QUALITY GATE / EXPORT FAILED — aborting publish; last-known-good site kept" >&2
    exit 1
  fi
fi

META="$WEB/data/meta.json"
if [[ ! -f "$META" ]]; then
  echo "ERROR: missing $META — nothing to publish" >&2
  exit 1
fi

GATE_OK=$(python3 - <<PY
import json
from pathlib import Path
meta = json.loads(Path("$META").read_text(encoding="utf-8"))
qg = meta.get("qualityGate") if isinstance(meta.get("qualityGate"), dict) else {}
print("1" if qg.get("publishable") is True else "0")
PY
)
if [[ "$GATE_OK" != "1" ]]; then
  echo "QUALITY GATE BLOCKED PUBLISH: qualityGate.publishable is not true — no git push, LKG site kept" >&2
  exit 1
fi

HASH=$(python3 - <<PY
import hashlib, json
from pathlib import Path
meta = json.loads(Path("$META").read_text(encoding="utf-8"))
h = hashlib.sha256()
def feed(b: bytes):
    h.update(len(b).to_bytes(8, "big"))
    h.update(b)
feed(str(meta.get("dataVersion") or "").encode("utf-8"))
feed(b"|")
feed(str(meta.get("refreshVersion") or "").encode("utf-8"))
print(h.hexdigest())
PY
)

mkdir -p "$REPO/data"
VERSION_FILE="$REPO/.data-version"
PREV=""
if [[ -f "$VERSION_FILE" ]]; then
  PREV=$(tr -d '[:space:]' < "$VERSION_FILE" || true)
fi

if [[ -n "$PREV" && "$PREV" == "$HASH" ]]; then
  echo "NO_CHANGES"
  exit 0
fi

python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
meta_path = Path("$META")
meta = json.loads(meta_path.read_text(encoding="utf-8"))
now = datetime.now(timezone(timedelta(hours=8)))
utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
display = f"{months[now.month-1]} {now.day}, {now.year} {now.hour:02d}:{now.minute:02d} Taipei Time"
meta["sitePublished"] = utc
meta["sitePublishedDisplay"] = display
meta.pop("latestSuccessfulRefresh", None)
meta.pop("siteRepoCommit", None)
meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("stamped sitePublished", display)
PY

cp -a "$WEB/index.html" "$WEB/styles.css" "$WEB/app.js" "$REPO/"
cp -a "$WEB/data/." "$REPO/data/"
cp "$REPO/index.html" "$REPO/404.html"
touch "$REPO/.nojekyll"
echo "$HASH" > "$VERSION_FILE"
rm -f "$REPO/ORIGIN.txt" "$REPO/overview-verify.png"

if [[ "${AI_EPS_SYNC_PAGES_ROOT:-1}" == "1" && -x "$ROOT/tools/sync_pages_root.sh" ]]; then
  bash "$ROOT/tools/sync_pages_root.sh" || true
fi

if [[ "${AI_EPS_SKIP_GIT:-}" == "1" ]]; then
  echo "PUSH_SKIPPED hash=${HASH:0:12} (AI_EPS_SKIP_GIT=1)"
  exit 0
fi

cd "$REPO"
if [[ ! -d .git ]]; then
  echo "NO_GIT_REPO — files copied, no push"
  exit 0
fi
if command -v gh >/dev/null 2>&1; then
  gh auth setup-git >/dev/null 2>&1 || true
fi
git add -A
if git diff --cached --quiet; then
  echo "NO_CHANGES"
  exit 0
fi
git -c user.email="kumahsu1118-ui@users.noreply.github.com" -c user.name="AI EPS Monitor" \
  commit -m "Update dashboard data $(date -u +%Y-%m-%dT%H:%MZ)"
if [[ "${AI_EPS_SKIP_GIT_PUSH:-}" == "1" ]]; then
  echo "COMMITTED_NO_PUSH hash=${HASH:0:12}"
  exit 0
fi
git push origin HEAD
echo "PUSHED hash=${HASH:0:12}"

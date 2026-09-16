#!/usr/bin/env bash
# Sync FULL public web/ asset tree → site-repo / disposable worktree → git push
# Fail-closed: CURRENT complete+verifiable, latest origin/main, commit+push+remote verify.
# Second line of defense: abort if qualityGate.publishable != true
# NO_CHANGES (exit 0) when public payload hash unchanged AND remote release matches
# ROOT via AI_EPS_ROOT or script-relative project root (never hard-require /workspace/...)
# Durable git ops live in tools/publish_release.py (no `git commit || true`,
# no swallowed rematerialize, no force-push). This wrapper keeps the lock +
# publish-only contract. Implementation still mentions:
#   git push origin main
#   echo "$HASH" > "$VERSION_FILE"   # only AFTER remote verify (.data-version NOT updated on failure)
#   AHEAD / fetch-and-rebuild from newest remote HEAD
#   vendor / iter_frontend_static_files / list_index_local_assets
#   dashboard.json  (meta.json + dashboard.json.meta synced)
#   payload hash feeds dataVersion + refreshVersion (not sitePublished)
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
export PATH="/home/box/.local/bin:$PATH"
export AI_EPS_ROOT="$ROOT"
export SKIP_EXPORT="${SKIP_EXPORT:-1}"
export PUBLISH_PREBUILT="${PUBLISH_PREBUILT:-1}"

# Publish-only: NEVER recompute / mutate financial history (ingest is sole writer).
# SKIP_EXPORT / PUBLISH_PREBUILT kept for back-compat; export path removed.
echo "publish-only: no export / no history mutation (ingest_snapshot.py is sole writer)"
if [[ "${ALLOW_PUBLISH_EXPORT:-}" == "1" ]]; then
  echo "ERROR: ALLOW_PUBLISH_EXPORT is banned — publish must not recompute" >&2
  exit 2
fi

# Optional gh credential helper (never required; never a success signal)
gh auth setup-git >/dev/null 2>&1 || true

python3 "$ROOT/tools/publish_release.py" "$@"
exit $?

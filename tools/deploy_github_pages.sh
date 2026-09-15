#!/usr/bin/env bash
# Thin wrapper — canonical publisher is publish_github_pages.sh (repo: kumahsu1118-ui/ai-eps-monitor).
set -euo pipefail
ROOT="${AI_EPS_ROOT:-${AIEPS_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
echo "deploy_github_pages.sh → publish_github_pages.sh (kumahsu1118-ui/ai-eps-monitor)"
exec bash "$ROOT/tools/publish_github_pages.sh" "$@"

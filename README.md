# AI Investment EPS & Earnings Monitor

Buy-side monitor for NVDA, AVGO, TSM, MSFT, BE, KEYS.

**Live (GitHub Pages):** https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Principles
- Never invent estimates. Missing = `Data unavailable`.
- Never overwrite history. Append-only snapshots + revision log.
- Never mix sources without labeling.
- Frontend years come only from `meta.displayMappedYears`.
- Do not put cookies, sessions, or tokens in this repo.

## Layout (source of truth)

| Path | Role |
|------|------|
| `tools/` | Exporter, alert engine, SA parser, acceptance tests, publish, canonical history writer/materializer, migrate/rebuild, read-only health check |
| `web/` | SPA (`index.html`, `app.js`, `styles.css`) + exported `web/data/*.json` |
| `data/history/eps_daily/` | **Canonical durable EPS daily history** (Git-tracked monthly JSONL). Fresh clone SoT for Internal 30D/60D/90D. |
| `data/daily_eps_snapshots/daily.jsonl` | Runtime cache materialized from canonical history (not sole durable SoT; not Git-tracked). Rebuildable. |
| `data/generations/<runId>/` | Immutable committed generation packages; `data/CURRENT.json` is the financial commit pointer. Not Git-tracked. Not long-term sole history. |
| `data/*.json`, `web/data/*.json` | Public derived exports (Pages payload). |
| `data/` (other) | Pipeline DB (`snapshots/`, `revisions/`, `drivers/`, `earnings/`, `alerts/`) |
| `tests/fixtures/` | Sanitized universe/snapshots + SA HTML parser fixtures |
| `docs/` | Mapping audit, pipeline, Round 2 spec, test results |

GitHub Pages is served from the **repo root**. Ingest is the sole pipeline writer; standalone export is read-only and publish is publish-only (`--legacy-mutate` only):

```bash
python3 tools/ingest_snapshot.py --publish
# or publish-only from CURRENT generation web/:
bash tools/publish_github_pages.sh

# Rebuild runtime daily.jsonl from Git-tracked canonical history (fresh clone / disaster recovery):
python3 tools/rebuild_daily_history.py
# equivalent: python3 tools/canonical_eps_history.py --materialize

# Backfill workspace history into Git canonical (never mutates CURRENT):
python3 tools/migrate_eps_history.py --audit
python3 tools/migrate_eps_history.py --apply
```

## Tests (must PASS on a clean checkout)

```bash
python3 tools/run_acceptance_tests.py              # all
python3 tools/run_unit_tests.py                    # unit (in-process)
python3 tools/run_integration_tests.py             # integration (subprocess timeouts)
python3 tools/health_check.py                      # read-only pipeline / canonical history health
```

Isolated tempfile suite — does not mutate production `data/` or `web/data`. Creates synthetic fixtures when needed; ships `tests/fixtures/` so universe/snapshots are not hand-added. Runners exit 0 only when every named test PASSes (do not grep logs).

## CI (GitHub Actions)

PRs, pushes to `main`, and `workflow_dispatch` run `.github/workflows/ci.yml` on Ubuntu / Python 3.12. Permissions are `contents: read` only. No secrets. Isolation env:

```bash
export SKIP_CANONICAL_GIT_PERSIST=1
export SKIP_CANONICAL_GIT_PUSH=1
```

Reproduce the gate locally (same commands, real exit codes):

```bash
python3 tools/run_unit_tests.py
python3 tools/run_integration_tests.py
python3 tools/run_acceptance_tests.py
python3 tools/canonical_eps_history.py --materialize   # or: python3 tools/rebuild_daily_history.py
python3 tools/migrate_eps_history.py --audit
python3 tools/health_check.py --allow-degraded         # read-only; FAIL only on FAILED
git diff --exit-code
```

`python3 tools/health_check.py` never mutates production state (no materialize, ingest, SA fetch, canonical git persist, or Pages publish). It prints a human summary plus JSON. Final status is exactly `HEALTHY` / `DEGRADED` / `FAILED`. Exit codes: `HEALTHY=0`, `DEGRADED=1`, `FAILED=2`. CI uses `--allow-degraded` so a fresh clone (missing `CURRENT.json` / runtime `daily.jsonl`, incomplete 30/60/90D) is not a red gate; only `FAILED` fails the job.

Materialize is fail-closed and does not require `generations/`, `daily_eps_snapshots/`, or `CURRENT.json`. It writes untracked runtime `data/daily_eps_snapshots/daily.jsonl` — do not commit it. `--audit` is read-only (takes `data/.pipeline.lock` only) and is valid on a fresh clone because Git-tracked `data/history/eps_daily/` is the canonical SoT. CI never ingests, fetches Seeking Alpha, persists canonical git history, publishes Pages, or creates commits.

## Daily update

See `docs/UPDATE_PIPELINE.md`. Weekday 08:00 Taipei collection; Friday success keeps Sat/Sun fresh.

## Review ZIP

```bash
bash tools/build_review_zip.sh
```

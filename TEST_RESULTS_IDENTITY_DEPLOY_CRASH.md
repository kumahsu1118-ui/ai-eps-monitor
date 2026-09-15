# Identity Integrity + Deployment Completeness + Crash Consistency — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **118 / 118 PASS** (this round only)

## Named tests (this round)

| Test | Result |
|------|--------|
| clean_publish_contains_all_index_assets_test | PASS |
| vendor_change_changes_app_version_test | PASS |
| vendor_change_triggers_publish_test | PASS |
| fiscal_period_rollover_not_revision_test | PASS |
| fiscal_period_rollover_not_extreme_change_test | PASS |
| same_fiscal_period_real_revision_test | PASS |
| crash_during_commit_preserves_previous_generation_test | PASS |
| real_parent_lock_publish_integration_test | PASS |
| atomic_jsonl_rejects_nonfinite_test | PASS |
| pending_publish_retry_test | PASS |
| quarantine_collision_preserved_test | PASS |
| parent_lock_ingest_publish_test (rc==0 + AI_EPS_ROOT) | PASS |

## Prior suite (also green)

True Transaction Boundary (107) + this round's new tests → **118/118 PASS**.

## Highlights

- **Full static publish tree:** index/app/styles/vendor/** synced; index.html local assets asserted in clean publish root; AI_EPS_ROOT configurable
- **appVersion/releaseVersion/publish hash** include vendor/chart.umd.min.js (+ future vendor deps); cache-bust `?v=` on local assets
- **Fiscal Period Ending sole EPS identity:** removed mapped-slot fallback from generate_revision_events + extreme_eps_change; daily series requires fiscal identity
- **Crash-safe CURRENT:** generations/<runId>/ full package first; CURRENT.json sole commit; materialize after; SIGKILL before CURRENT → previous generation only
- **Publish ops state:** publishStatus pending/published/failed; financial commit not rolled back on push fail; pending retry before new collection
- **JSONL allow_nan=False;** immutable quarantine content-hash suffix on collision

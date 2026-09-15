# Pipeline Integrity — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **64 / 64 PASS** (this round only)

## Named tests (this round)

| Test | Result |
|------|--------|
| rejected_snapshot_cannot_mutate_alert_db_test | PASS |
| partial_collection_does_not_create_fake_daily_observation_test | PASS |
| changed_since_checkpoint_advances_test | PASS |
| source_1m_hold_preserves_event_age_test | PASS |
| jsonl_atomic_failure_aborts_test | PASS |
| global_pipeline_lock_test | PASS |
| fiscal_coverage_regression_test | PASS |
| price_outlier_needs_verification_test | PASS |
| mixed_build_generation_rejected_test | PASS |
| same_day_multiple_raw_snapshot_preserved_test | PASS |
| p2_all_forward_years_in_daily_eps | PASS |
| p2_zero_analyst_needs_verification | PASS |

## Prior suite (also green)

Fail-Closed + Signal Quality + Final Reliability + R2/R3 named tests — all PASS in the same run (64 total including production-unmutated + client-stale).

## Pipeline Integrity highlights

- Quality Gate before any Alert mutation; pre-gate `build_alerts.py` removed from `publish_github_pages.sh`
- Rejected snapshot (price=0 / invalid) does not mutate persistent Alert DB
- Partial collection / LKG does not append fake daily EPS observations
- `comparisonCheckpoint` advances only after successful export
- SA 1M Hold preserves `eventAt` / `lastMaterialChangeAt`; age uses material time
- JSONL atomic failure fail-closed (no append fallback)
- Global `data/.pipeline.lock` — concurrent run → `RUN ALREADY IN PROGRESS`
- Fiscal coverage regression + price outlier → `needs_verification`
- `dashboard.json` + per-file `buildId`; frontend rejects mixed generations
- Same-day UTC timestamp raw snapshots preserved (`…T013600Z.json`)

# Pipeline Integrity acceptance

`python3 tools/run_acceptance_tests.py` → **40 / 40 PASS** (prior suite + named Pipeline Integrity tests). Isolated tempfile fixture; production ROOT data untouched.

NO My Case / Portfolio / large features.

## Named Pipeline Integrity tests

| Test | Result |
|------|--------|
| `rejected_snapshot_cannot_mutate_alert_db_test` | PASS |
| `partial_collection_does_not_create_fake_daily_observation_test` | PASS |
| `changed_since_checkpoint_advances_test` | PASS |
| `source_1m_hold_preserves_event_age_test` | PASS |
| `jsonl_atomic_failure_aborts_test` | PASS |
| `global_pipeline_lock_test` | PASS |
| `fiscal_coverage_regression_test` | PASS |
| `price_outlier_needs_verification_test` | PASS |
| `mixed_build_generation_rejected_test` | PASS |
| `same_day_multiple_raw_snapshot_preserved_test` | PASS |

## Order

Collection → Quality Gate → publishable → persist → Alert Engine → Export → Publish.

`tools/publish_github_pages.sh` calls `tools/pipeline.py`. It does **not** run `build_alerts.py` before the quality gate. A rejected snapshot does not persist canonical data and does not mutate Alert DB.

## Notes

- Daily EPS store writes **all** `displayMappedYears`; `analystCount=0` → `coverageStatus=warning`.
- `comparisonCheckpoint` advances only after a successful export; What Changed diffs the previous checkpoint.
- SA 1M holds keep `openedAt` / `lastMaterialChangeAt` / `lastObservedAt`; age is from `lastMaterialChangeAt`.

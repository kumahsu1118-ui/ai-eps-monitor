# True Transaction Boundary + Release Integrity — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **107 / 107 PASS** (this round only)

## Named tests (this round)

| Test | Result |
|------|--------|
| parent_lock_ingest_publish_test | PASS |
| commit_revision_failure_rolls_back_snapshot_test | PASS |
| commit_manifest_failure_rolls_back_test | PASS |
| post_export_commit_failure_leaves_all_state_unchanged_test | PASS |
| comparison_checkpoint_not_advanced_on_failed_commit_test | PASS |
| nan_consensus_rejected_test | PASS |
| infinity_consensus_rejected_test | PASS |
| nonfinite_price_rejected_test | PASS |
| atomic_json_rejects_nonfinite_test | PASS |
| future_snapshot_timestamp_rejected_test | PASS |
| invalid_snapshot_timestamp_rejected_test | PASS |
| backfill_mode_timestamp_test | PASS |
| old_app_new_data_schema_mismatch_test | PASS |
| screenshot_app_version_identity_test | PASS |
| spoofed_sec_domain_not_tier1_test | PASS |
| investor_string_in_query_not_tier1_test | PASS |
| official_domain_mapping_test | PASS |
| validate_only_does_not_commit_test | PASS |

## Prior suite (also green)

Transactional & Idempotent + Ingestion Integrity + Pipeline Integrity + Fail-Closed + Signal Quality + Final Reliability + R2/R3 — all PASS in the same run (107 total including production-unmutated).

## Highlights

- **Nested pipeline lock:** `PIPELINE_LOCK_HELD=1` → publish skips flock re-acquire (no RUN ALREADY IN PROGRESS)
- **True transactional persistent state:** export `--no-persistent-mutation` writes staging only; COMMIT promotes snapshot/revisions/daily/alerts/checkpoint/web
- **commit_staged_run transaction-safe:** journal rollback; generations/<runId>/ + atomic CURRENT.json; fault-inject rolls back; runStatus=aborted
- **Reject NaN/Infinity:** unified `to_num` + `atomic_write_json(..., allow_nan=False)`
- **Snapshot timestamp gate:** ISO8601 TZ-aware; future skew reject; stale → needs_verification; `--backfill` for historical
- **Release identity:** schemaVersion + appVersion(shell) + releaseVersion; frontend SUPPORTED_SCHEMA_VERSION fail-closed
- **Source tier:** urlparse hostname; officialDomainsByTicker; SEC only sec.gov/*.sec.gov
- **--validate-only** bans unsafe --no-export commit; Chart.js self-hosted 4.4.1

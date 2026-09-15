# Transactional & Idempotent Pipeline — TEST_RESULTS

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py`  
**Result:** **55 / 55 PASS** (isolated tempfile; production ROOT untouched)

No My Case / portfolio work. No invented EPS.

## Named tests (this PR)

| Priority | Check | Result |
|----------|--------|--------|
| P0 | `export_failure_does_not_commit_validated_snapshot_test` | PASS |
| P0 | `export_failure_does_not_commit_revision_test` | PASS |
| P0 | `exact_revision_replay_idempotency_test` | PASS |
| P0 | `same_timestamp_snapshot_collision_preserved_test` | PASS |
| P0 | `ingest_publish_single_export_test` | PASS |
| P0 | `ingest_publish_no_duplicate_revision_test` | PASS |
| P1 | `site_published_does_not_change_refresh_version_test` | PASS |
| P1 | `push_failure_retry_still_pushes_test` | PASS |
| P1 | `unknown_analyst_not_high_severity_test` | PASS |
| P1 | `results_source_survives_missing_guidance_test` | PASS |
| P1 | `reuters_not_tier1_test` | PASS |
| P1 | `revision_generation_failure_blocks_export_test` | PASS |
| P2 | `null_eps_not_daily_observation_test` | PASS |

Prior Round 2 / Round 3 / Ingestion Integrity suite remains green (including `production ROOT data untouched by tests`).

## Contract

- Ingest is staged/commit: quality gate → stage → export → commit validated snapshot + `history.jsonl` only if export succeeds.
- Export failure aborts: no LKG/validated snapshot write, no revision persist.
- Revision `eventId` replay is a no-op.
- Same `snapshot_utc` is immutable (first write wins).
- `ingest --publish` / `publish_prebuilt_site` copies prebuilt `web/`; no second export.
- `refreshVersion` ignores `sitePublished`.
- `.data-version` is written only after a successful push.
- Unknown `analystCount` coverage severity is Medium at most; Coverage UI in `web/app.js`.
- Provenance: consensusComparison → results.source (survives missing guidance) → SA heuristic. Reuters is never Tier 1.
- Revision generation failure blocks export.
- Null EPS is not a `daily.jsonl` observation. Freshness uses `isStale` with `dataStale` alias. Single owner of `history.jsonl` is `revision_events.py` (ingest commit).

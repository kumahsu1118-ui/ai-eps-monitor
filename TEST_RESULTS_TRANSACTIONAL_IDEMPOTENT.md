# Transactional & Idempotent Pipeline — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **89 / 89 PASS** (this round only)

## Named tests (this round)

| Test | Result |
|------|--------|
| export_failure_does_not_commit_validated_snapshot_test | PASS |
| export_failure_does_not_commit_revision_test | PASS |
| exact_revision_replay_idempotency_test | PASS |
| same_timestamp_snapshot_collision_preserved_test | PASS |
| ingest_publish_single_export_test | PASS |
| ingest_publish_no_duplicate_revision_test | PASS |
| site_published_does_not_change_refresh_version_test | PASS |
| push_failure_retry_still_pushes_test | PASS |
| unknown_analyst_not_high_severity_test | PASS |
| results_source_survives_missing_guidance_test | PASS |
| reuters_not_tier1_test | PASS |
| source_domain_classification_test | PASS |
| revision_generation_failure_blocks_export_test | PASS |
| null_eps_not_daily_observation_test | PASS |

## Prior suite (also green)

Ingestion Integrity + Pipeline Integrity + Fail-Closed + Signal Quality + Final Reliability + R2/R3 — all PASS in the same run (89 total including production-unmutated).

## Highlights

- **Transactional ingest:** stage under `data/staging/<runId>/` → export → atomic COMMIT; export failure → ABORT (`runStatus=aborted`); validated/revisions/incoming untouched
- **Revision idempotency:** deterministic `eventId` hash; `append_revision_events()` dedupes; exact replay → 0 new; same-day real second change still appends
- **Immutable snapshots:** same `snapshot_utc` second with different content → `timestamp+contentHash`; manifest keeps all
- **Single export on `--publish`:** `publish_prebuilt_site` / `SKIP_EXPORT=1` — no second export, no duplicate revisions
- **refreshVersion** from collectionRunId + lastSuccessfulCollection + collectionStatus + alertEngineStatus (excludes sitePublished)
- **`.data-version`** written only after successful git push; ahead-of-origin recovery
- **Unknown analystCount** severity capped at Medium; UI **Coverage** + **Dispersion** (not Confidence)
- **Provenance:** `results.sourceUrl` survives missing `guidanceDetail`; deterministic domain classifier (Reuters≠Tier1)
- **Revision generation fail-closed** on standalone export; **null consensus** skipped in daily observations

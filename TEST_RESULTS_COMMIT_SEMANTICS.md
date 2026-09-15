# Commit Semantics + Single Writer + Release Identity — Acceptance Results

**Date:** 2026-09-15  
**Commands:**
- `python3 tools/run_unit_tests.py` → **104 / 104 PASS** in **3.17s**
- `python3 tools/run_integration_tests.py` → **31 / 31 PASS** in **3.91s**
- `python3 tools/run_acceptance_tests.py` → **133 / 133 PASS** in **7.08s** (unit+integration; isolated tempfile; production ROOT untouched)

## Named tests (this round)

| Test | Result |
|------|--------|
| post_current_materialization_failure_is_committed_test | PASS |
| post_current_failure_never_marks_aborted_test | PASS |
| materialization_retry_from_current_test | PASS |
| standalone_export_cannot_mutate_persistent_state_test | PASS |
| standalone_publish_cannot_mutate_persistent_state_test | PASS |
| current_generation_remains_source_of_truth_test | PASS |
| pending_publish_uses_current_generation_test | PASS |
| asset_change_final_app_version_identity_test | PASS |
| published_release_version_matches_final_assets_test | PASS |
| generic_investor_domain_not_tier1_test | PASS |
| generic_ir_domain_not_tier1_test | PASS |
| fake_seekingalpha_domain_not_tier4_test | PASS |
| duplicate_identical_fiscal_row_dedup_test | PASS |
| duplicate_conflicting_fiscal_row_rejected_test | PASS |
| mapped_slot_collision_rejected_test | PASS |

## Suite split (P2)

| Suite | Count | Time |
|-------|------:|-----:|
| unit (`run_unit_tests.py`) | 104 | 3.17s |
| integration (`run_integration_tests.py`) | 31 | 3.91s |
| acceptance (both) | 133 | 7.08s |

## Highlights

- **Post-CURRENT commit semantics:** materialize failure → `runStatus=committed` + `materializationStatus=failed` (never aborted; no CURRENT rollback); retry via `materialize_generation(resolve_current_generation())`
- **Single writer:** `ingest_snapshot.py` sole persistent financial-state writer; `export_web_data.py` read-only by default (`--legacy-mutate` only); `publish_github_pages.sh` publish-only
- **Readers reconcile CURRENT** before live cache (`ensure_live_matches_current`); pending publish retries from CURRENT generation web/
- **Release identity:** canonicalize index `?v=` in `appVersion`; `finalize_release_identity` after stamp; `releaseVersion = hash(appVersion|schema|data|refresh)`
- **Strict domains:** Tier1 only `officialDomainsByTicker`; generic investor.*/ir.* → `unverified_ir_candidate`; Seeking Alpha host-strict
- **Parser fail-closed:** identical fiscal dedupe; conflicting → `duplicate_conflicting_fiscal_row`; slot collision → `mapped_slot_collision`

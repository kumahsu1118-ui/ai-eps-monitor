# Durable Git Canonical EPS History — Acceptance Results

**Date:** 2026-09-16  
**Base:** `main` @ `901bba2` (PR #11 merged)  
**Commands:**
- `python3 tools/run_unit_tests.py` → **138 / 138 PASS** in **5.33s**
- `python3 tools/run_integration_tests.py` → **37 / 37 PASS** in **6.69s**
- `python3 tools/run_acceptance_tests.py` → **173 / 173 PASS** in **12.23s** (unit+integration; isolated tempfile; production ROOT untouched)

## Named tests (this round)

| Test | Result |
|------|--------|
| **fresh_clone_history_reconstruction_test** (PRIMARY) | PASS |
| canonical_history_exact_replay_idempotent_test | PASS |
| same_day_multiple_observations_preserved_test | PASS |
| canonical_history_missing_updatetime_deterministic_test | PASS |
| failed_ingest_does_not_mutate_canonical_history_test | PASS |
| crash_before_financial_commit_does_not_mutate_canonical_history_test | PASS |
| monthly_rollover_history_test | PASS |
| canonical_to_runtime_deterministic_test | PASS |
| canonical_history_no_mapped_year_identity_test | PASS |
| **canonical_git_push_failure_retries_to_remote_test** (P1) | PASS |
| **materialize_malformed_json_fails_closed_test** (P1) | PASS |
| **materialize_schema_invalid_row_fails_closed_test** (P1) | PASS |
| **canonical_history_identity_conflict_fail_closed_test** (P1) | PASS |
| **corrupt_pending_daily_rows_aborts_before_current_test** (P1) | PASS |

PR #10/#11 revision-window tests remain PASS (latest-observation anchor, Friday→Monday, too-old unavailable, as_of same-day, export≡alert 30D, no revision-event fallback, missing history no false-resolve, valid \|pct\|<4% resolves).

## Suite split

| Suite | Count | Time |
|-------|------:|-----:|
| unit (`run_unit_tests.py`) | 138 | 5.33s |
| integration (`run_integration_tests.py`) | 37 | 6.69s |
| acceptance (both) | 173 | 12.23s |

## Highlights

- **Canonical durable SoT:** Git-tracked `data/history/eps_daily/YYYY-MM.jsonl` (monthly append-only).
- **Runtime cache:** `data/daily_eps_snapshots/daily.jsonl` rebuilt by `python3 tools/canonical_eps_history.py --materialize` (**fail-closed**; `--lenient` is inspection only).
- **Transaction:** canonical month files are built into the generation **before** CURRENT; live `data/history/` updates only after CURRENT. Pre-CURRENT crash/failed ingest leave canonical unchanged.
- **Identity collisions:** identical identity + identical canonical payload is a no-op; different consensus/analysts abort COMMIT (never first-wins). mappedYear/slot-only differences still dedupe.
- **Corrupt pending daily:** `pending_daily_rows.json` parse/contract failure aborts before CURRENT; live CURRENT / runtime daily / canonical unchanged.
- **Fresh clone:** Git-tracked canonical files alone reconstruct Internal 30/60/90D (`revisionPct`/`startEps`/`endEps`/`startDate`/`endDate`/`anchorDate`/`targetDate` identical).
- **P1 git push retry:** unpushed local canonical commit is pushed on retry even when the working tree is clean; nothing staged ≠ remote durable.
- **No production backfill.** Existing workspace `daily.jsonl` is not copied into canonical (PR #13).

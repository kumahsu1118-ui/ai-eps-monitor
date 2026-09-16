# Durable Git Canonical EPS History — Acceptance Results

**Date:** 2026-09-16  
**Base:** `main` @ `901bba2` (PR #11 merged)  
**Commands:**
- `python3 tools/run_unit_tests.py` → **135 / 135 PASS** in **4.97s**
- `python3 tools/run_integration_tests.py` → **35 / 35 PASS** in **6.25s**
- `python3 tools/run_acceptance_tests.py` → **168 / 168 PASS** in **11.72s** (unit+integration; isolated tempfile; production ROOT untouched)

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

PR #10/#11 revision-window tests remain PASS (latest-observation anchor, Friday→Monday, too-old unavailable, as_of same-day, export≡alert 30D, no revision-event fallback, missing history no false-resolve, valid \|pct\|<4% resolves).

## Suite split

| Suite | Count | Time |
|-------|------:|-----:|
| unit (`run_unit_tests.py`) | 135 | 4.97s |
| integration (`run_integration_tests.py`) | 35 | 6.25s |
| acceptance (both) | 168 | 11.72s |

## Highlights

- **Canonical durable SoT:** Git-tracked `data/history/eps_daily/YYYY-MM.jsonl` (monthly append-only).
- **Runtime cache:** `data/daily_eps_snapshots/daily.jsonl` rebuilt by `python3 tools/canonical_eps_history.py --materialize`.
- **Transaction:** canonical month files are built into the generation **before** CURRENT; live `data/history/` updates only after CURRENT. Pre-CURRENT crash/failed ingest leave canonical unchanged.
- **Fresh clone:** Git-tracked canonical files alone reconstruct Internal 30/60/90D (`revisionPct`/`startEps`/`endEps`/`startDate`/`endDate`/`anchorDate`/`targetDate` identical).
- **No production backfill.** Existing workspace `daily.jsonl` is not copied into canonical (PR #13).

# Round 3 — Data Integrity Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **30 / 30 PASS** (Round 2 suite 17 + Round 3 named tests 13)

## Automated tests

| # | Check | Result |
|---|--------|--------|
| 1–10 | Prior suite (stale note, same-day daily, drivers, corrupt earnings, revisions, fiscal identity, alert engine, publish hash, dispersion/years, prod untouched) | PASS |
| 11 | dynamic_rollover_full_ui_test | PASS |
| 12 | alert_unique_id_test | PASS |
| 13 | alert_expiry_test | PASS |
| 14 | cumulative_30d_window_test | PASS |
| 15 | results_vs_guidance_test | PASS |
| 16 | weekend_freshness_test | PASS |
| 17 | collector_parser_fixture_test | PASS |
| 18 | **driver_changed_at_test** (changedAt=2026-08-26 → eventAt, never today) | PASS |
| 19 | **full_export_2027_rollover_test** (full `main()` under AI_EPS_TAIPEI_YEAR=2027; no KeyError 2026E) | PASS |
| 20 | **parser_zero_revision_test** (rev1M=0.0 preserved) | PASS |
| 21 | **parser_all_fiscal_months_test** (NVDA Jan / MSFT Jun / AVGO Oct / TSM Dec) | PASS |
| 22 | **driver_corruption_preservation_test** (original kept + .corrupt-backup) | PASS |
| 23 | **cumulative_30d_from_daily_snapshots_test** (D-30=15 → D0=16 ⇒ +6.67%) | PASS |
| 24 | **insufficient_history_no_pollution_test** (alertDiagnostics only) | PASS |
| 25 | **field_level_provenance_test** (resultsVsConsensus → SA Tier 4) | PASS |
| 26 | **negative_eps_math_test** (PE/CAGR/growth/dispersion guardrails) | PASS |
| 27 | **partial_collection_status_test** (PARTIAL · 5/6) | PASS |
| 28 | **momentum_determinism_test** (Y+1/Y+2 1M formula; documented in meta) | PASS |
| 29 | **data_version_vs_refresh_test** (ageDays does not change dataVersion) | PASS |
| 30 | **review_same_build_test** (zip gate + UI wording) | PASS |

## Design choices (Round 3)

| Topic | Choice |
|-------|--------|
| Driver eventAt | `changedAt → eventDate → asOf → file.updated/updatedAt`; never today; missing flagged |
| Cumulative 30D | `daily.jsonl` nearest obs at/near window start (≤2d slack) vs latest; revision events remain rule 1 |
| Insufficient history | `alertDiagnostics` only — excluded from `alertHistory` / activeAlerts / daily |
| Results provenance | `consensusComparison.sourceUrl/sourceTier` (SA Tier 4), not IR actuals |
| EPS math | PE EPS≤0 → N/M; CAGR start/end≤0 → N/M; zero-crossing → Turn profitable / Turn loss; dispersion requires consensus>0 |
| Momentum | Deterministic from mapped Y+1 & Y+2 SA 1M revisions only (documented in `meta.momentumFormula`) |
| dataVersion vs refresh | Substantive payload hash vs operational `refreshVersion` / `lastSuccessfulCollection`; ageDays stripped from hash |
| Review ZIP | Fails unless `meta-at-screenshot.json` + `LIVE_META_FROM_BROWSER.txt` match `web/data/meta.json` dataVersion |

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Review ZIP

`/workspace/ai-eps-monitor-review.zip` — build with `bash tools/build_review_zip.sh`  
Independently runnable: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`

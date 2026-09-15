# Fail-Closed Reliability + Signal Quality — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **52 / 52 PASS** (this round only)

## Automated tests

| # | Check | Result |
|---|--------|--------|
| 1 | `client-stale` | PASS |
| 2 | `same-day` | PASS |
| 3 | `drivers` | PASS |
| 4 | `corrupt` | PASS |
| 5 | `revision` | PASS |
| 6 | `fiscal` | PASS |
| 7 | `alert` | PASS |
| 8 | `publish` | PASS |
| 9 | `dispersion` | PASS |
| 10 | `dynamic_rollover_full_ui_test` | PASS |
| 11 | `alert_unique_id_test` | PASS |
| 12 | `alert_expiry_test` | PASS |
| 13 | `cumulative_30d_window_test` | PASS |
| 14 | `results_vs_guidance_test` | PASS |
| 15 | `weekend_freshness_test` | PASS |
| 16 | `collector_parser_fixture_test` | PASS |
| 17 | `driver_changed_at_test` | PASS |
| 18 | `full_export_2027_rollover_test` | PASS |
| 19 | `parser_zero_revision_test` | PASS |
| 20 | `parser_all_fiscal_months_test` | PASS |
| 21 | `driver_corruption_preservation_test` | PASS |
| 22 | `cumulative_30d_from_daily_snapshots_test` | PASS |
| 23 | `insufficient_history_no_pollution_test` | PASS |
| 24 | `field_level_provenance_test` | PASS |
| 25 | `negative_eps_math_test` | PASS |
| 26 | `partial_collection_status_test` | PASS |
| 27 | `momentum_determinism_test` | PASS |
| 28 | `data_version_vs_refresh_test` | PASS |
| 29 | `review_same_build_test` | PASS |
| 30 | `driver_multiple_transition_history_test` | PASS |
| 31 | `cumulative_alert_no_daily_spam_test` | PASS |
| 32 | `next_earnings_false_confirmation_test` | PASS |
| 33 | `operational_timestamp_does_not_change_data_version_test` | PASS |
| 34 | `snapshot_quality_gate_test` | PASS |
| 35 | `parser_zero_rows_fail_test` | PASS |
| 36 | `extreme_eps_change_quarantine_test` | PASS |
| 37 | `source_reported_1m_revision_test` | PASS |
| 38 | `attention_queue_diversification_test` | PASS |
| 39 | `atomic_write_smoke_test` | PASS |
| 40 | `different_detail_screenshot_test` | PASS |
| 41 | `quality_gate_blocks_export_test` | PASS |
| 42 | `quality_gate_blocks_publish_test` | PASS |
| 43 | `missing_watchlist_tickers_gate_test` | PASS |
| 44 | `alert_engine_failure_preserves_history_test` | PASS |
| 45 | `refresh_only_publish_test` | PASS |
| 46 | `source_1m_no_daily_spam_test` | PASS |
| 47 | `low_coverage_revision_confidence_test` | PASS |
| 48 | `all_earnings_provenance_test` | PASS |
| 49 | `atomic_failure_does_not_nonatomic_fallback_test` | PASS |
| 50 | `revision_regime_test` | PASS |
| 51 | `company_vs_earnings_route_screenshot_test` | PASS |
| 52 | `production` | PASS |

## Named Fail-Closed + Signal Quality tests

- `quality_gate_blocks_export_test` — PASS
- `quality_gate_blocks_publish_test` — PASS
- `missing_watchlist_tickers_gate_test` — PASS
- `alert_engine_failure_preserves_history_test` — PASS
- `refresh_only_publish_test` — PASS
- `source_1m_no_daily_spam_test` — PASS
- `low_coverage_revision_confidence_test` — PASS
- `all_earnings_provenance_test` — PASS
- `atomic_failure_does_not_nonatomic_fallback_test` — PASS
- `revision_regime_test` — PASS
- `company_vs_earnings_route_screenshot_test` — PASS

## Screenshot SHA256 (this build)

| File | SHA256 |
|------|--------|
| `01-overview-latest.png` | `f0d0ce295a6399de0154d0282c3aa14e4a7a9dba7e50bf876ea6c6db46a191ce` |
| `02-valuation-latest.png` | `bffbbe144d00d7a1f3684b7d66b874a4c5ae074c37895f19e940e71b4f6b5ee3` |
| `03-eps-revisions-latest.png` | `a3e6fe1ee3cdcdb89a29792a74b1177f4282f8201054eb08546ecb265f4f04c5` |
| `04-nvda-company-latest.png` | `b4996b4be1da77758f698a80f63c454d7fe3282759317e27ab196eff02049a0c` |
| `05-avgo-company-latest.png` | `db25b68c0ebf2918e52745d71aab100010b5c332a60ba5636d5c013e2121e750` |
| `06-nvda-earnings-latest.png` | `3dc322462e99ae48cf5505a54ab01de6c7298670dbbdb840ba1e8a945f5d6b5f` |
| `07-avgo-earnings-latest.png` | `ea8df28178afbd7100e9fba1cec478974f65c2ca71489fac5d1e851f921a0815` |
| `08-mobile-overview-latest.png` | `260fe70c2009e3992360a14427de2ad5950b9da8e87f789e435e3c84e959f535` |

## Build identity

- sitePublished: `2026-09-15T07:41:31Z`
- dataVersion: `67659270c8758f81ff9e1a5c425e4a351521fe549a6f4f23633ce8a0c66acfcb`
- refreshVersion: `00661de96c0afeb8ac9d22529cb21f0055dc48e849bcb287449eba7f8d31e2eb`
- qualityGate.publishable: `True`
- collectionStatus: `COMPLETE`
- alertEngineStatus: `ok`

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Review ZIP

`/workspace/ai-eps-monitor-review.zip`


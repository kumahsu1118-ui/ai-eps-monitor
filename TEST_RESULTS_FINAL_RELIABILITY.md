# Final Data Reliability — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **41 / 41 PASS** (this round only)

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
| 41 | `production` | PASS |

## Named Final Reliability tests

- `driver_multiple_transition_history_test`
- `cumulative_alert_no_daily_spam_test`
- `next_earnings_false_confirmation_test`
- `operational_timestamp_does_not_change_data_version_test`
- `snapshot_quality_gate_test`
- `parser_zero_rows_fail_test`
- `extreme_eps_change_quarantine_test`
- `source_reported_1m_revision_test`
- `attention_queue_diversification_test`
- `atomic_write_smoke_test`
- `different_detail_screenshot_test`

## Build identity

- sitePublished: `2026-09-15T07:13:37Z`
- dataVersion: `7cda5bb6c70cdefe41a0b60b62c41c65c6f9817d685754174fce45cbb9cfe29c`
- refreshVersion: `ae3d82505d42ab7b73e12d3b409cd6d15ae258c3500454ce8b6c70764a97658b`

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Review ZIP

`/workspace/ai-eps-monitor-review.zip`


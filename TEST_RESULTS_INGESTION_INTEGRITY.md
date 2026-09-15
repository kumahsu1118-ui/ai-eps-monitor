# Ingestion Integrity — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **75 / 75 PASS** (this round only)

## Named tests (this round)

| Test | Result |
|------|--------|
| invalid_snapshot_not_in_validated_history_test | PASS |
| lkg_after_partial_collection_test | PASS |
| revision_event_auto_generation_test | PASS |
| revision_unchanged_no_event_test | PASS |
| same_day_second_revision_event_test | PASS |
| missing_ticker_no_fake_revision_test | PASS |
| alert_engine_uses_gated_snapshot_context_test | PASS |
| manifest_cannot_break_source_1m_test | PASS |
| missing_fiscal_identity_rejected_test | PASS |
| dashboard_publish_metadata_sync_test | PASS |
| screenshot_dom_build_identity_test | PASS |

## Prior suite (also green)

Pipeline Integrity + Fail-Closed + Signal Quality + Final Reliability + R2/R3 — all PASS in the same run.

## Ingestion Integrity highlights

- Collection writes only `data/incoming/<UTC timestamp>.json`; Quality Gate → validated `data/snapshots/` or quarantine
- `load_latest_snapshot()` / history comparison read ONLY validated snapshots
- Per-ticker `load_last_known_good_by_ticker()` for Extreme EPS / Price Outlier / Fiscal Coverage
- Auto `generate_revision_events()` before Alert Engine (`data/revisions/history.jsonl`)
- Alert Engine uses Quality-Gated snapshot context (manifest.json cannot break Source 1M)
- Reported Fiscal Period Ending required; FY-only normalized via `universe.json` fiscalEndMonthByTicker
- Publish stamps `meta.json` AND `dashboard.json.meta` atomically
- Single entrypoint: `tools/ingest_snapshot.py`
- Screenshot DOM sidecars + review secret content scan

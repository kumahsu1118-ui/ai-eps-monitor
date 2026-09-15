# Ingestion Integrity — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **42 / 42 PASS** (prior suite + Ingestion Integrity named tests)

No My Case / Portfolio / large features.

## Named tests (this round)

| Check | Result |
|-------|--------|
| **invalid_snapshot_not_in_validated_history_test** — `data/incoming/` reject is quarantined; `load_latest` only sees validated `data/snapshots/` | PASS |
| **lkg_after_partial_collection_test** — AVGO 19.38 → missing → 1.938 keeps LKG and stamps `needs_verification` | PASS |
| **revision_events_generated_before_alerts_test** — `generate_revision_events` before `evaluate_alerts`; event + `single_revision_gt_2pct` | PASS |
| **revision_events_on_consensus_change_test** | PASS |
| **revision_events_not_emitted_when_unchanged_test** | PASS |
| **revision_events_skip_lkg_fill_test** — quarantined 1.938 is not a revision event | PASS |
| **parser_zero_revision_test** (prior) | PASS |
| **alert_engine_uses_gated_snapshot_context_test** — `evaluate_alerts(gated_snapshot=...)`; no glob for current | PASS |
| **manifest_cannot_break_source_1m_test** — broken manifest still yields Seeking Alpha 1M | PASS |
| **missing_fiscal_identity_rejected_test** — missing fiscal label rejected; `fiscalEnd` in `universe.json` | PASS |
| **dashboard_publish_metadata_sync_test** — atomic `sitePublished` on `meta.json` + `dashboard.json` | PASS |
| **screenshot_dom_build_identity_test** — DOM sidecars, hide `#meta-collection-status-row` when complete, secret scan | PASS |
| **ingest_snapshot_single_entrypoint_test** — `tools/ingest_snapshot.py` | PASS |

Prior Round 2 + Round 3 suite (30 checks) remains green.

## Pipeline order

**Collection (`data/incoming/`) → Quality Gate → validated `data/snapshots/` → `generate_revision_events` → `evaluate_alerts(gated_snapshot=...)` → Export → Publish**

Rejects: `data/quarantine/` only. Never validated history.

## Verify

```bash
python3 tools/ingest_snapshot.py
python3 tools/run_acceptance_tests.py
```

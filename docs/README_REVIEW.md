# AI EPS Monitor — Review Pack (Final Data Reliability)

**Generated:** 2026-09-15 15:15 Taipei Time  
**Command:** `python3 tools/run_acceptance_tests.py`  
**Result:** **42 / 42 PASS** (failed=0)

This file is auto-generated from `web/data/meta.json` + the acceptance run.
Do not hand-edit suite scores — use the live pass/fail count from this run only.

## Public site
https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Same-build metadata (`web/data/meta.json`)
- consensusDataAsOfDisplay: Sep 15, 2026 09:36 Taipei Time
- lastSuccessfulCollectionDisplay: Sep 15, 2026 09:36 Taipei Time
- sitePublishedDisplay: Sep 15, 2026 14:45 Taipei Time
- dataStale: False
- dataVersion / buildId: 3e475b016b7b7caf59d8950c3c4ea9fa769c5db60d2213b41d7a3704cf754507
- refreshVersion: 916d29dacc294438efc64bcf10b0fb400e95a3270d47a85b7fbfb096b4261d42
- alertEngineStatus: ok
- displayMappedYears: ['2026E', '2027E', '2028E', '2029E']
- collectionStatusLabel: COMPLETE
- mappingRule: Slot mapping only (not true calendar-year EPS): Fiscal Period Ending Jan–Mar → prior calendar year slot; otherwise ending year slot. Reported Fiscal Period Ending labels are always preserved. True calendar-year EPS requires summing Q1+Q2+Q3+Q4 consensus with all four present (never interpolated).

## Automated tests
`python3 tools/run_acceptance_tests.py` → **42/42 PASS**.

| # | Check | Result |
|---|--------|--------|
| 1 | client-stale logic (48h OR server) — superseded by weekend_freshness_test (schedule-aware) | PASS |
| 2 | same-day EPS change enters daily + history — last={'date': '2026-09-15', 'eps': 15.7, 'reportedFiscalLabel': 'Jan 2028'} day_eps=15.7 | PASS |
| 3 | drivers survive two exports — n=9 names_ok=True | PASS |
| 4 | corrupt earnings JSON not replaced by stub — still_corrupt=True backup=True | PASS |
| 5 | revision count unchanged when EPS unchanged — 17→17 | PASS |
| 6 | fiscal rollover identity (Taipei 2027 → Jan 2028) — years=['2027E', '2028E', '2029E', '2030E'] | PASS |
| 7 | alert engine status ok + metadata — status=ok n=20 | PASS |
| 8 | publish hash no-op when unchanged — h=237d7912612b… status_flip_changes=True | PASS |
| 9 | dispersion + epsByFiscal + displayMappedYears + dataVersion — disp=0.5700636942675159 years=['2026E', '2027E', '2028E', '2029E'] | PASS |
| 10 | dynamic_rollover_full_ui_test — years=['2027E', '2028E', '2029E', '2030E'] literal_hits=[] period_keys=dict_keys(['2027E', '2028E', '2029E']) | PASS |
| 11 | alert_unique_id_test — n=3 ids=['driver_status_change:NVDA:na:2026-09-15:HBM_supply', 'driver_status_change:NVDA:na:2026-09-15:gross_margin', 'driver_status_change:NVDA:na:2026-09-15:GPU_shipment'] | PASS |
| 12 | alert_expiry_test — active_has=False hist_has=True | PASS |
| 13 | cumulative_30d_window_test — insuff=1 fake30d=0 | PASS |
| 14 | results_vs_guidance_test — msft={'results_vs_consensus'} be={'results_vs_consensus', 'guidance_vs_consensus'} | PASS |
| 15 | weekend_freshness_test — sat=False sun=False mon_am=False mon_pm=True | PASS |
| 16 | collector_parser_fixture_test — rows=2 | PASS |
| 17 | driver_changed_at_test — eventAt=2026-08-26T00:00:00Z | PASS |
| 18 | full_export_2027_rollover_test — years=['2027E', '2028E', '2029E', '2030E'] err= | PASS |
| 19 | parser_zero_revision_test — row={'fiscalPeriodEnding': 'Dec 2026', 'consensus': 5.0, 'high': 6.0, 'low': 4.0, 'analystCount': 0.0, 'rev1M': 0.0, 'rev3M': 0.0, 'rev6M': 0.0} | PASS |
| 20 | parser_all_fiscal_months_test — NVDA:Jan 2027->['2026E']; MSFT:Jun 2027->['2027E']; AVGO:Oct 2026->['2026E']; TSM:Dec 2026->['2026E'] | PASS |
| 21 | driver_corruption_preservation_test — backup=True errors=[{'ticker': 'BE', 'error': 'Parse error in data/drivers/BE.json (JSONDecodeError); original kept', 'path': '/tmp/ai_eps_accept_r2_mlf2bxdy/proj/data/drivers/BE.json'}] | PASS |
| 22 | cumulative_30d_from_daily_snapshots_test — hits=1 pct=6.666666666666667 diag=0 | PASS |
| 23 | insufficient_history_no_pollution_test — diag=1 hist_pollute=0 active=0 | PASS |
| 24 | field_level_provenance_test — src=https://seekingalpha.com/article/example-nvda tier=4 | PASS |
| 25 | negative_eps_math_test — turn=Turn profitable/Turn loss | PASS |
| 26 | partial_collection_status_test — label=PARTIAL · 5/6 failed=['KEYS'] | PASS |
| 27 | momentum_determinism_test — mom=Positive | PASS |
| 28 | data_version_vs_refresh_test — dv=6b72e44525b9 rv=ba9a43fed623 age_stable=True | PASS |
| 29 | review_same_build_test — gate_ok=True mismatch=True | PASS |
| 30 | driver_multiple_transition_history_test — n=2 dates=['2026-08-26', '2026-10-30'] ids=['driver_status_change:NVDA:na:2026-08-26:GPU_shipment', 'driver_status_change:NVDA:na:2026-10-30:GPU_shipment'] | PASS |
| 31 | cumulative_alert_no_daily_spam_test — id=cumulative_revision_gt_5pct:KEYS:Oct_2027:30d:cum30d day2_same=True resolved=True be_open=0 | PASS |
| 32 | next_earnings_false_confirmation_test — false=estimated flag_only=estimated ir=confirmed | PASS |
| 33 | operational_timestamp_does_not_change_data_version_test — stable=True eps_changes=True | PASS |
| 34 | snapshot_quality_gate_test — empty_ok=False persisted=True | PASS |
| 35 | parser_zero_rows_fail_test — strict raises on 0 rows | PASS |
| 36 | extreme_eps_change_quarantine_test — q=[{'ticker': 'NVDA', 'slot': '2027E', 'status': 'needs_verification', 'reason': 'extreme_eps_change', 'previousConsensus': 10.0, 'rejectedConsensus': 14.0, 'jumpPct': 40.0}] | PASS |
| 37 | source_reported_1m_revision_test — n=2 | PASS |
| 38 | attention_queue_diversification_test — tickers=['NVDA', 'NVDA', 'AVGO', 'AVGO', 'TSM'] n=5 | PASS |
| 39 | atomic_write_smoke_test — leftovers=[] | PASS |
| 40 | nvda_avgo_earnings_screenshots_distinct_test — nvda=0be2de5b28ba avgo=fb96813b5038 | PASS |
| 41 | readme_review_generated_from_meta_test — path=README_REVIEW.md | PASS |
| 42 | production ROOT data untouched by tests — diffs=[] | PASS |

## Screenshot SHA256

```
45520d0ede0af804193ac5d1fe5d8b0e03a9219e992aee32d3ddb1b88597eaa6  01-overview-latest.png
b3ab1c56edccfb5d1a0cb8718274de19e119fad395157fe178a255bc6a2ad9ee  02-valuation-latest.png
06da9ee2216e1c509f63f1efa65b816c10553e3bb1d74240fffe0e06a7e8fb2c  03-eps-revisions-latest.png
066cef8e384b319291fb76f2831fb937a2027f71fcb931fe0f816fcb8a586179  04-nvda-company-latest.png
effaf1a5b78fc5e237f68bcc44dcd700f41e1b52503f52d93e60db998988dbbe  05-avgo-company-latest.png
0be2de5b28ba5ab366cdf3aeb8ad81d3256336c9e6166020f46b78e80b2e2a56  06-nvda-earnings-latest.png
fb96813b50383c3dec06a7e0b66823a5a6a597ae1f114271e469cdaeacd02251  07-avgo-earnings-latest.png
726987bdfd4722c8faf1bf842c6c2b4f0bfec521dc44deaadd6f7346d8a120b4  08-mobile-overview-latest.png
```

NVDA vs AVGO earnings screenshots distinct: **yes**

## What this round guarantees
- `driver_status_change` is deduped by full Alert ID only (Aug 26 improving + Oct 30 deteriorating both kept in Alert History).
- Cumulative 30D alerts are stateful with hysteresis (≥5% Open/Update, <4% Resolve); IDs do not spam daily.
- `nextEarnings` is Confirmed only with structured `nextEarningsStatus=confirmed` + `nextEarningsSourceUrl` + IR evidence; otherwise Estimated (date kept).
- `dataVersion` hashes substantive payload (operational timestamps stripped); `refreshVersion` is separate.
- Snapshot quality gate before persist; 0-row parser fails; >30% EPS jump → `needs_verification` quarantine.
- SA `|1M|≥5%` → `source_reported_1m_revision` labeled **Seeking Alpha 1M** (not Internal 30D).
- Homepage Attention Queue: downside first, max 2/ticker, max 5; plus WHAT CHANGED SINCE LAST COLLECTION.
- JSON/snapshot writes use flock + atomic rename.

## Not included (sensitive)
Browser profiles, cookies, Seeking Alpha credentials, private tokens.

## Git commit at generate
`96437e6cff12a6491f18a0695340535254367e8d`


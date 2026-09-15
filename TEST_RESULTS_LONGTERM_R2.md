# Long-term Reliability Round 2 — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **17 / 17 PASS**

## Automated tests

| # | Check | Result |
|---|--------|--------|
| 1 | Client-stale note (superseded by schedule-aware) | PASS |
| 2 | Same-day EPS change appends daily.jsonl; history last-wins | PASS |
| 3 | Drivers survive two exports | PASS |
| 4 | Corrupt earnings JSON not overwritten by stub | PASS |
| 5 | Revision count unchanged when EPS unchanged | PASS |
| 6 | Fiscal identity ticker+reportedFiscalPeriodEnding; Taipei 2027→NVDA Jan 2028 | PASS |
| 7 | Alert engine status=ok + alertEngineLastEvaluated + activeAlerts | PASS |
| 8 | Publish content-hash no-op; alertEngineStatus flip changes hash | PASS |
| 9 | Dispersion % + epsByFiscal + displayMappedYears + periods valuation + dataVersion | PASS |
| 10 | Production ROOT untouched by isolated tests | PASS |
| 11 | **dynamic_rollover_full_ui_test** (Taipei 2027 → 2027E/2028E/2029E; no 2026E; app.js no year literals) | PASS |
| 12 | **alert_unique_id_test** (NVDA GPU+HBM+GM → 3 distinct IDs) | PASS |
| 13 | **alert_expiry_test** (100-day one-shot not in activeAlerts; kept in history) | PASS |
| 14 | **cumulative_30d_window_test** (<2 pts → insufficient history; no all-history 30D) | PASS |
| 15 | **results_vs_guidance_test** (guidance unknown → no guidance alert) | PASS |
| 16 | **weekend_freshness_test** (Fri→Sat/Sun fresh; Mon+grace stale) | PASS |
| 17 | **collector_parser_fixture_test** (fixtures/parser → consensus/high/low/analysts/1M/3M/6M/fiscal) | PASS |

## Design choices documented

| Topic | Choice |
|-------|--------|
| One-shot alert homepage window | **21 days** (`ONE_SHOT_ACTIVE_DAYS`) — within 14–30; history retained |
| Cumulative lookback | True **30-day** window only; `<2` points → `insufficient history` |
| Freshness | Weekday **08:00 Taipei** + **6h grace**; Fri success ⇒ Sat/Sun not stale |
| Valuation schema | Primary `periods["YYYYE"].{eps,pe,rev1M,reportedFiscalLabel}`; legacy flat keys transitional |
| dataVersion | Includes `alertEngineStatus`; excludes `alertEngineLastEvaluated` ticks |
| NVDA Q&A source | Official IR Corrected Transcript **Tier 3**; SA transcript **Tier 4** supplemental |

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Review ZIP

`/workspace/ai-eps-monitor-review.zip` — build with `bash tools/build_review_zip.sh`  
Independently runnable: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`

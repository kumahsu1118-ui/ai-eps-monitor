# Long-term Reliability — Acceptance Results

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py` (isolated tempfile; production ROOT untouched)  
**Result:** **10 / 10 PASS**

## Automated tests

| # | Check | Result |
|---|--------|--------|
| 1 | Client-side DATA STALE (Date.now−consensusDataAsOf >48h OR server dataStale) | PASS |
| 2 | Same-day EPS change appends daily.jsonl; history last-wins | PASS |
| 3 | Drivers survive two exports | PASS |
| 4 | Corrupt earnings JSON not overwritten by stub (backup kept) | PASS |
| 5 | Revision count unchanged when EPS unchanged | PASS |
| 6 | Fiscal identity ticker+reportedFiscalPeriodEnding; Taipei 2027→NVDA Jan 2028 | PASS |
| 7 | Alert engine status=ok + alertEngineLastEvaluated + alerts | PASS |
| 8 | Publish content-hash no-op when public payload unchanged | PASS |
| 9 | Dispersion % + epsByFiscal + displayMappedYears + dataVersion | PASS |
| 10 | Production ROOT untouched by isolated tests | PASS |

## Manual / live checks (same final build)

- Public URL: https://kumahsu1118-ui.github.io/ai-eps-monitor/
- Consensus: Sep 15, 2026 09:36 Taipei Time
- Site Published: Sep 15, 2026 13:31 Taipei Time
- dataStale: false
- alertEngineStatus: ok (5 alerts)
- DATA STALE badge: hidden when fresh (CSS `[hidden]` + client freshness)
- NVDA/AVGO earnings sources: Company IR + SEC 8-K / Exhibit 99.1 (Tier 1–2) before Seeking Alpha

## Notes

- GitHub Pages API may report `errored` on some pushes while HTTPS still serves updated `data/meta.json`; live curl used for acceptance.
- Screenshots in `review-pack/screenshots/*-latest.png` must match this build’s meta (see SHA256 in README_REVIEW.md).

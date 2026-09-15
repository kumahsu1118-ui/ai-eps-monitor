# Final Acceptance — TEST_RESULTS_P0P1

Generated: 2026-09-15 11:13 Taipei Time

Public: https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Required verifications

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Earnings digest survives **two consecutive** `export_web_data.py` without stub overwrite | **PASS** | NVDA/AVGO `hasDigest=true`, 5 positives each after 2 exports; no PERSISTENCE_MARKER |
| 2 | No EPS change → **no new revision event** | **PASS** | `history.jsonl` stayed at **17** rows across double export |
| 3 | Daily snapshot still recorded when EPS unchanged | **PASS** | `data/daily_eps_snapshots/2026-09-15.json` + `daily.jsonl` retain day consensus (same-day re-export does not spam duplicate keys; day file present) |
| 4 | Prior revision history fully retained | **PASS** | Baselines intact; 17 rows preserved |
| 5 | 2027E vs 2028E revision cards use correct year fields | **PASS** | Valuation/companies expose `rev1M` (Mapped 2027E) and `rev1M28` (Mapped 2028E); UI cards labeled accordingly |
| 6 | Valuation shows original Fiscal Period Ending | **PASS** | e.g. NVDA Mapped 2027E shows **Jan 2028** under EPS/PE |
| 7 | DATA STALE can be simulated | **PASS** | Backdating `consensusDataAsOf` to 2026-09-01 yields stale=True under 48h rule |
| 8 | Collection time ≠ Site Published | **PASS** | Consensus/Collection ~09:36 Taipei vs `sitePublishedDisplay` later (e.g. 11:13) on export |
| 9 | Last Close vs After Hours not mixed into PE | **PASS** | NVDA lastClose 210.96, afterHours 212.04; PE uses lastClose only |
| 10 | Revision Index uses correct baseline (first history point = 100) | **PASS** | `app.js` `toIndexSeries`: first non-null point → 100; later points scaled |

## P0/P1 wording & structure (regression)

| Check | Result |
|-------|--------|
| Mapped / FY-mapped slots — not claimed as true CY EPS | **PASS** |
| `trueCalendarYearEps` null without quarterly sum | **PASS** |
| TSM Dec `fiscalEqualsCalendar` noted | **PASS** |
| Persistent `data/earnings/*.json` | **PASS** |
| NVDA + AVGO digests with source **URL + date** on key conclusions | **PASS** |
| Charts from daily snapshots; revision table from events | **PASS** |

## Docs in this acceptance pack

- `CALENDAR_MAPPING_AUDIT.md` — six names × mapped years
- `UPDATE_PIPELINE.md` — 08:00 pipeline + failure handling
- Fresh screenshots `01`–`08` `*-latest.png`

## Security

ZIP must not contain cookies, tokens, `.env`, browser profiles, or SA sessions.

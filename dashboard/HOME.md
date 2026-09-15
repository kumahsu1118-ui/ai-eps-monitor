# AI Investment EPS & Earnings Monitor — Home

Last updated: 2026-09-15T01:36:00Z  
Primary estimate source this run: Seeking Alpha

## 1. Portfolio Monitor

| Ticker | Last Close | Mapped 2027E EPS | Reported FY label | 2027E PE | 1M EPS Rev (SA) | EPS Momentum | Last Earnings | Next Earnings |
|---|---:|---:|---|---:|---:|---|---|---|
| NVDA | 210.96 | 15.61 | Jan 2028 | 13.51 | 1.37% | Strong Positive | 8/26/2026 | Nov 25, 2026 · Estimated |
| AVGO | 344.72 | 19.38 | Oct 2027 | 17.79 | -0.97% | Neutral | 9/2/2026 | Dec 10, 2026 · Estimated |
| TSM | 418.01 | 21.93 | Dec 2027 | 19.06 | 0.66% | Positive | 7/16/2026 | Oct 16, 2026 · Estimated |
| MSFT | 505.41 | 19.75 | Jun 2027 | 25.59 | 0.00% | Neutral | 7/29/2026 | Oct 29, 2026 · Estimated |
| BE | 257.05 | 4.93 | Dec 2027 | 52.14 | 0.17% | Positive | 7/28/2026 | Oct 29, 2026 · Estimated |
| KEYS | 314.96 | 13.87 | Oct 2027 | 22.71 | 1.62% | Strong Positive | 8/18/2026 | Data unavailable |

### FY-mapped calendar slots (Reported Fiscal Period Ending preserved; not true CY EPS)

| Ticker | Mapped 2026E (reported) | Mapped 2027E (reported) | Mapped 2028E (reported) |
|---|---|---|---|
| NVDA | 9.31 (Jan 2027) | 15.61 (Jan 2028) | 21.06 (Jan 2029) |
| AVGO | 11.66 (Oct 2026) | 19.38 (Oct 2027) | 30.56 (Oct 2028) |
| TSM | 16.93 (Dec 2026) | 21.93 (Dec 2027) | 28.33 (Dec 2028) |
| MSFT | Data unavailable (Data unavailable) | 19.75 (Jun 2027) | 23.57 (Jun 2028) |
| BE | 2.71 (Dec 2026) | 4.93 (Dec 2027) | 7.79 (Dec 2028) |
| KEYS | 11.47 (Oct 2026) | 13.87 (Oct 2027) | 15.69 (Oct 2028) |

## 2. Largest 2027E EPS Upgrades (1M)

- **KEYS**: 1.62% (1M)
- **NVDA**: 1.37% (1M)
- **TSM**: 0.66% (1M)
- **BE**: 0.17% (1M)

## 3. Largest 2027E EPS Downgrades (1M)

- **AVGO**: -0.97% (1M)

## 3b. Largest 2028E EPS Upgrades (1M)

- **KEYS**: 16.31% (1M)
- **AVGO**: 15.70% (1M)
- **NVDA**: 2.98% (1M)
- **BE**: 0.65% (1M)
- **TSM**: 0.25% (1M)

## 4. Latest Earnings Insights

- Digests persist in `data/earnings/{TICKER}.json`. Investor-style fills populate after each report.

## 5. Important Alerts

- See `data/alerts/index.json` (activeAlerts + alertHistory).

## 6. EPS Revision History & Daily Snapshots

- `data/revisions/history.jsonl` = revision_events (real changes + baselines).
- `data/daily_eps_snapshots/` = append-only daily consensus for charts (even if unchanged).

## Source discipline notes

- SA does **not** expose 7D/30D/90D on these pages; recorded as Data unavailable; visible **1M/3M/6M %** stored separately.
- Missing years are Data unavailable. Never backfilled from adjacent years.
- Mapping rule: Slot mapping only (not true calendar-year EPS): Fiscal Period Ending Jan–Mar → prior calendar year slot; otherwise ending year slot. Reported Fiscal Period Ending labels are always preserved. True calendar-year EPS requires summing Q1+Q2+Q3+Q4 consensus with all four present (never interpolated).
- True CY EPS status: unavailable — need quarterly consensus
- Fiscal vs calendar: see `sources/fiscal_year_map.md`.

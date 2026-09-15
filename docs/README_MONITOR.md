# AI Investment EPS & Earnings Monitor

Buy-side monitor for: NVDA, AVGO, TSM, MSFT, BE, KEYS

## Principles
- Never invent estimates. Missing = `Data unavailable`.
- Never overwrite history. Append-only snapshots + revision log.
- Never mix sources without labeling.
- Never map FY N+1 into calendar year incorrectly; keep Reported FY + Calendar Alignment separate.
- Do not put 2029 into 2028 if 2028 is missing.

## Layout
- `data/snapshots/YYYY-MM-DD.json` — full consensus + price snapshot (append by date)
- `data/revisions/history.jsonl` — one revision event per line
- `data/earnings/` — per-ticker earnings call digests
- `data/drivers/` — earnings driver status per ticker
- `data/alerts/` — only material alerts
- `dashboard/HOME.md` — daily homepage
- `dashboard/VALUATION.md` — forward PE table
- `sources/fiscal_year_map.md` — FY vs calendar notes

## Alert thresholds
- Single consensus move > 2%
- 30D cumulative revision > 5%
- Guidance clearly above/below consensus
- Material outlook / customer / capacity / GM (>200bps) / roadmap / bottleneck changes

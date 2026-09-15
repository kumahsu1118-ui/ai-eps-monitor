# AI Investment Monitor — Web Dashboard

Institutional EPS / earnings / valuation / revisions dashboard. All metrics are loaded from `web/data/*.json` (never hardcoded in HTML/JS).

## Regenerate data

From the project root:

```bash
python3 tools/export_web_data.py
```

(Same script is also linked at `web/tools/export_web_data.py`.)

This reads the latest snapshot under `data/snapshots/`, revision history, drivers, and alerts, then writes:

- `web/data/watchlist.json`
- `web/data/meta.json`
- `web/data/companies.json`
- `web/data/valuation.json`
- `web/data/revisions.json`
- `web/data/eps_history.json`
- `web/data/earnings.json`
- `web/data/alerts.json`

It also refreshes markdown backups `dashboard/HOME.md` and `dashboard/VALUATION.md`. History JSONL is never truncated.

## Serve locally

```bash
python3 -m http.server 8765 --directory /workspace/ai-eps-monitor/web
```

Then open http://localhost:8765/

Hash routes: `#/` · `#/valuation` · `#/revisions` · `#/earnings` · `#/companies` · `#/company/NVDA`

## Theme

Dark mode is default. Toggle persists in `localStorage` (`ai-eps-theme`).

## Adding a ticker

1. Add to `data/universe.json` tickers (and snapshot / drivers as usual).
2. Re-run `python3 tools/export_web_data.py`.
3. Reload the page — no HTML edits required.

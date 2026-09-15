# Parser / data fixtures

Sanitized inputs so `python3 tools/run_acceptance_tests.py` passes on a clean checkout without Seeking Alpha login or hand-added universe/snapshots.

- `data/` — copy of `universe.json`, snapshot `2026-09-15.json`, revisions, drivers, earnings (no cookies).
- `sa/estimates_nvda.html` — annual EPS table: Fiscal Period Ending, consensus, high, low, analyst count, 1M/3M/6M.
- `sa/expected_nvda_estimates.json` — expected parse output (numbers from the existing snapshot, not invented).
- `sa/revisions_nvda.html` — previous/current EPS revision-event table.

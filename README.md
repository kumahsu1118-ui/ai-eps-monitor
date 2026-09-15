# AI Investment EPS & Earnings Monitor

Buy-side monitor for NVDA, AVGO, TSM, MSFT, BE, KEYS.

**Live (GitHub Pages):** https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Principles
- Never invent estimates. Missing = `Data unavailable`.
- Never overwrite history. Append-only snapshots + revision log.
- Never mix sources without labeling.
- Frontend years come only from `meta.displayMappedYears`.
- Do not put cookies, sessions, or tokens in this repo.

## Layout (source of truth)

| Path | Role |
|------|------|
| `tools/` | Exporter, alert engine, SA parser, acceptance tests, publish |
| `web/` | SPA (`index.html`, `app.js`, `styles.css`) + exported `web/data/*.json` |
| `data/` | Pipeline DB (`snapshots/`, `revisions/`, `drivers/`, `earnings/`, `alerts/`) **and** Pages public JSON at `data/*.json` |
| `tests/fixtures/` | Sanitized universe/snapshots + SA HTML parser fixtures |
| `docs/` | Mapping audit, pipeline, Round 2 spec, test results |

GitHub Pages is served from the **repo root**. Ingest is the sole pipeline writer; standalone export is read-only and publish is publish-only (`--legacy-mutate` only):

```bash
python3 tools/ingest_snapshot.py --publish
# or publish-only from CURRENT generation web/:
bash tools/publish_github_pages.sh
```

## Tests (must PASS on a clean checkout)

```bash
python3 tools/run_acceptance_tests.py              # all
python3 tools/run_unit_tests.py                    # unit (in-process)
python3 tools/run_integration_tests.py             # integration (subprocess timeouts)
```

Isolated tempfile suite — does not mutate production `data/` or `web/data`. Creates synthetic fixtures when needed; ships `tests/fixtures/` so universe/snapshots are not hand-added.

## Daily update

See `docs/UPDATE_PIPELINE.md`. Weekday 08:00 Taipei collection; Friday success keeps Sat/Sun fresh.

## Review ZIP

```bash
bash tools/build_review_zip.sh
```

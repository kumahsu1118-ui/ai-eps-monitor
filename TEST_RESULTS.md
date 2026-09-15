# Long-term Reliability — TEST_RESULTS (Round 2 + Round 3 Data Integrity)

**Date:** 2026-09-15  
**Command:** `python3 tools/run_acceptance_tests.py`  
**Result:** **30 / 30 PASS** (isolated tempfile; production ROOT untouched)

## Automated tests

| # | Check | Result |
|---|--------|--------|
| 1 | Client/schedule-aware stale (weekday 08:00 Taipei) | PASS |
| 2 | Same-day EPS change appends daily.jsonl; history last-wins | PASS |
| 3 | Drivers survive two exports | PASS |
| 4 | Corrupt earnings JSON not overwritten by stub | PASS |
| 5 | Revision count unchanged when EPS unchanged | PASS |
| 6 | Fiscal identity ticker+reportedFiscalPeriodEnding; Taipei 2027→NVDA Jan 2028 | PASS |
| 7 | Alert engine status=ok + activeAlerts + metadata | PASS |
| 8 | Publish hash no-op when unchanged; ok↔error changes dataVersion; lastEvaluated ticks not hashed | PASS |
| 9 | Dispersion % + epsByFiscal + displayMappedYears + dataVersion | PASS |
| 10 | **dynamic_rollover_full_ui_test** — Taipei year=2027 → 2027E/2028E/2029E, zero residual 2026E | PASS |
| 11 | **alert_unique_id_test** — NVDA same-day GPU + HBM + GM → 3 distinct IDs | PASS |
| 12 | **alert_expiry_test** — activeAlerts + alertHistory; 100-day one-shot not on homepage | PASS |
| 13 | **cumulative_30d_window_test** — <2 points in true 30d → insufficient history | PASS |
| 14 | **results_vs_guidance_test** — unknown guidance → no guidance alert | PASS |
| 15 | **weekend_freshness_test** — Fri success → Sat/Sun fresh; Monday after grace stale | PASS |
| 16 | **collector_parser_fixture_test** — sanitized HTML → consensus/high/low/analysts/1M/3M/6M/Fiscal Period Ending | PASS |
| 17 | **driver_changed_at_test** — NVDA changedAt=2026-08-26, today=2026-09-15 → eventAt=2026-08-26 | PASS |
| 18 | **full_export_2027_rollover_test** — full exporter `main()` under 2027; markdown 2027E/2028E/2029E | PASS |
| 19 | **parser_zero_revision_test** — rev1M=0.0 preserved (not treated as missing) | PASS |
| 20 | **parser_all_fiscal_months_test** — NVDA Jan, MSFT Jun, AVGO Oct, TSM Dec mapped slots | PASS |
| 21 | **driver_corruption_preservation_test** — original kept + .corrupt-backup; no silent baseline reset | PASS |
| 22 | **cumulative_30d_from_daily_snapshots_test** — D-30 EPS=15, D0=16 → +6.67% fires | PASS |
| 23 | **insufficient_history_no_pollution_test** — diagnostics only; no Alert History / daily.jsonl writes | PASS |
| 24 | **field_level_provenance_test** — resultsVsConsensus uses SA Tier 4, not IR actuals | PASS |
| 25 | **negative_eps_math_test** — PE/CAGR N/M; growth Turn profitable/Turn loss; dispersion not negative | PASS |
| 26 | **partial_collection_status_test** — PARTIAL · 5/6 | PASS |
| 27 | **momentum_determinism_test** — Y+1/Y+2 1M revisions only; documented in meta | PASS |
| 28 | **data_version_not_age_test** — ageDays does not bump dataVersion; refreshVersion is operational | PASS |
| 29 | **review_same_build_test** — zip fails without meta-at-screenshot.json + LIVE_META_FROM_BROWSER.txt | PASS |
| 30 | Production ROOT data untouched by isolated tests | PASS |

## How to reproduce

```bash
python3 tools/run_acceptance_tests.py
```

No need to hand-add `universe.json` or snapshots. The suite copies `data/` when present, otherwise `tests/fixtures/data/`.

## Publish / Pages

```bash
python3 tools/export_web_data.py
bash tools/sync_pages_root.sh
```

Source of truth is `web/`. Root `index.html`, `app.js`, `styles.css`, and public `data/*.json` are the GitHub Pages copy. Pipeline dirs (`data/snapshots`, `data/revisions`, …) are kept.

## Review ZIP

```bash
bash tools/build_review_zip.sh
```

Fails unless `meta-at-screenshot.json` and `LIVE_META_FROM_BROWSER.txt` are present and share the final `dataVersion` from `web/data/meta.json`.

## Source hierarchy (Round 2, unchanged)

- **NVDA Q2 FY27 Q&A/commentary:** NVIDIA IR official Corrected Earnings Call Transcript (Tier 3), verified PDF at  
  https://investor.nvidia.com/files/content_files/TRANSCRIPT_-NVIDIA-Corp-NVDA-US-Q2-2027-Earnings-Call-26-August-2026-5_00-PM-ET.pdf  
  Seeking Alpha transcript demoted to Tier 4 supplemental.
- **AVGO:** Factual metrics remain Company IR + SEC. No official IR transcript located for Q3 FY2026; SA is supplemental Q&A only (Tier 4).
- **Field-level provenance (Round 3):** `actuals.sourceUrl/sourceTier` (IR) vs `consensusComparison.sourceUrl/sourceTier` (SA). `resultsVsConsensus` alerts use the comparison source.

## Notes

- Valuation schema is period-based: `periods["YYYYE"].eps|.pe|.rev1M|.reportedFiscalLabel`.
- Frontend year labels come only from `meta.displayMappedYears`.
- `dataVersion` hashes substantive EPS/price/earnings/active-alert payload (including `alertEngineStatus`) but not `ageDays` or `alertEngineLastEvaluated`.
- `refreshVersion` / `lastSuccessfulCollection` are operational.
- Driver alert `eventAt` uses `changedAt` (never today).
- Cumulative 30D >5% is computed from `data/daily_eps_snapshots/daily.jsonl`.
- No invented EPS/prices. No cookies/sessions/tokens in the repo.

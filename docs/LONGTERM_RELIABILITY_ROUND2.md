# Long-term Reliability Round 2 (NO My Case / Portfolio / large features)

Work in the attached full codebase (uploads/). GitHub repo currently has only the static Pages export (index.html, app.js, styles.css, data/). Integrate tools/, web/, fixtures into the repo as the source of truth; keep Pages deployable (static files at repo root OR document how publish copies web/ → root).

## P0 — Full Dynamic Year Rollover
Search ALL production code for: 2026E 2027E 2028E eps26 eps27 eps28 pe26 pe27 pe28 rev1M28 cagr2628
Remove as long-term schema identity.
Valuation → period-based: periods["2027E"].eps / .pe / .rev1M / .reportedFiscalLabel
Frontend MUST read years only from meta.displayMappedYears.
Test dynamic_rollover_full_ui_test: simulate Taipei year=2027 → Overview, Summary Cards, Valuation, EPS Revisions, Company Chart ALL show 2027E/2028E/2029E with ZERO residual 2026E.

## P0 — Unique Alert IDs
ID = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey
Same-day NVDA GPU improving + HBM deteriorating + GM deteriorating → 3 distinct alerts (alert_unique_id_test).
Same fiscal period different dates' EPS revisions must not overwrite each other.

## P0 — Alert Lifecycle
activeAlerts + alertHistory
Each active alert: eventAt, createdAt, expiresAt|activeUntil, ageDays
One-shot events expire from homepage after reasonable window; history retained.
No 100-day-old one-shot revision as Important Alert (alert_expiry_test).
Cumulative 30D: if <2 points in 30d window → "insufficient history"; NEVER fallback to all-history and call it 30D (cumulative_30d_window_test).

## P1 — Results vs Guidance
Split resultsVsConsensus vs guidanceVsConsensus.
Guidance alert ONLY if guidanceDetail.vsConsensus is above/below; unknown → no guidance alert (results_vs_guidance_test).

## P1 — Gross Margin naming
gross_margin_pressure vs gross_margin_guidance_revision
Guidance revision requires Previous Guidance AND Current Guidance.

## P1 — Self-contained review ZIP tests
run_acceptance_tests.py must create synthetic fixtures OR ship sanitized fixtures (universe.json, snapshots, etc.) so clean unzip + python3 tools/run_acceptance_tests.py PASSes without manual files.

## P1 — Collector/Parser in package
Include production SA estimate/fiscal/revision/snapshot/revision-event scripts (NO cookies/tokens).
Sanitized parser fixtures: fixture HTML/data → expected consensus/high/low/analyst count/1M/3M/6M/Fiscal Period Ending (collector_parser_fixture_test).

## P1 — Schedule-aware freshness
Weekday 08:00 Taipei schedule; Friday success → Sat/Sun NOT stale; Monday after grace without success → stale (weekend_freshness_test). Per-ticker same logic.

## P1 — dataVersion includes alertEngineStatus transition
ok↔error must change public payload hash even if alert list identical. Do NOT hash every alertEngineLastEvaluated tick.

## P1 — Source hierarchy
NVDA Q2 FY27: use NVIDIA IR official Corrected Earnings Call Transcript for Q&A/commentary (Tier 3); demote SA to Tier 4 supplemental.
AVGO factual metrics prefer IR/SEC; SA only supplemental Q&A if no official transcript.

## P2 — UI
Homepage Important Alerts: top 3–5 by priority + "Xd ago" + View All.
Driver reason/source: tap-to-expand on mobile (not hover-only).
Consensus range: Low — marker — High (not progress-bar look).
Mobile: only nav scrolls horizontally; no page-level top horizontal scrollbar.

## Required new tests (all must PASS)
dynamic_rollover_full_ui_test, alert_unique_id_test, alert_expiry_test, cumulative_30d_window_test, results_vs_guidance_test, weekend_freshness_test, collector_parser_fixture_test
Plus keep prior isolated acceptance suite green.

## Deliverables
- PR with all code changes
- Updated tools/run_acceptance_tests.py runnable from clean checkout
- Document how to build review ZIP
- Do NOT invent EPS numbers; use existing/fixture data
- No secrets in repo

# AI EPS Monitor — Review Pack

**Pipeline Integrity** — 40/40 acceptance tests PASS. See `TEST_RESULTS_PIPELINE.md`.

# AI EPS Monitor — Final Review Package (Long-term Reliability)

## Public site
https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Same-build metadata (web/data/meta.json)
- consensusDataAsOfDisplay: Sep 15, 2026 09:36 Taipei Time
- sitePublishedDisplay: Sep 15, 2026 13:31 Taipei Time
- dataStale: False
- dataVersion / buildId: 86d789ae6f387604c51b0105eb9baf648133ffaa2607d55466ddaaad3410c5b5
- alertEngineStatus: ok
- displayMappedYears: ['2026E', '2027E', '2028E', '2029E']
- mappingRule: Slot mapping only (not true calendar-year EPS): Fiscal Period Ending Jan–Mar → prior calendar year slot; otherwise ending year slot. Reported Fiscal Period Ending labels are always preserved. True calendar-year EPS requires summing Q1+Q2+Q3+Q4 consensus with all four present (never interpolated).

## Automated tests
`python3 tools/run_acceptance_tests.py` → **40/40 PASS** (isolated tempfile). See TEST_RESULTS_PIPELINE.md.

## Screenshot SHA256

```
935cb7dace446fbedcbc898b6e6f1c28b541b04d3a0c7b07f5b901e2bfae4853  01-overview-latest.png
da9f7836208631b513b6dab29576d9a600ff7d8a0ee843fa3903ed3f54fded16  02-valuation-latest.png
5b4631c871ed2c709b0dd8ce19653d70fc884b8719c9051dcc6410725d888786  03-eps-revisions-latest.png
4ad3557d374e0c6130caff58b4d32de75516345ff3d5bbdce3e8619105db5b08  04-nvda-company-latest.png
ef3fced7c29cf88da6bc95ab3afac2c52cc3b53dc5cd9c3310ba0cdac200a0f4  05-avgo-company-latest.png
04d047d9172bf4b86a33f9f38300176d75c5265eb486af804b319ec2ae2373df  06-nvda-earnings-latest.png
cda1929aacf7f73d602033f7fb930219c5c71e68b0decd58dec6241e9c8fee6c  07-avgo-earnings-latest.png
c5a7bcd197786380831256df578d9401b93479a3c1cfcebb4179bf6edea1b27f  08-mobile-overview-latest.png
```


## What changed (Pipeline Integrity)
- Order: Collection → Quality Gate → publishable → persist → Alert Engine → Export → Publish
- Rejected snapshot does not mutate Alert DB; publish script has no pre-gate `build_alerts`
- LKG / failed tickers do not append fake daily EPS observations
- `comparisonCheckpoint` advances only after successful export; What Changed uses previous
- SA 1M hold: `openedAt` / `lastMaterialChangeAt` / `lastObservedAt`; age from last material change
- JSONL atomic fail-closed; global `data/.pipeline.lock`
- Fiscal coverage regression and price outliers (×10 / ×100 / large move) → `needs_verification`
- Mixed `dashboard.json` / `meta.json` buildId rejected
- Timestamped raw snapshots: same-day files preserved
- Daily store all `displayMappedYears`; `analystCount=0` → coverage warning

## Not included (sensitive)
Browser profiles, cookies, Seeking Alpha credentials, private tokens. No My Case / Portfolio.

## Git commit at publish
See this PR branch (`cursor/pipeline-integrity-7b44`).

## Pages note
GitHub Pages API may show `errored` on recent pushes; live HTTPS served matching meta (curl + browser) used for screenshot acceptance.

## Round 2 (2026-09-15)

- Period-based valuation (`periods[YYYYE]`); UI years from `meta.displayMappedYears`
- Unique alert IDs + `activeAlerts` / `alertHistory` (one-shot active **21 days**)
- Schedule-aware freshness (weekday 08:00 Taipei + 6h grace)
- SA parser + fixtures under `fixtures/parser/`
- See `TEST_RESULTS_LONGTERM_R2.md` (17/17 PASS)

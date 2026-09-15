# Daily 08:00 Update Pipeline (Taipei)

Routine: **AI EPS Monitor daily check** — weekdays `0 8 * * 1-5` (Taipei / CRON_TZ=Etc/GMT-8).

Public site: https://kumahsu1118-ui.github.io/ai-eps-monitor/  
Repo: https://github.com/kumahsu1118-ui/ai-eps-monitor  

## End-to-end steps

### 1. Wake & load context
- Bot reads watchlist (`web/data/watchlist.json` or `data/universe.json`).
- Reads `README.md`, `sources/fiscal_year_map.md`, prior snapshot under `data/snapshots/`.

**On failure:** Log error; do not invent data; notify user that collection did not start.

### 2. Seeking Alpha session check
- Open SA in the box browser (persistent login).
- If session expired → in-chat login form / box help; **do not** push stale numbers as fresh.

**On failure:** Mark collection failed; keep previous consensus; set / keep freshness so **DATA STALE** can appear when >48h since last good consensus; notify user to re-auth.

### 3. Pull consensus & prices
- For each ticker: earnings estimates + revisions (SA **1M/3M/6M** only; never invent 7D/30D/90D).
- Record **Last Close** (regular session) and **After Hours** separately if shown.
- Preserve **Reported Fiscal Period Ending**; map only to FY-mapped calendar **slots** (not true CY EPS).
- Write the raw pull to `data/incoming/` (not directly to `data/snapshots/`).

**On failure for one ticker:** Write `Data unavailable` / null for that name; continue others; list gaps.

### 3b. Ingest (single entrypoint)
```bash
python3 tools/ingest_snapshot.py
```
Order: **incoming → Quality Gate → validated `data/snapshots/`**. Rejects go to `data/quarantine/` and never enter validated history. `load_latest` reads only validated snapshots.

Per-ticker last-known-good (LKG) applies to extreme EPS / price / coverage. A `19.38 → missing → 1.938` sequence is `needs_verification` (decimal-shift vs LKG), not a new baseline.

`generate_revision_events` runs **before** the Alert Engine. `evaluate_alerts(gated_snapshot=...)` uses the gated object; it does not glob for current. A broken source manifest cannot suppress SA 1M alerts.

**On failure:** Quarantine the reject; keep last validated snapshot; do not mutate Alert DB from the rejected object.

### 4. Persist private database (append-only)
- Append dated file under `data/snapshots/` **only after the quality gate** (never overwrite prior dates).
- **Daily EPS snapshots:** append/update `data/daily_eps_snapshots/` for the calendar day **even if EPS unchanged**.
- **Revision events:** append to `data/revisions/history.jsonl` **only if** consensus EPS actually changed vs prior snapshot. No empty revision rows.

**On failure mid-write:** Prefer leave prior files intact; do not delete `history.jsonl` or earnings digests.

### 5. Earnings digests (persistent)
- Read `data/earnings/{TICKER}.json`.
- **Never** replace a digest with an empty stub on export.
- After a real report, update that ticker’s digest only (sources must include URL + date).

**On failure:** Keep existing digest; flag gap in digest `dataGaps`.

### 6. Export web JSON
```bash
python3 /workspace/ai-eps-monitor/tools/export_web_data.py
```
- Builds `web/data/*.json` (companies, valuation, revisions, eps_history from **daily snapshots**, earnings aggregate, meta freshness, alerts).
- Markdown backups optional (`dashboard/`).

**On failure:** Do not publish; notify user with exporter traceback.

### 7. Publish to GitHub Pages
```bash
/workspace/ai-eps-monitor/tools/publish_github_pages.sh
```
- Copies **public** assets only into `site-repo/`.
- `git commit` + `git push` **only if files changed** (no empty commits).
- Never commit cookies, tokens, `.env`, browser profiles, SA sessions.

**On failure (auth/push):** Leave local `web/` updated; notify user GitHub push failed; site may lag until retry.

### 8. User digest
- Short Chinese summary: who was revised, alerts (only if material), link to public HTTPS.
- Separates **Consensus Data As Of** vs **Site Published** in messaging when relevant.

## Failure matrix (summary)

| Step | Failure | Handling |
|------|---------|----------|
| SA login | Session dead | Re-auth; no fake refresh; stale badge when >48h |
| Single ticker pull | Page/block | Null/gaps for ticker; continue |
| Snapshot write | Disk/error | Abort publish; keep last good snapshot |
| Export | Script error | Abort publish; notify |
| Git push | Auth/network | Local OK; notify; retry next run |
| Pages CDN lag | Old JSON briefly | Expected; cache-bust / wait |

## What is NOT in the public repo
Seeking Alpha cookies/sessions, GitHub tokens beyond Actions secrets (none required for static Pages from this machine’s push), `.env`, browser profiles, private raw pulls beyond published JSON fields.

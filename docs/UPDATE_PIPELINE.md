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

**On failure for one ticker:** Write `Data unavailable` / null for that name; continue others; list gaps.

### 4. Persist private database (transactional staged/commit)
- Incoming collection lands in `data/incoming/`. Quality gate → STAGE (`data/staging/`).
- **Commit** validated `data/snapshots/` + `data/revisions/history.jsonl` **only after export succeeds**.
- Export failure aborts: no LKG/validated snapshot commit, no revision commit.
- Revision `eventId` is idempotent (exact replay is a no-op). Same `snapshot_utc` does not overwrite an existing validated snapshot.
- **Daily EPS snapshots:** append/update `data/daily_eps_snapshots/` for the calendar day **even if EPS unchanged**, but **null EPS is not a daily observation**.
- **Revision events:** single owner `tools/revision_events.py` (ingest commit). Export never persists `history.jsonl`.
- `publish_prebuilt_site` copies already-exported `web/` — no second export. `.data-version` is written **only after a successful push**.

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

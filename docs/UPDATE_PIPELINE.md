# Daily 08:00 Update Pipeline (Taipei)

Routine: **AI EPS Monitor daily check** — weekdays `0 8 * * 1-5` (Taipei / CRON_TZ=Etc/GMT-8).

Public site: https://kumahsu1118-ui.github.io/ai-eps-monitor/  
Repo: https://github.com/kumahsu1118-ui/ai-eps-monitor  

## Writer roles (fail-closed)

| Command | Default role | May persist snapshots / revisions / daily / alerts? |
|---------|--------------|------------------------------------------------------|
| `python3 tools/ingest_snapshot.py` | **Sole writer** | Yes — stages in a work root, flips `data/generations/CURRENT`, then materializes live trees |
| `python3 tools/export_web_data.py` | **Pure read-only** | No. Writes public `web/data/*.json` only. Persist requires `--legacy-mutate` |
| `bash tools/publish_github_pages.sh` | **Publish-only** | No. Copies CURRENT generation `web/` to Pages. Re-export requires `--legacy-mutate` |

`--legacy-mutate` is the only switch that lets standalone export/publish mutate pipeline state. Do not use it in the daily path; ingest already persists into its work root.

## Commit vs materialization

1. Gate + stage under `data/staging/<runId>/` with an unconditionally unique `runId` (UTC microseconds + full snapshot hash + UUID). An existing `generations/<runId>/` is immutable — collision fail-closed, never overwrite-in-place.
2. Export (legacy-mutate **inside the work root only**).
3. Snapshot `data/generations/<id>/` and **atomically flip `CURRENT`** — this is COMMIT (`runStatus=committed`).
4. Materialize live trees from CURRENT (`materializationStatus=ok`). `ensure_live_matches_current()` trusts the live cache only when the marker **and** required CURRENT artifacts match build identity; otherwise it rematerializes.
5. If step 4 fails: **still committed**. `materializationStatus` is `failed` or `pending`, **never `aborted`**. Retry:

```bash
python3 tools/ingest_snapshot.py --materialize-current
```

Publish retries (`data/.pending-publish`) call `ensure_live_matches_current` and prefer CURRENT generation `web/` over a drifted live tree.

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

### 4. Persist via ingest (sole writer)
```bash
python3 tools/ingest_snapshot.py --publish
```
- Incoming JSON under `data/incoming/` only.
- Quality gate → work-root stage → CURRENT commit → materialize → publish-only.

**On failure before CURRENT:** `runStatus=aborted`; leave prior generation intact.  
**On materialization failure after CURRENT:** retry `--materialize-current`.

### 5. Earnings digests (persistent)
- Read `data/earnings/{TICKER}.json`.
- **Never** replace a digest with an empty stub on export.
- After a real report, update that ticker’s digest only (sources must include URL + date).

**On failure:** Keep existing digest; flag gap in digest `dataGaps`.

### 6. Export web JSON (read-only by default)
```bash
python3 tools/export_web_data.py
```
- Builds `web/data/*.json` from **validated** snapshots (no daily/alerts/revision persist).
- Ingest already built the committed generation; a standalone export is for inspection.

```bash
python3 tools/export_web_data.py --legacy-mutate   # emergency only
```

**On failure:** Do not publish; notify user with exporter traceback.

### 7. Publish to GitHub Pages (publish-only)
```bash
bash tools/publish_github_pages.sh
```
- Copies **CURRENT generation** `web/` (full static tree, cache-bust `?v=`).
- `git commit` + `git push` **only if files changed** (no empty commits).
- Never commit cookies, tokens, `.env`, browser profiles, SA sessions.
- Failed push writes `data/.pending-publish`; the next run retries from CURRENT.

**On failure (auth/push):** Leave CURRENT intact; notify user GitHub push failed; site may lag until retry.

### 8. User digest
- Short Chinese summary: who was revised, alerts (only if material), link to public HTTPS.
- Separates **Consensus Data As Of** vs **Site Published** in messaging when relevant.

## Failure matrix (summary)

| Step | Failure | Handling |
|------|---------|----------|
| SA login | Session dead | Re-auth; no fake refresh; stale badge when >48h |
| Single ticker pull | Page/block | Null/gaps for ticker; continue |
| Snapshot write / export before CURRENT | Disk/error | Abort; keep last good generation |
| Materialize after CURRENT | Promote error | committed + failed/pending; retry `--materialize-current` |
| Git push | Auth/network | pending-publish; retry from CURRENT web/ |
| Pages CDN lag | Old JSON briefly | Expected; cache-bust / wait |

## Internal revision windows (30/60/90D)

Shared helper: `tools/revision_windows.py` — used by both `export_web_data.py` and `build_alerts.py`.

- Identity = ticker + normalized `reportedFiscalPeriodEnding`.
- Anchor = latest valid daily observation ≤ `as_of`; `targetDate = latestDate − N days`.
- Same-day collapse is cutoff-aware: latest `updateTime` ≤ `as_of`, else last append among remaining rows. Calendar `date` is the day bucket.
- Baseline: schedule-aware weekday-gap (`MAX_BASELINE_WEEKDAY_GAP = 1`). Friday→Monday is valid; ancient observations cannot fake a 30D baseline. See the helper module docstring.
- Internal 30D is **daily.jsonl only** — revision-event history is never substituted when daily observations are absent.

## daily.jsonl persistence

Live cache `data/daily_eps_snapshots/daily.jsonl`; committed copy under `data/generations/<runId>/`. Not published to GitHub Pages and not tracked in git. A fresh workspace cannot reconstruct Internal 30/60/90D until weekday collections accumulate on that machine.

## What is NOT in the public repo
Seeking Alpha cookies/sessions, GitHub tokens beyond Actions secrets (none required for static Pages from this machine’s push), `.env`, browser profiles, private raw pulls beyond published JSON fields.

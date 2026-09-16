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
- Missing/unavailable daily history is fail-closed for lifecycle too: do not mint a new Internal 30D and do not resolve a prior open. True `Resolved` requires a valid computed Internal 30D with `|pct| < 4%`. Empty `daily.jsonl` is data loss, not evidence the revision fell below the resolve threshold.

## Durable Git canonical EPS history vs runtime cache

| Path | Role | Git-tracked? |
|------|------|----------------|
| `data/history/eps_daily/YYYY-MM.jsonl` | Canonical durable SoT (monthly append-only). | Yes (source repo) |
| `data/daily_eps_snapshots/daily.jsonl` | Runtime cache. Rebuild: `python3 tools/canonical_eps_history.py --materialize` (fail-closed). | No |
| `data/generations/<runId>/` | Immutable generation; CURRENT is the financial commit. Updated canonical months only — not a full history copy. | No |
| `data/*.json`, `web/data/*.json` | Public derived exports (Pages). | Public JSON only |

Canonical joins financial COMMIT: generation package includes updated month files **before** CURRENT; live `data/history/` updates only after CURRENT. Pre-CURRENT crash → canonical live unchanged. Same identity + different consensus/analysts aborts COMMIT (never first-wins). Corrupt `pending_daily_rows.json` aborts before CURRENT. Source-git persist of `data/history/eps_daily/` is after COMMIT; push failure does not roll back CURRENT; retry is idempotent. Pages publish is unchanged (public `web/` only). Actual pending-publish state is `data/publish_state.json` (docs historically mentioned `data/.pending-publish`, which is gitignored).

## Migration / backfill (PR #13)

```bash
python3 tools/migrate_eps_history.py --audit
python3 tools/migrate_eps_history.py --apply
python3 tools/rebuild_daily_history.py
```

`--audit` is read-only (and takes `data/.pipeline.lock` for a coherent snapshot unless `PIPELINE_LOCK_HELD=1`). `--apply` acquires that same ingest single-writer lock **before planning** and holds it through write. Concurrent apply exits `RUN ALREADY IN PROGRESS` with no mutation. Month-file replaces are transactional: a mid-write failure restores every original month and deletes any newly created month. `--apply` never mutates `data/CURRENT.json`, never invents EPS/fiscal endings, never auto-picks a conflict winner, and writes monthly canonical files only after the full proposed result validates with `conflicts=0`. Identity = ticker + normalized `reportedFiscalPeriodEnding` + date + `updateTime` (PR #12). `reportedFiscalLabel` (`Jan 2027`) is reconstructed via the existing `normalize_fiscal_period_label` mapping — not a fabricated calendar ending. mappedYear/slot is not identity. `seed_from_revision_history` and aborted/orphan generations are not imported. Public `eps_history.json` is used only when exact canonical fields (including `updateTime` or proven absence) are reconstructable.

Fresh clone: Git canonical exists → runtime `daily.jsonl` / generations absent → `python3 tools/rebuild_daily_history.py` → Internal 30/60/90D match recoverable history. Partial history is OK; missing periods stay unavailable.

## daily.jsonl persistence (runtime cache)

Live cache `data/daily_eps_snapshots/daily.jsonl`; committed copy under `data/generations/<runId>/`. Rebuildable from canonical history; not published to GitHub Pages and not tracked in git. After backfill, a fresh clone reconstructs recoverable observations from `data/history/eps_daily/` via `python3 tools/rebuild_daily_history.py`.

## What is NOT in the public repo
Seeking Alpha cookies/sessions, GitHub tokens beyond Actions secrets (none required for static Pages from this machine’s push), `.env`, browser profiles, private raw pulls beyond published JSON fields.

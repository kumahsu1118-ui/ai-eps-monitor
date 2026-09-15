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

**On failure:** Mark collection failed; keep previous consensus; set / keep freshness so **DATA STALE** can appear when past the most recent weekday **08:00 Taipei** collection window + grace without a successful pull; notify user to re-auth.

### 3. Pull consensus & prices
- For each ticker: earnings estimates + revisions (SA **1M/3M/6M** only; never invent 7D/30D/90D).
- Record **Last Close** (regular session) and **After Hours** separately if shown.
- Preserve **Reported Fiscal Period Ending**; map only to FY-mapped calendar **slots** (not true CY EPS).

**On failure for one ticker:** Write `Data unavailable` / null for that name; continue others; list gaps.

### 4. Quality Gate (fail-closed) then persist
- Order: **Collection → Quality Gate → publishable → persist history → Alert Engine → Export → Publish**.
- Browser/collection writes ONLY `data/incoming/<UTC timestamp>.json` (never unvalidated into `data/snapshots/`).
- Single entrypoint: `python3 tools/ingest_snapshot.py` — Incoming → Gate → **stage** `data/staging/<runId>/` → pure-build export (`--no-persistent-mutation`) → **atomic COMMIT** of ALL mutable outputs (validated snapshots, revisions, daily, alerts, comparison checkpoint, web/data; `runStatus=committed`; `data/CURRENT.json` sole commit pointer) → optional `publish_prebuilt_site` (no second export). Pre-CURRENT failure → `runStatus=aborted` (must not become LKG). Post-CURRENT materialization failure → `runStatus=committed` + `materializationStatus=failed` (never aborted; no CURRENT rollback; retry `materialize_generation(resolve_current_generation())`).
- `--validate-only` gates/stages without commit (unsafe `--no-export` commit banned). Historical timestamps require explicit `--backfill`.
- Rejected / needs_verification / future/invalid timestamps → quarantine only; **must not** remain as validated snapshot candidates; **do not** mutate Alert DB or daily EPS history before COMMIT.
- Global lock: `data/.pipeline.lock` (concurrent → `RUN ALREADY IN PROGRESS`). Nested `PIPELINE_LOCK_HELD=1` skips re-acquire for ingest→publish.

### 4b. Persist private database (append-only)
- Append dated file under `data/snapshots/` (never overwrite prior dates).
- **Daily EPS snapshots:** append/update `data/daily_eps_snapshots/` for the calendar day **even if EPS unchanged**.
- **Revision events:** append to `data/revisions/history.jsonl` **only if** consensus EPS actually changed vs prior snapshot. No empty revision rows.

**On failure mid-write:** Prefer leave prior files intact; do not delete `history.jsonl` or earnings digests.

### 5. Earnings digests (persistent)
- Read `data/earnings/{TICKER}.json`.
- **Never** replace a digest with an empty stub on export.
- After a real report, update that ticker’s digest only (sources must include URL + date).

**On failure:** Keep existing digest; flag gap in digest `dataGaps`.

### 6. Export web JSON (via ingest only — single writer)
- **`ingest_snapshot.py` is the sole persistent financial-state writer** (validated snapshots, revisions, daily, alerts, checkpoint, CURRENT).
- Export runs inside ingest as a **read-only pure-build** (`--no-persistent-mutation`) into staging; COMMIT materializes.
- Standalone `export_web_data.py` is **read-only by default** (writes web JSON only; does not mutate daily/revision/alert/checkpoint). Legacy mutation requires explicit `--legacy-mutate` (not for production/scheduler).
- Do **not** run standalone export after ingest as a second writer.

**On failure / qualityGate.publishable!=true:** Do not modify public web/data; quarantine snapshot; keep last-known-good site; exit non-zero; do not git push.
Partial watchlist: missing tickers use last-known-good + FAILED/STALE; collectionStatus=PARTIAL (never COMPLETE with missing tickers).

### 7. Publish to GitHub Pages (publish-only)
```bash
/workspace/ai-eps-monitor/tools/publish_github_pages.sh
# or: python3 tools/ingest_snapshot.py --publish   # after commit
```
- **Publish-only:** no recompute / no history mutation; syncs prebuilt `web/` (prefer CURRENT generation web/).
- Copies **public** assets only into `site-repo/`.
- `git commit` + `git push` **only if files changed** (no empty commits).
- Never commit cookies, tokens, `.env`, browser profiles, SA sessions.
- Pending publish retries **reconcile CURRENT → live** before push.

**On failure (auth/push):** Leave financial commit intact; publishStatus=failed/pending; notify user; retry next run before new collection.

### 8. User digest
- Short Chinese summary: who was revised, alerts (only if material), link to public HTTPS.
- Separates **Consensus Data As Of** vs **Site Published** in messaging when relevant.

## Failure matrix (summary)

| Step | Failure | Handling |
|------|---------|----------|
| SA login | Session dead | Re-auth; no fake refresh; stale badge after weekday 08:00 Taipei + grace |
| Single ticker pull | Page/block | Null/gaps for ticker; continue |
| Snapshot write | Disk/error | Abort publish; keep last good snapshot |
| Export | Script error | Abort publish; notify |
| Git push | Auth/network | Local OK; notify; retry next run |
| Pages CDN lag | Old JSON briefly | Expected; cache-bust / wait |

## What is NOT in the public repo
Seeking Alpha cookies/sessions, GitHub tokens beyond Actions secrets (none required for static Pages from this machine’s push), `.env`, browser profiles, private raw pulls beyond published JSON fields.

## Identity / Deploy / Crash (2026-09-15)

- EPS comparison identity = ticker + Reported Fiscal Period Ending only (mapped slot is UI display)
- Publish syncs full static tree including `web/vendor/**`; `AI_EPS_ROOT` selects project root
- Commit: `data/generations/<runId>/` then atomic `CURRENT.json`; materialize live after; SIGKILL-safe
- Publish status: `data/publish_state.json` pending/published/failed (no financial rollback on push fail)


## Commit Semantics + Single Writer + Release Identity (2026-09-15)

- **CURRENT flip = financial commit DONE.** Post-CURRENT materialize failure → committed + materializationStatus=failed/pending (never aborted; no CURRENT rollback).
- **Single writer:** `ingest_snapshot.py` only. `export_web_data.py` read-only by default; `publish_github_pages.sh` publish-only.
- **Readers:** `ensure_live_matches_current()` verifies marker AND required CURRENT artifacts/build identity before trusting live cache; otherwise rematerialize from CURRENT (incl. pending publish retry).
- **runId:** unconditionally unique (UTC microseconds + full snapshot hash + UUID). Existing `generations/<runId>/` is immutable; collision fail-closed (never overwrite-in-place).
- **Release identity:** stamp cache-bust then finalize; `appVersion` hashes canonicalized index (strip `?v=`) + assets; `releaseVersion = hash(appVersion|schema|data|refresh)`.
- **Official domains:** Tier1 only `officialDomainsByTicker[ticker]` (or any listed domain when ticker unknown). Explicit `sourceType` cannot bypass hostname checks when a URL is present. Generic `investor.*`/`ir.*` → `unverified_ir_candidate`. Seeking Alpha host must be `seekingalpha.com` / `*.seekingalpha.com` (no substring).
- **Parser:** duplicate identical fiscal rows dedupe; conflicting → `needs_verification` / `duplicate_conflicting_fiscal_row`; slot collision → `mapped_slot_collision`.

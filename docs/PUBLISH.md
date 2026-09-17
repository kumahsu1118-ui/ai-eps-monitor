# Publish GitHub Pages from `web/`

This repo serves GitHub Pages from the **repository root**.

Source of truth:

- SPA: `web/index.html`, `web/app.js`, `web/styles.css`
- Exported JSON: `web/data/*.json`
- Pipeline: `data/snapshots`, `data/revisions`, `data/drivers`, `data/earnings`, `data/alerts`
- Canonical EPS history (source repo, **not** the Pages payload): `data/history/eps_daily/*.jsonl`


To refresh the public site:

```bash
python3 tools/ingest_snapshot.py --publish
# or, publish-only from the committed CURRENT generation:
bash tools/publish_github_pages.sh
```

Standalone `python3 tools/export_web_data.py` is read-only (no daily/alerts persist) unless `--legacy-mutate`.
`publish_github_pages.sh` is publish-only unless `--legacy-mutate`.

Publish copies CURRENT generation `web/` (full static tree, cache-bust `?v=`) into a disposable worktree of latest `origin/main` (and `site-repo/` after remote verify). Publish stamps are produced in a disposable staging tree first; live `meta.json` / `dashboard.json.meta` / `sitePublished` / `canonicalHeadSha` / `.last-publish-web` / `.data-version` are updated only after remote verification. `--preflight` is read-only (no rematerialize, no live cache rewrite). Without a Git repository/remote the publisher exits non-zero; local fixture overlay requires an explicit test entrypoint (`python3 tools/publish_release.py` with `PUBLISH_ALLOW_LOCAL=1`). Formal `publish_github_pages.sh` refuses/unsets `PUBLISH_ALLOW_LOCAL` and fails closed even if the flag is inherited. Publish is fail-closed: missing/corrupt/dangling CURRENT, rematerialize failure, pending/failed canonical git state, unpushed canonical commits, dirty `data/history/eps_daily`, remote missing CURRENT canonical observations, stale/malformed collection timestamps, commit/push failure, or remote verification mismatch all exit non-zero with no `.data-version` advance and no false `NO_CHANGES`. Bounded fetch-and-rebuild retry on non-fast-forward; never force-push. `canonicalHeadSha` is stamped only after proving that commit contains CURRENT-required canonical history.

Pending-publish retries use `ensure_live_matches_current` and prefer CURRENT `web/` over a drifted live tree. Canonical source-git persist uses a disposable worktree of latest `origin/main` and never `git reset --hard` the source clone (live `web/data` and other tracked modifications stay put; source HEAD may lag). Release metadata stamped on success includes `generationRunId`, `canonicalHeadSha`, `appVersion`, `dataVersion`, `refreshVersion`, `releaseVersion`, and collection timestamp.

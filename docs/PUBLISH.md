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

Publish copies CURRENT generation `web/` (full static tree, cache-bust `?v=`) into `site-repo/` / Pages root. Pending-publish retries use `ensure_live_matches_current` and prefer CURRENT `web/` over a drifted live tree.

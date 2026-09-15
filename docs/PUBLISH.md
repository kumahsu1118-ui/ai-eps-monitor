# Publish GitHub Pages from `web/`

This repo serves GitHub Pages from the **repository root**.

Source of truth:

- SPA: `web/index.html`, `web/app.js`, `web/styles.css`
- Exported JSON: `web/data/*.json`
- Pipeline: `data/snapshots`, `data/revisions`, `data/drivers`, `data/earnings`, `data/alerts`

To refresh the public site:

```bash
python3 tools/export_web_data.py
bash tools/sync_pages_root.sh
git add index.html 404.html app.js styles.css data/*.json .data-version web/data
git commit -m "Update dashboard data"
git push
```

`sync_pages_root.sh` copies public JSON onto `data/*.json` without deleting pipeline subdirectories.

`tools/publish_github_pages.sh` is a wrapper around `sync_pages_root.sh` (this repo is the Pages repo; there is no separate `site-repo` clone).

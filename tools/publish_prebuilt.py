#!/usr/bin/env python3
"""Publish already-exported web/ assets. Does NOT recompute or re-export.

ingest --publish / ingest_and_publish runs export once, then this copier.
`.data-version` is written ONLY after a successful push so a failed push
can be retried (hash mismatch vs missing version file).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))


def resolve_root(start: Path | None = None) -> Path:
    cand = start or Path(__file__).resolve().parent
    for _ in range(6):
        if (cand / "web").exists() and (cand / "tools").exists():
            return cand
        cand = cand.parent
    return Path(__file__).resolve().parent.parent


def public_payload_hash(root: Path | str) -> str:
    root = Path(root)
    web = root / "web"
    h = hashlib.sha256()

    def feed_bytes(b: bytes) -> None:
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)

    for name in ("index.html", "app.js", "styles.css"):
        p = web / name
        if p.exists():
            feed_bytes(p.read_bytes())
    meta_path = web / "data" / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if meta.get("dataVersion"):
        feed_bytes(str(meta["dataVersion"]).encode("utf-8"))
    else:
        for name in (
            "companies.json",
            "valuation.json",
            "revisions.json",
            "eps_history.json",
            "earnings.json",
            "watchlist.json",
        ):
            p = web / "data" / name
            if p.exists():
                feed_bytes(p.read_bytes())
        ap = web / "data" / "alerts.json"
        if ap.exists():
            alerts = json.loads(ap.read_text(encoding="utf-8"))
            feed_bytes(
                json.dumps(
                    {
                        "activeAlerts": alerts.get("activeAlerts") or alerts.get("alerts") or [],
                        "alertEngineStatus": alerts.get("alertEngineStatus"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    return h.hexdigest()


def _copy_prebuilt(web: Path, repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "data").mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "styles.css", "app.js"):
        src = web / name
        if src.exists():
            shutil.copy2(src, repo / name)
    data_src = web / "data"
    if data_src.exists():
        for src in data_src.iterdir():
            dest = repo / "data" / src.name
            if src.is_file():
                shutil.copy2(src, dest)
    if (repo / "index.html").exists():
        shutil.copy2(repo / "index.html", repo / "404.html")
    (repo / ".nojekyll").touch()
    for junk in (repo / "ORIGIN.txt", repo / "overview-verify.png", repo / "data" / ".pipeline.lock"):
        if junk.exists():
            junk.unlink()


def _default_git_push(repo: Path) -> None:
    env = os.environ.copy()
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo), env=env)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo), env=env)
    if diff.returncode == 0:
        return
    subprocess.check_call(
        [
            "git",
            "-c",
            "user.email=kumahsu1118-ui@users.noreply.github.com",
            "-c",
            "user.name=AI EPS Monitor",
            "commit",
            "-m",
            "Update dashboard data",
        ],
        cwd=str(repo),
        env=env,
    )
    subprocess.check_call(["git", "push", "origin", "HEAD"], cwd=str(repo), env=env)


def publish_prebuilt_site(
    root: Path | str | None = None,
    *,
    site_repo: Path | str | None = None,
    git_push=None,
    stamp: bool = True,
    skip_git: bool = False,
) -> dict:
    """Copy exported web/ → site-repo. Never calls export_web_data or ingest.

    git_push: optional callable(repo: Path) used instead of git push.
    Raises from git_push leave `.data-version` unwritten so retry still pushes.
    """
    root = Path(root) if root else resolve_root()
    web = root / "web"
    repo = Path(site_repo) if site_repo else (root / "site-repo")
    version_file = repo / ".data-version"

    payload_hash = public_payload_hash(root)
    prev = ""
    if version_file.exists():
        prev = version_file.read_text(encoding="utf-8").strip()
    if prev and prev == payload_hash:
        return {"ok": True, "noop": True, "pushed": False, "hash": payload_hash}

    if stamp:
        try:
            from publish_metadata import stamp_site_published

            stamp_site_published(web / "data")
        except Exception:
            pass

    _copy_prebuilt(web, repo)

    result = {
        "ok": True,
        "noop": False,
        "pushed": False,
        "hash": payload_hash,
        "versionWritten": False,
    }
    if skip_git and git_push is None:
        # Test helper: treat copy as success without writing .data-version
        # unless a push callback is provided.
        return result

    pusher = git_push if git_push is not None else _default_git_push
    try:
        pusher(repo)
    except Exception as exc:
        result["ok"] = False
        result["pushed"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["versionWritten"] = False
        # Intentionally do NOT write .data-version — retry must still push.
        return result

    version_file.write_text(payload_hash + "\n", encoding="utf-8")
    result["pushed"] = True
    result["versionWritten"] = True
    result["ok"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = resolve_root()
    if args:
        root = Path(args[0])
    result = publish_prebuilt_site(root)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

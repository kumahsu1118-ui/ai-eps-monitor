#!/usr/bin/env python3
"""Parse index.html assets, hash app/release versions, cache-bust, publish tree.

Identity:
  appVersion     = sha256 of the static shell including vendor/**
  releaseVersion = sha256 of appVersion + public data payload versions
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# Local asset refs in <link href> / <script src> / <img src>
_ATTR_RE = re.compile(
    r"""(?P<tag><(?:link|script|img)\b[^>]*?\b(?:href|src)\s*=\s*)(?P<q>['"])(?P<url>[^'"]+)(?P=q)""",
    re.IGNORECASE | re.DOTALL,
)


def is_local_asset(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    lower = u.lower()
    if lower.startswith(("http://", "https://", "//", "data:", "mailto:", "javascript:")):
        return False
    if u.startswith("#"):
        return False
    return True


def strip_query(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0]


def normalize_asset_path(url: str) -> str:
    path = strip_query(url).replace("\\", "/").lstrip("./")
    while path.startswith("/"):
        path = path[1:]
    return path


def parse_index_assets(index_html: str) -> list[str]:
    """Return unique local asset paths referenced by index.html (href/src)."""
    seen: list[str] = []
    found: set[str] = set()
    for m in _ATTR_RE.finditer(index_html or ""):
        url = m.group("url")
        if not is_local_asset(url):
            continue
        rel = normalize_asset_path(url)
        if not rel or rel in found:
            continue
        found.add(rel)
        seen.append(rel)
    return seen


def iter_vendor_files(web_dir: Path) -> list[Path]:
    vendor = Path(web_dir) / "vendor"
    if not vendor.exists():
        return []
    files = [p for p in vendor.rglob("*") if p.is_file()]
    files.sort(key=lambda p: str(p.relative_to(web_dir)).replace("\\", "/"))
    return files


def list_static_shell_files(web_dir: Path, *, index_name: str = "index.html") -> list[Path]:
    """Static files that make up appVersion: index + parsed assets + vendor/**."""
    web_dir = Path(web_dir)
    index_path = web_dir / index_name
    files: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            return
        if p.is_file():
            seen.add(key)
            files.append(p)

    if index_path.is_file():
        add(index_path)
        html = index_path.read_text(encoding="utf-8")
        for rel in parse_index_assets(html):
            add(web_dir / rel)
    else:
        for name in ("index.html", "app.js", "styles.css"):
            add(web_dir / name)

    for p in iter_vendor_files(web_dir):
        add(p)
    return files


def _feed(h, data: bytes) -> None:
    h.update(len(data).to_bytes(8, "big"))
    h.update(data)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def compute_app_version(web_dir: Path) -> str:
    """sha256 of the static shell including vendor/** and every index asset."""
    web_dir = Path(web_dir)
    h = hashlib.sha256()
    files = list_static_shell_files(web_dir)
    for p in files:
        rel = str(p.relative_to(web_dir)).replace("\\", "/")
        _feed(h, rel.encode("utf-8"))
        _feed(h, p.read_bytes())
    return h.hexdigest()


def compute_release_version(
    web_dir: Path,
    *,
    app_version: str | None = None,
    data_version: str | None = None,
    refresh_version: str | None = None,
) -> str:
    """sha256 of appVersion + public data identity (dataVersion/refreshVersion)."""
    web_dir = Path(web_dir)
    app_v = app_version or compute_app_version(web_dir)
    meta_path = web_dir / "data" / "meta.json"
    dv = data_version
    rv = refresh_version
    if meta_path.exists() and (dv is None or rv is None):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        if dv is None:
            dv = meta.get("dataVersion") or ""
        if rv is None:
            rv = meta.get("refreshVersion") or ""
    h = hashlib.sha256()
    _feed(h, app_v.encode("utf-8"))
    _feed(h, str(dv or "").encode("utf-8"))
    _feed(h, str(rv or "").encode("utf-8"))
    return h.hexdigest()


def compute_publish_hash(web_dir: Path) -> str:
    """Content hash that must change when vendor/static shell OR data versions change."""
    web_dir = Path(web_dir)
    app_v = compute_app_version(web_dir)
    return compute_release_version(web_dir, app_version=app_v)


def cache_bust_html(html: str, web_dir: Path) -> str:
    """Rewrite local href/src to append ?v=<file sha256 prefix>."""
    web_dir = Path(web_dir)

    def repl(m: re.Match) -> str:
        prefix, quote, url = m.group("tag"), m.group("q"), m.group("url")
        if not is_local_asset(url):
            return m.group(0)
        rel = normalize_asset_path(url)
        target = web_dir / rel
        if not target.is_file():
            return m.group(0)
        digest = file_sha256(target)[:12]
        fragment = ""
        if "#" in url:
            fragment = "#" + url.split("#", 1)[1]
        return f"{prefix}{quote}{rel}?v={digest}{fragment}{quote}"

    return _ATTR_RE.sub(repl, html)


def copy_static_tree(web_dir: Path, dest: Path) -> list[str]:
    """Copy index assets + vendor/** + public data into dest. Returns copied relative paths."""
    import shutil

    web_dir = Path(web_dir)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    def copy_one(src: Path, rel: str) -> None:
        if not src.is_file():
            return
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(rel.replace("\\", "/"))

    index_src = web_dir / "index.html"
    html = index_src.read_text(encoding="utf-8") if index_src.is_file() else ""
    busted = cache_bust_html(html, web_dir) if html else html
    if html:
        (dest / "index.html").write_text(busted, encoding="utf-8")
        copied.append("index.html")
        (dest / "404.html").write_text(busted, encoding="utf-8")
        copied.append("404.html")

    for rel in parse_index_assets(html):
        if rel in {"index.html", "404.html"}:
            continue
        copy_one(web_dir / rel, rel)

    for p in iter_vendor_files(web_dir):
        rel = str(p.relative_to(web_dir)).replace("\\", "/")
        copy_one(p, rel)

    # Always copy well-known shell files even if index omitted them
    for name in ("app.js", "styles.css"):
        copy_one(web_dir / name, name)

    data_src = web_dir / "data"
    if data_src.exists():
        (dest / "data").mkdir(parents=True, exist_ok=True)
        for p in data_src.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(web_dir)).replace("\\", "/")
                copy_one(p, rel)

    (dest / ".nojekyll").touch()
    return copied

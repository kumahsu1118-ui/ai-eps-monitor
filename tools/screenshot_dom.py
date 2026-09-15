#!/usr/bin/env python3
"""Screenshot DOM sidecars + content secret scan.

Sidecars capture build identity (dataVersion / buildId) and whether
#meta-collection-status-row is hidden when collectionStatus is complete.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("openai_sk", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+")),
    ("sa_session", re.compile(r"(?i)seeking.?alpha.{0,40}(cookie|session|token)")),
    ("bearer", re.compile(r"(?i)authorization:\s*bearer\s+\S+")),
    ("env_secret", re.compile(r"(?i)(api[_-]?key|secret[_-]?key|auth[_-]?token)\s*[=:]\s*['\"][^'\"]{8,}")),
]


def collection_status_row_should_hide(collection_status: str | None) -> bool:
    status = str(collection_status or "").strip().lower()
    return status in {"", "complete", "ok", "success"}


def scan_content_secrets(text: str | None) -> dict:
    hits = []
    blob = text or ""
    for name, pat in SECRET_PATTERNS:
        for m in pat.finditer(blob):
            hits.append({"rule": name, "span": m.group(0)[:80]})
    return {"ok": len(hits) == 0, "hits": hits}


def build_dom_sidecar(
    meta: dict,
    *,
    html: str | None = None,
    extra_text: str | None = None,
    screenshot: str | None = None,
) -> dict:
    status = meta.get("collectionStatus")
    hidden = collection_status_row_should_hide(status)
    scan_parts = [html or "", extra_text or "", json.dumps(meta, ensure_ascii=False)]
    scan = scan_content_secrets("\n".join(scan_parts))
    return {
        "screenshot": screenshot,
        "dataVersion": meta.get("dataVersion"),
        "buildId": meta.get("buildId") or meta.get("dataVersion"),
        "refreshVersion": meta.get("refreshVersion"),
        "collectionStatus": status,
        "collectionStatusLabel": meta.get("collectionStatusLabel"),
        "selectors": {
            "#meta-collection-status-row": {
                "hidden": hidden,
                "hideWhen": "collectionStatus == complete",
            }
        },
        "secretScan": scan,
    }


def write_screenshot_sidecars(
    screenshots_dir: Path | str,
    meta: dict,
    *,
    html: str | None = None,
) -> list[Path]:
    """Write {stem}.dom.json next to each *-latest.png (or any .png)."""
    screenshots_dir = Path(screenshots_dir)
    written = []
    if not screenshots_dir.exists():
        return written
    pngs = sorted(screenshots_dir.glob("*-latest.png")) or sorted(screenshots_dir.glob("*.png"))
    for png in pngs:
        sidecar = build_dom_sidecar(meta, html=html, screenshot=png.name)
        dest = png.with_suffix(".dom.json")
        dest.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(dest)
    return written

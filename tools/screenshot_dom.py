#!/usr/bin/env python3
"""Screenshot DOM sidecars with release identity (schema/app/release versions)."""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    from schema_versions import version_fields
except Exception:  # pragma: no cover
    def version_fields() -> dict:  # type: ignore
        return {"schemaVersion": "1", "appVersion": "1.0.0", "releaseVersion": "1.0.0", "supportedSchemaVersion": "1"}

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("openai_sk", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+")),
    ("sa_session", re.compile(r"(?i)seeking.?alpha.{0,40}(cookie|session|token)")),
    ("bearer", re.compile(r"(?i)authorization:\s*bearer\s+\S+")),
]


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
    route: str | None = None,
) -> dict:
    vers = version_fields()
    scan = scan_content_secrets("\n".join([html or "", extra_text or "", json.dumps(meta, ensure_ascii=False)]))
    return {
        "screenshot": screenshot,
        "route": route,
        "dataVersion": meta.get("dataVersion"),
        "buildId": meta.get("buildId") or meta.get("dataVersion"),
        "refreshVersion": meta.get("refreshVersion"),
        "sitePublished": meta.get("sitePublished"),
        "schemaVersion": meta.get("schemaVersion") or vers["schemaVersion"],
        "appVersion": meta.get("appVersion") or vers["appVersion"],
        "releaseVersion": meta.get("releaseVersion") or vers["releaseVersion"],
        "supportedSchemaVersion": meta.get("supportedSchemaVersion") or vers.get("supportedSchemaVersion"),
        "secretScan": scan,
    }


def write_screenshot_sidecars(
    screenshots_dir: Path | str,
    meta: dict,
    *,
    html: str | None = None,
) -> list[Path]:
    screenshots_dir = Path(screenshots_dir)
    written: list[Path] = []
    if not screenshots_dir.exists():
        return written
    pngs = sorted(screenshots_dir.glob("*-latest.png")) or sorted(screenshots_dir.glob("*.png"))
    for png in pngs:
        sidecar = build_dom_sidecar(meta, html=html, screenshot=png.name)
        dest = png.with_suffix(".dom.json")
        dest.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        written.append(dest)
    return written

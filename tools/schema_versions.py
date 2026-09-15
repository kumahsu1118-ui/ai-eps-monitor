#!/usr/bin/env python3
"""Release identity: schemaVersion / appVersion / releaseVersion.

SUPPORTED_SCHEMA_VERSION is fail-closed: unknown versions abort ingest/export/UI.
"""
from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSION = "1"
APP_VERSION = "1.0.0"

_SUPPORTED = frozenset({"1", "1.0", "1.0.0"})


class SchemaVersionError(ValueError):
    """Payload schemaVersion is missing from the supported set."""


def _read_app_version() -> str:
    env = os.environ.get("APP_VERSION")
    if env:
        return env.strip()
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "VERSION", Path(os.environ.get("AIEPS_ROOT") or ".") / "VERSION"):
        try:
            text = cand.read_text(encoding="utf-8").strip()
            if text:
                return text.splitlines()[0].strip()
        except Exception:
            continue
    return APP_VERSION


def release_version() -> str:
    return (
        os.environ.get("RELEASE_VERSION")
        or os.environ.get("GITHUB_SHA")
        or _read_app_version()
    )


def app_version() -> str:
    return _read_app_version()


def schema_supported(version) -> bool:
    if version is None:
        return False
    v = str(version).strip()
    if not v:
        return False
    if v in _SUPPORTED:
        return True
    s = str(SUPPORTED_SCHEMA_VERSION)
    return v == s or v.startswith(s + ".")


def assert_supported_schema(version, *, allow_missing: bool = False) -> str:
    """Fail-closed unless version is in SUPPORTED_SCHEMA_VERSION (or missing+legacy)."""
    if version is None or str(version).strip() == "":
        if allow_missing:
            return SUPPORTED_SCHEMA_VERSION
        raise SchemaVersionError(
            f"schemaVersion missing — SUPPORTED_SCHEMA_VERSION={SUPPORTED_SCHEMA_VERSION}"
        )
    v = str(version).strip()
    if not schema_supported(v):
        raise SchemaVersionError(
            f"unsupported schemaVersion={v!r} (SUPPORTED_SCHEMA_VERSION={SUPPORTED_SCHEMA_VERSION})"
        )
    return v


def version_fields() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "appVersion": app_version(),
        "releaseVersion": release_version(),
        "supportedSchemaVersion": SUPPORTED_SCHEMA_VERSION,
    }

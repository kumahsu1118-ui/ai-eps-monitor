#!/usr/bin/env python3
"""Domain classifier for source URLs.

Tier 1 = company IR / SEC primary filings — NOT newswires.
Reuters, Bloomberg, WSJ, CNBC are tertiary (Tier 3), never Tier 1.
Seeking Alpha is Tier 4 supplemental.
"""
from __future__ import annotations

from urllib.parse import urlparse


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(str(url).strip())
        host = (parsed.netloc or parsed.path or "").lower()
    except Exception:
        host = str(url).lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _as_int_tier(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if 1 <= n <= 5 else None
    s = str(value).strip().lower()
    if s.startswith("t") and s[1:].isdigit():
        n = int(s[1:])
        return n if 1 <= n <= 5 else None
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 5 else None
    return None


def classify_source_tier(
    url: str | None = None,
    *,
    attribution: str | None = None,
    explicit_tier=None,
) -> int | None:
    """Return a source tier for a URL / attribution.

    Reuters (and other newswires) are never Tier 1, even if a payload
    stamps `sourceTier: 1`.
    """
    host = _host(url)
    attr = str(attribution or "").lower()
    blob = f"{host} {attr} {url or ''}".lower()

    # Newswires / tertiary — never IR/SEC.
    if "reuters.com" in host or "reuters" in attr or "reuters" in blob.split("/")[0]:
        return 3
    if any(h in host for h in ("bloomberg.com", "wsj.com", "cnbc.com", "ft.com")):
        return 3

    # Seeking Alpha — supplemental
    if "seekingalpha.com" in host or "seeking alpha" in attr:
        return 4
    if any(h in host for h in ("x.com", "twitter.com", "substack.com")):
        return 4

    # Primary: SEC EDGAR and company IR hosts
    if "sec.gov" in host or host.endswith(".sec.gov"):
        return 1
    if host.startswith("investor.") or host.startswith("ir.") or ".investor." in host:
        return 1
    if "/investor" in str(url or "").lower() and any(
        tok in host for tok in (".com", ".net", ".org")
    ) and "seekingalpha" not in host and "reuters" not in host:
        # company IR path on the issuer domain
        if not any(h in host for h in ("bloomberg.com", "wsj.com", "cnbc.com")):
            return 1

    classified = None
    if "sec.gov" in blob or "8-k" in attr or "10-q" in attr or "10-k" in attr:
        classified = 1
    elif "company ir" in attr or "investor relations" in attr:
        classified = 1

    explicit = _as_int_tier(explicit_tier)
    if explicit is not None:
        # Domain wins when it would demote a newswire claiming Tier 1.
        if classified is not None:
            return classified
        return explicit
    return classified

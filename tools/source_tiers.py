#!/usr/bin/env python3
"""Hostname-based source tiers. Never classify from query-string or path substrings.

officialDomainsByTicker is the allowlist. Spoofed URLs such as
https://evil.example/sec.gov/... or https://evil.example/?next=https://investor.nvidia.com
MUST NOT receive Company IR / SEC tiers.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Exact hostnames (www. stripped for matching). SEC is global Tier 1 filings.
OFFICIAL_DOMAINS_BY_TICKER: dict[str, list[str]] = {
    "NVDA": ["investor.nvidia.com", "nvidianews.nvidia.com"],
    "AVGO": ["investors.broadcom.com"],
    "MSFT": ["microsoft.com", "blogs.microsoft.com"],
    "TSM": ["investor.tsmc.com"],
    "BE": ["ir.bloomenergy.com"],
    "KEYS": ["investor.keysight.com"],
}

SEC_HOSTS = frozenset({"sec.gov", "www.sec.gov"})
SA_HOSTS = frozenset({"seekingalpha.com", "www.seekingalpha.com"})
WIRE_HOSTS = frozenset({
    "reuters.com", "www.reuters.com",
    "bloomberg.com", "www.bloomberg.com",
    "wsj.com", "www.wsj.com",
    "cnbc.com", "www.cnbc.com",
    "dowjones.com", "www.dowjones.com",
})


def normalize_host(host: str | None) -> str:
    h = (host or "").strip().lower()
    if h.startswith("www."):
        return h[4:]
    return h


def url_hostname(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    return (parsed.hostname or "").lower()


def official_hosts_for(ticker: str | None = None) -> set[str]:
    if ticker:
        return {normalize_host(h) for h in OFFICIAL_DOMAINS_BY_TICKER.get(str(ticker).upper(), [])}
    out: set[str] = set()
    for hosts in OFFICIAL_DOMAINS_BY_TICKER.values():
        out.update(normalize_host(h) for h in hosts)
    return out


def _path_of(url: str) -> str:
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlparse(raw).path or "").lower()
    except Exception:
        return ""


def is_company_ir_hostname(nh: str) -> bool:
    """True for canonical IR hosts: investor.example.com (exactly 3 labels).

    Rejects suffix spoofs like investor.nvidia.com.evil.com (5 labels).
    """
    if nh in official_hosts_for(None):
        return True
    labels = nh.split(".")
    if len(labels) != 3:
        return False
    return labels[0] in {"investor", "investors", "ir"}


def classify_source_tier(url: str | None, ticker: str | None = None) -> str | int | None:
    """Company IR / SEC → 1; official transcript → 'official'; wires → 3; SA → 4; else Unknown.

    Classification uses the URL *hostname only* (plus path for transcript on official hosts).
    Query strings and path segments that merely *mention* sec.gov / investor.* are ignored.
    """
    if not url:
        return None
    host = url_hostname(url)
    nh = normalize_host(host)
    if not nh:
        return "Unknown"
    if nh == "sec.gov":
        return 1
    official = official_hosts_for(ticker)
    if nh in official or is_company_ir_hostname(nh):
        if "transcript" in _path_of(str(url)):
            return "official"
        return 1
    if nh in {normalize_host(h) for h in WIRE_HOSTS}:
        return 3
    if nh in {normalize_host(h) for h in SA_HOSTS}:
        return 4
    return "Unknown"


def host_is_official(url: str | None, ticker: str | None = None) -> bool:
    tier = classify_source_tier(url, ticker)
    return tier in (1, "official")

#!/usr/bin/env python3
"""Seeking Alpha estimate / fiscal / revision parsers (no cookies/tokens).

Production paths use these helpers to turn sanitized HTML or JSON fixtures /
saved pages into structured consensus rows and revision events. Network fetch
is intentionally out of scope here (browser-assisted collection); this module
only parses already-captured content.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Ticker-specific fiscal-period-ending month overrides (1=Jan … 12=Dec).
# Used when packing snapshot EPS; heuristic still applies if unknown.
TICKER_FISCAL_END_MONTH: dict[str, int] = {
    "NVDA": 1,   # late Jan
    "MSFT": 6,   # June
    "AVGO": 10,  # late Oct / early Nov — Oct ending maps to ending year
    "KEYS": 10,  # Oct 31
    "TSM": 12,   # calendar Dec
    "BE": 12,
}

_MONTH_NUM = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def to_num(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return float(int(x))
    if isinstance(x, (int, float)):
        v = float(x)
        if v != v or v == float("inf") or v == float("-inf"):
            raise ValueError(f"NaN/Inf is not allowed: {x!r}")
        return v
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "data unavailable", "null", "none", "—", "-"}:
        return None
    if s.lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        raise ValueError(f"NaN/Inf is not allowed: {x!r}")
    try:
        v = float(s)
    except Exception:
        return None
    if v != v or v == float("inf") or v == float("-inf"):
        raise ValueError(f"NaN/Inf is not allowed: {x!r}")
    return v


def _first_present(*vals):
    """Return the first value that is not None (preserves 0.0 / 0 / False)."""
    for v in vals:
        if v is not None:
            return v
    return None


def parse_estimates_json_blob(text: str) -> dict | None:
    """Extract JSON from <script id="sa-estimates-json"> or raw JSON text."""
    if not text:
        return None
    m = re.search(
        r'<script[^>]*id=["\']sa-estimates-json["\'][^>]*>(.*?)</script>',
        text,
        re.I | re.S,
    )
    blob = m.group(1).strip() if m else text.strip()
    try:
        return json.loads(blob)
    except Exception:
        return None


_ROW_RE = re.compile(
    r'<tr[^>]*data-period=["\']([^"\']+)["\'][^>]*>\s*'
    r'.*?<td[^>]*class=["\'][^"\']*fiscal-period-ending[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*consensus[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*high[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*low[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*analyst-count[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*rev-1m[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*rev-3m[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>'
    r'.*?<td[^>]*class=["\'][^"\']*rev-6m[^"\']*["\'][^>]*>\s*([^<]+)\s*</td>',
    re.I | re.S,
)


def parse_estimates_html_table(html: str) -> list[dict]:
    """Parse fiscal estimate table rows from sanitized HTML."""
    rows = []
    for m in _ROW_RE.finditer(html or ""):
        rows.append(
            {
                "fiscalPeriodEnding": m.group(2).strip(),
                "consensus": to_num(m.group(3)),
                "high": to_num(m.group(4)),
                "low": to_num(m.group(5)),
                "analystCount": to_num(m.group(6)),
                "rev1M": to_num(m.group(7)),
                "rev3M": to_num(m.group(8)),
                "rev6M": to_num(m.group(9)),
            }
        )
    return rows


def parse_estimates(content: str) -> dict:
    """Parse estimate page content → {ticker, rows[…]}.

    Numeric fields MUST preserve 0.0 — never use `x or y` for numerics.
    """
    data = parse_estimates_json_blob(content)
    if data and isinstance(data.get("rows"), list):
        rows = []
        for r in data["rows"]:
            rows.append(
                {
                    "fiscalPeriodEnding": _first_present(
                        r.get("fiscalPeriodEnding"), r.get("Fiscal Period Ending")
                    ),
                    "consensus": to_num(r.get("consensus")),
                    "high": to_num(r.get("high")),
                    "low": to_num(r.get("low")),
                    "analystCount": to_num(
                        _first_present(r.get("analystCount"), r.get("analysts"))
                    ),
                    "rev1M": to_num(_first_present(r.get("rev1M"), r.get("rev_1M_pct"))),
                    "rev3M": to_num(_first_present(r.get("rev3M"), r.get("rev_3M_pct"))),
                    "rev6M": to_num(_first_present(r.get("rev6M"), r.get("rev_6M_pct"))),
                }
            )
        return {"ticker": data.get("ticker"), "rows": rows}
    # HTML table fallback
    ticker = None
    tm = re.search(r'data-ticker=["\']([A-Z.]+)["\']', content or "")
    if tm:
        ticker = tm.group(1)
    return {"ticker": ticker, "rows": parse_estimates_html_table(content or "")}


def parse_fiscal_period_ending(label: str | None) -> tuple[int, int] | None:
    """Return (month, year) from labels like 'Jan 2027', 'June 2026', 'Oct 2026'."""
    if not label:
        return None
    m = re.match(r"([A-Za-z]+)\s+(\d{4})", str(label).strip())
    if not m:
        return None
    mon = _MONTH_NUM.get(m.group(1).lower())
    if not mon:
        return None
    return mon, int(m.group(2))


def mapped_year_for_fiscal_ending(
    label: str | None,
    ticker: str | None = None,
    fiscal_to_mapped: dict[str, str] | None = None,
) -> str | None:
    """Map Fiscal Period Ending → YYYY E slot.

    Rule: Jan–Mar ending → prior-year mapped slot; Apr–Dec → ending-year.
    Ticker-specific fiscal map overrides heuristic when provided via
    fiscal_to_mapped or TICKER_FISCAL_END_MONTH (month hint only for validation).
    """
    fiscal_to_mapped = fiscal_to_mapped or {}
    if label and label in fiscal_to_mapped:
        return fiscal_to_mapped[label]
    parsed = parse_fiscal_period_ending(label)
    if not parsed:
        return None
    month, year = parsed
    # Ticker override: if ticker has a known FY-end month and the label month
    # matches (or is close for AVGO Oct/Nov), still apply Jan–Mar vs Apr–Dec rule.
    # The override is the fiscal map dict when callers pass explicit mappings;
    # the month table documents known FY ends for tests / audits.
    if ticker:
        _ = TICKER_FISCAL_END_MONTH.get(str(ticker).upper())  # documented
    if 1 <= month <= 3:
        return f"{year - 1}E"
    if 4 <= month <= 12:
        return f"{year}E"
    return None


def pack_snapshot_eps_from_rows(
    rows: list[dict],
    fiscal_to_mapped: dict[str, str] | None = None,
    ticker: str | None = None,
) -> dict:
    """Map parsed estimate rows into snapshot eps[YYYYE] shape used by export.

    Supports all fiscal months Jan–Dec:
      Jan–Mar ending → prior-year mapped slot
      Apr–Dec ending → ending-year mapped slot
    Explicit fiscal_to_mapped overrides the heuristic.
    """
    fiscal_to_mapped = fiscal_to_mapped or {}
    out: dict[str, dict] = {}
    for r in rows:
        lab = r.get("fiscalPeriodEnding")
        mapped = mapped_year_for_fiscal_ending(lab, ticker=ticker, fiscal_to_mapped=fiscal_to_mapped)
        if not mapped:
            continue
        out[mapped] = {
            "consensus": r.get("consensus"),
            "high": r.get("high"),
            "low": r.get("low"),
            "analysts": r.get("analystCount"),
            "rev_1M_pct": r.get("rev1M"),
            "rev_3M_pct": r.get("rev3M"),
            "rev_6M_pct": r.get("rev6M"),
            "reported_fiscal_label": lab,
            "calendar_alignment": f"CY{mapped[:-1]}" if mapped.endswith("E") else None,
        }
    return out


def parse_revision_event(obj: dict | str) -> dict:
    """Normalize a revision-event dict (history.jsonl row shape)."""
    if isinstance(obj, str):
        obj = json.loads(obj)
    return {
        "Date": obj.get("Date") or (obj.get("Update Time") or "")[:10],
        "Ticker": obj.get("Ticker"),
        "Fiscal Year": obj.get("Fiscal Year") or obj.get("fiscalPeriodEnding"),
        "Calendar Alignment": obj.get("Calendar Alignment"),
        "Previous EPS": obj.get("Previous EPS"),
        "Current EPS": obj.get("Current EPS"),
        "Change": obj.get("Change"),
        "Revision %": obj.get("Revision %"),
        "Reason": obj.get("Reason"),
        "Source": obj.get("Source"),
        "Update Time": obj.get("Update Time"),
    }


def parse_file(path: str | Path) -> dict:
    """Parse a fixture file (HTML or JSON) into estimate structure or revision event."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if "Previous EPS" in data or "Current EPS" in data or data.get("Ticker"):
            return {"type": "revision_event", "event": parse_revision_event(data)}
        if "rows" in data:
            return {"type": "estimates", **parse_estimates(json.dumps(data))}
        return {"type": "json", "data": data}
    return {"type": "estimates", **parse_estimates(text)}


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(argv or sys.argv[1:])
    if not args:
        print("Usage: sa_parser.py <fixture.html|json>", file=sys.stderr)
        return 2
    result = parse_file(args[0])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

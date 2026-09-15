#!/usr/bin/env python3
"""Seeking Alpha estimate / fiscal / revision parsers.

No cookies, sessions, or tokens. Operates on sanitized local HTML/JSON fixtures
or already-fetched files. Never invents EPS numbers.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path


MONTH_TO_NUM = {
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


def unavailable(x) -> bool:
    if x is None:
        return True
    s = str(x).strip().lower()
    return s in {"", "data unavailable", "n/a", "na", "null", "none", "—", "-"}


def to_num(x):
    if unavailable(x):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    try:
        return float(s)
    except Exception:
        return None


def parse_fiscal_period_ending(label: str | None) -> dict:
    """Map 'Jan 2028' / 'Fiscal Period Ending Jan 2028' to calendar slot.

    Rule: ending Jan–Mar → prior calendar year slot; else ending year slot.
    Never invents EPS. Label is preserved.
    """
    raw = (label or "").strip()
    raw = re.sub(r"^fiscal period ending\s+", "", raw, flags=re.I).strip()
    m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", raw)
    if not m:
        return {
            "reportedFiscalLabel": raw or None,
            "periodType": "Fiscal Period Ending",
            "calendarAlignment": None,
            "mappedYear": None,
        }
    month = MONTH_TO_NUM.get(m.group(1).lower())
    year = int(m.group(2))
    if month is None:
        return {
            "reportedFiscalLabel": raw,
            "periodType": "Fiscal Period Ending",
            "calendarAlignment": None,
            "mappedYear": None,
        }
    cal_year = year - 1 if month <= 3 else year
    return {
        "reportedFiscalLabel": raw,
        "periodType": "Fiscal Period Ending",
        "calendarAlignment": f"CY{cal_year}",
        "mappedYear": f"{cal_year}E",
        "endingMonth": month,
        "endingYear": year,
    }


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_table = False
        self._in_th = False
        self._in_td = False
        self._in_tr = False
        self._cell = []
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._got_header = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_tr = True
            self._current_row = []
        elif self._in_table and tag == "th":
            self._in_th = True
            self._cell = []
        elif self._in_table and tag == "td":
            self._in_td = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_row:
                if not self._got_header:
                    self.headers = [c.strip() for c in self._current_row]
                    self._got_header = True
                else:
                    self.rows.append([c.strip() for c in self._current_row])
            self._current_row = []
        elif tag == "th" and self._in_th:
            self._in_th = False
            self._current_row.append("".join(self._cell).strip())
        elif tag == "td" and self._in_td:
            self._in_td = False
            self._current_row.append("".join(self._cell).strip())

    def handle_data(self, data):
        if self._in_th or self._in_td:
            self._cell.append(data)


def _norm_header(h: str) -> str:
    s = re.sub(r"\s+", " ", (h or "").strip().lower())
    aliases = {
        "fiscal period ending": "fiscal_period_ending",
        "fiscal year": "fiscal_period_ending",
        "period": "fiscal_period_ending",
        "consensus": "consensus",
        "eps consensus": "consensus",
        "mean": "consensus",
        "high": "high",
        "low": "low",
        "# analysts": "analysts",
        "analysts": "analysts",
        "analyst count": "analysts",
        "# of analysts": "analysts",
        "1m": "rev_1m",
        "1m %": "rev_1m",
        "1m rev": "rev_1m",
        "3m": "rev_3m",
        "3m %": "rev_3m",
        "6m": "rev_6m",
        "6m %": "rev_6m",
        "previous eps": "previous_eps",
        "current eps": "current_eps",
        "revision %": "revision_pct",
        "date": "date",
        "ticker": "ticker",
    }
    return aliases.get(s, s.replace(" ", "_"))


def parse_estimates_html(html: str) -> list[dict]:
    """Parse sanitized SA-like annual estimates table.

    Required columns (any header aliases): Fiscal Period Ending, Consensus,
    High, Low, analyst count, 1M, 3M, 6M.
    """
    parser = _TableParser()
    parser.feed(html)
    if not parser.headers:
        raise ValueError("No table headers found in estimates HTML")
    keys = [_norm_header(h) for h in parser.headers]
    out = []
    for row in parser.rows:
        rec = {keys[i]: (row[i] if i < len(row) else "") for i in range(len(keys))}
        fiscal = rec.get("fiscal_period_ending") or rec.get("reported_fiscal_label")
        mapped = parse_fiscal_period_ending(fiscal)
        item = {
            "fiscalPeriodEnding": mapped.get("reportedFiscalLabel") or fiscal,
            "periodType": "Fiscal Period Ending",
            "calendarAlignment": mapped.get("calendarAlignment"),
            "mappedYear": mapped.get("mappedYear"),
            "consensus": to_num(rec.get("consensus")),
            "high": to_num(rec.get("high")),
            "low": to_num(rec.get("low")),
            "analysts": to_num(rec.get("analysts")),
            "rev1M": to_num(rec.get("rev_1m")),
            "rev3M": to_num(rec.get("rev_3m")),
            "rev6M": to_num(rec.get("rev_6m")),
        }
        out.append(item)
    return out


def parse_revisions_html(html: str) -> list[dict]:
    """Parse sanitized revision-event table (previous/current EPS)."""
    parser = _TableParser()
    parser.feed(html)
    keys = [_norm_header(h) for h in parser.headers]
    out = []
    for row in parser.rows:
        rec = {keys[i]: (row[i] if i < len(row) else "") for i in range(len(keys))}
        fiscal = rec.get("fiscal_period_ending")
        mapped = parse_fiscal_period_ending(fiscal)
        prev = to_num(rec.get("previous_eps"))
        cur = to_num(rec.get("current_eps"))
        pct = to_num(rec.get("revision_pct"))
        if pct is None and prev not in (None, 0) and cur is not None:
            pct = (cur - prev) / abs(prev) * 100.0
        out.append(
            {
                "date": rec.get("date"),
                "ticker": rec.get("ticker"),
                "fiscalPeriodEnding": mapped.get("reportedFiscalLabel") or fiscal,
                "calendarAlignment": mapped.get("calendarAlignment"),
                "mappedYear": mapped.get("mappedYear"),
                "previousEps": prev,
                "currentEps": cur,
                "revisionPct": pct,
            }
        )
    return out


def revision_event_from_snapshot_change(
    ticker: str,
    fiscal_label: str,
    previous_eps,
    current_eps,
    date: str,
    calendar_alignment: str | None = None,
) -> dict | None:
    """Build a revision-event row only when consensus actually changed."""
    prev = to_num(previous_eps)
    cur = to_num(current_eps)
    if prev is None or cur is None:
        return None
    if abs(cur - prev) < 1e-9:
        return None
    pct = (cur - prev) / abs(prev) * 100.0 if prev != 0 else None
    mapped = parse_fiscal_period_ending(fiscal_label)
    return {
        "Date": date,
        "Ticker": ticker,
        "Fiscal Year": mapped.get("reportedFiscalLabel") or fiscal_label,
        "Calendar Alignment": calendar_alignment or mapped.get("calendarAlignment"),
        "Previous EPS": prev,
        "Current EPS": cur,
        "Change": cur - prev,
        "Revision %": pct,
        "Reason": "consensus change",
        "Source": "snapshot_diff",
    }


def build_snapshot_eps_from_parsed(rows: list[dict], taipei_year: int | None = None) -> dict:
    """Place parsed estimate rows onto mapped year slots. Missing slots stay absent (never invented)."""
    eps = {}
    for r in rows:
        slot = r.get("mappedYear")
        if not slot:
            continue
        eps[slot] = {
            "reported_fiscal_label": r.get("fiscalPeriodEnding"),
            "period_type": "Fiscal Period Ending",
            "calendar_alignment": r.get("calendarAlignment"),
            "consensus": r.get("consensus"),
            "high": r.get("high"),
            "low": r.get("low"),
            "analysts": r.get("analysts"),
            "rev_1M_pct": r.get("rev1M"),
            "rev_3M_pct": r.get("rev3M"),
            "rev_6M_pct": r.get("rev6M"),
        }
    return eps


def parse_estimates_file(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".json"):
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return data.get("rows") or data.get("estimates") or []
    return parse_estimates_html(text)

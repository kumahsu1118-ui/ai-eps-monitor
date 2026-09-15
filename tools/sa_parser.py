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


def first_defined(*vals):
    """Return the first value that is not None. Preserves 0.0 / 0 / False."""
    for v in vals:
        if v is not None:
            return v
    return None


# Typical fiscal-year-end month. Ticker-specific map wins over the generic
# Jan–Mar / Apr–Dec slot rule when packing annual rows for a known issuer.
FY_END_MONTH = {
    "NVDA": 1,
    "MSFT": 6,
    "AVGO": 10,
    "TSM": 12,
    "BE": 12,
    "KEYS": 10,
}


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
    cal_year = mapped_calendar_year(year, month, ticker=None)
    return {
        "reportedFiscalLabel": raw,
        "periodType": "Fiscal Period Ending",
        "calendarAlignment": f"CY{cal_year}",
        "mappedYear": f"{cal_year}E",
        "endingMonth": month,
        "endingYear": year,
    }


def parse_ending_year_month(ending) -> tuple:
    """Parse 'Jan 2027' / '2027-01-31' → (year, month)."""
    if ending is None:
        return None, None
    s = str(ending).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    mapped = parse_fiscal_period_ending(s)
    return mapped.get("endingYear"), mapped.get("endingMonth")


def mapped_calendar_year(ending_year: int, ending_month: int, ticker: str | None = None) -> int:
    """Jan–Mar ending → prior-year mapped slot; Apr–Dec → ending-year slot.

    Ticker-specific fiscal map wins: known FY-end month still uses the same
    Jan–Mar / Apr–Dec slot rule on that ending (NVDA Jan, MSFT Jun, AVGO Oct,
    TSM Dec).
    """
    month = int(ending_month)
    year = int(ending_year)
    t = (ticker or "").upper()
    if t in FY_END_MONTH:
        # Annual period for this issuer ends in FY_END_MONTH[t]. Slot mapping
        # remains Jan–Mar → prior calendar year, otherwise ending year.
        month = month  # ending month on the row is authoritative
    if month <= 3:
        return year - 1
    return year


def mapped_year_for_period_ending(ending, ticker: str | None = None) -> str | None:
    y, m = parse_ending_year_month(ending)
    if y is None or m is None:
        return None
    return f"{mapped_calendar_year(y, m, ticker=ticker)}E"


def pack_snapshot_eps_from_rows(rows: list[dict], ticker: str | None = None) -> dict:
    """Pack estimate rows onto mapped year slots. Zero revisions stay 0.0.

    Do not use ``r.get("rev1M") or r.get(...)`` — 0.0 is a valid revision.
    Jan–Mar ending → prior-year mapped slot; Apr–Dec → ending-year slot.
    Ticker-specific fiscal map wins when choosing among annual rows.
    """
    fy_end = FY_END_MONTH.get((ticker or "").upper())
    candidates: dict[str, list] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ending = first_defined(
            r.get("reportedFiscalPeriodEnding"),
            r.get("fiscalPeriodEnding"),
            r.get("period_ending"),
            r.get("reported_fiscal_label"),
        )
        y, m = parse_ending_year_month(ending)
        slot = r.get("mappedYear")
        if not slot and y is not None and m is not None:
            slot = f"{mapped_calendar_year(y, m, ticker=ticker)}E"
        if not slot:
            continue
        label = None
        if ending and not re.match(r"^\d{4}-", str(ending)):
            label = str(ending).strip()
        elif y is not None and m is not None:
            months = [
                "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
            ]
            label = f"{months[m - 1]} {y}"
        rec = {
            "reportedFiscalPeriodEnding": ending,
            "reportedFiscalLabel": label,
            "endingYear": y,
            "endingMonth": m,
            "epsMean": first_defined(to_num(r.get("epsMean")), to_num(r.get("eps_mean")), to_num(r.get("consensus"))),
            "epsHigh": first_defined(to_num(r.get("epsHigh")), to_num(r.get("eps_high")), to_num(r.get("high"))),
            "epsLow": first_defined(to_num(r.get("epsLow")), to_num(r.get("eps_low")), to_num(r.get("low"))),
            "rev1M": first_defined(to_num(r.get("rev1M")), to_num(r.get("rev_1m")), to_num(r.get("revision_1m")), to_num(r.get("rev_1M_pct"))),
            "rev3M": first_defined(to_num(r.get("rev3M")), to_num(r.get("rev_3m")), to_num(r.get("revision_3m")), to_num(r.get("rev_3M_pct"))),
            "rev6M": first_defined(to_num(r.get("rev6M")), to_num(r.get("rev_6m")), to_num(r.get("revision_6m")), to_num(r.get("rev_6M_pct"))),
            "nEst": first_defined(to_num(r.get("nEst")), to_num(r.get("n_est")), to_num(r.get("analysts"))),
            "mappedYear": slot,
        }
        candidates.setdefault(slot, []).append(rec)

    packed = {}
    for slot, recs in candidates.items():
        if fy_end is not None:
            matched = [r for r in recs if r.get("endingMonth") == fy_end]
            chosen = matched[-1] if matched else recs[-1]
        else:
            chosen = recs[-1]
        packed[slot] = chosen
    return packed


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

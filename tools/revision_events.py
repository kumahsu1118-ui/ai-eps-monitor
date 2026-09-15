#!/usr/bin/env python3
"""Generate revision events from gated/validated snapshot diffs.

MUST run before the Alert Engine so rule1_single_revision sees new events.
Never invents EPS. LKG fills and needs_verification restorations do not
create fake revision rows (consensus did not change vs last known good).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import locked_append_text
    from snapshot_quality import _eps_map, _fiscal_label, to_num
except ImportError:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import locked_append_text
    from snapshot_quality import _eps_map, _fiscal_label, to_num


EPS_UNCHANGED_EPS = 1e-9


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_alignment(year: dict, slot: str) -> str | None:
    align = year.get("calendar_alignment") or year.get("calendarAlignment")
    if align:
        return str(align)
    if isinstance(slot, str) and slot.endswith("E") and slot[:-1].isdigit():
        return f"CY{slot[:-1]}"
    return None


def _consensus_index(snap: dict | None) -> dict[tuple[str, str], dict]:
    """Index by (ticker, fiscal_label) with slot fallback."""
    out: dict[tuple[str, str], dict] = {}
    if not isinstance(snap, dict):
        return out
    tickers = snap.get("tickers") if isinstance(snap.get("tickers"), dict) else {}
    # Also accept LKG shape {tickers: {T: {eps: ...}}}
    for ticker, blob in tickers.items():
        if not isinstance(blob, dict):
            continue
        t = str(ticker).upper()
        for slot, year in _eps_map(blob).items():
            if not isinstance(year, dict):
                continue
            cons = to_num(year.get("consensus"))
            if cons is None:
                continue
            fiscal = _fiscal_label(year) or str(slot)
            out[(t, fiscal)] = {
                "ticker": t,
                "slot": str(slot),
                "fiscal": fiscal,
                "consensus": cons,
                "year": year,
                "lkgFill": bool(year.get("lkgFill") or blob.get("lkgFill")),
                "needs_verification": bool(year.get("needs_verification") or blob.get("needs_verification")),
                "collection_failed": bool(blob.get("collection_failed") or blob.get("collectionFailed")),
            }
    return out


def load_history_rows(path: Path | str) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _already_recorded(history: list[dict], event: dict) -> bool:
    key = (
        str(event.get("Date") or ""),
        str(event.get("Ticker") or ""),
        str(event.get("Fiscal Year") or ""),
        to_num(event.get("Current EPS")),
        to_num(event.get("Previous EPS")),
    )
    for row in history:
        got = (
            str(row.get("Date") or ""),
            str(row.get("Ticker") or ""),
            str(row.get("Fiscal Year") or ""),
            to_num(row.get("Current EPS")),
            to_num(row.get("Previous EPS")),
        )
        if got[0] == key[0] and got[1] == key[1] and got[2] == key[2]:
            if got[3] is not None and key[3] is not None and abs(got[3] - key[3]) < EPS_UNCHANGED_EPS:
                if got[4] is not None and key[4] is not None and abs(got[4] - key[4]) < EPS_UNCHANGED_EPS:
                    return True
                if got[4] is None and key[4] is None:
                    return True
    return False


def diff_revision_events(
    prior_snap: dict | None,
    gated_snap: dict | None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Return revision-event rows for real consensus changes on the gated snapshot."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    snap_date = ""
    if isinstance(gated_snap, dict):
        snap_date = str(gated_snap.get("snapshot_utc") or "")[:10]
    if not snap_date:
        snap_date = now.strftime("%Y-%m-%d")

    prior_idx = _consensus_index(prior_snap)
    cur_idx = _consensus_index(gated_snap)
    events: list[dict] = []
    for key, cur in cur_idx.items():
        if cur.get("lkgFill"):
            continue
        if cur.get("collection_failed"):
            continue
        year = cur.get("year") or {}
        # Restoring LKG after extreme reject is not a real revision.
        if year.get("needs_verification") and year.get("rejectedConsensus") is not None:
            kept = to_num(year.get("consensus"))
            lkg_c = to_num(year.get("rejectedConsensus"))
            # consensus kept == previous LKG; do not emit 19.38→1.938 as a revision
            if kept is not None and prior_idx.get(key) and abs(kept - prior_idx[key]["consensus"]) < EPS_UNCHANGED_EPS:
                continue
        prev = prior_idx.get(key)
        if prev is None:
            continue
        old_c = prev["consensus"]
        new_c = cur["consensus"]
        if old_c is None or new_c is None:
            continue
        if abs(new_c - old_c) < EPS_UNCHANGED_EPS:
            continue
        if old_c == 0:
            continue
        pct = (new_c - old_c) / abs(old_c) * 100.0
        direction = "upgrade" if pct > 0 else "downgrade"
        events.append(
            {
                "Date": snap_date,
                "Ticker": cur["ticker"],
                "Fiscal Year": cur["fiscal"],
                "Calendar Alignment": _slot_alignment(year, cur["slot"]),
                "Previous EPS": old_c,
                "Current EPS": new_c,
                "Change": round(new_c - old_c, 6),
                "Revision %": round(pct, 4),
                "Reason": f"consensus {direction} vs prior validated snapshot",
                "Source": (gated_snap or {}).get("source") or "Seeking Alpha",
                "Update Time": (gated_snap or {}).get("snapshot_utc") or now_iso,
            }
        )
    return events


def generate_revision_events(
    prior_snap: dict | None,
    gated_snap: dict | None,
    history_path: Path | str,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Diff gated vs prior, append new events to history.jsonl, return appended rows.

    Call this BEFORE evaluate_alerts().
    """
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_history_rows(history_path)
    events = diff_revision_events(prior_snap, gated_snap, now=now)
    appended = []
    for ev in events:
        if _already_recorded(existing, ev):
            continue
        locked_append_text(history_path, json.dumps(ev, ensure_ascii=False) + "\n")
        existing.append(ev)
        appended.append(ev)
    return appended

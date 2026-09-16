#!/usr/bin/env python3
"""Git-tracked canonical EPS daily history (durable SoT).

Canonical (Git-tracked):
  data/history/eps_daily/YYYY-MM.jsonl

Runtime cache (rebuildable, not sole durable SoT):
  data/daily_eps_snapshots/daily.jsonl

Generations keep CURRENT crash-safety; they must not be the only copy of
history, and they must not receive a full copy of every monthly file.

Observation identity (idempotent replay):
  (ticker, normalized reportedFiscalPeriodEnding, date, updateTime)
Missing updateTime → deterministic fallback ``{date}T00:00:00Z``.
mappedYear / slot / 2027E labels are display-only and are never identity.

Admission: quality-gate-passed, valid fiscal identity, finite consensus.
Never: quarantined, seed-from-revision-history, null/non-finite EPS,
invalid fiscal, staged-but-never-committed (caller writes only at COMMIT).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from atomic_io import atomic_write_text  # noqa: E402
from revision_windows import fiscal_identity_key, to_num  # noqa: E402

HISTORY_REL = Path("data") / "history" / "eps_daily"
RUNTIME_DAILY_REL = Path("data") / "daily_eps_snapshots" / "daily.jsonl"

CANONICAL_FIELDS = (
    "date",
    "ticker",
    "reportedFiscalPeriodEnding",
    "consensus",
    "analysts",
    "updateTime",
)

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")
SEED_SOURCES = frozenset({"seed_from_revision_history"})
MISSING_UPDATETIME_FALLBACK_SUFFIX = "T00:00:00Z"


class CanonicalHistoryError(ValueError):
    """Malformed canonical input that must not be invented/fixed."""


def history_dir(root: Path | None = None) -> Path:
    return Path(root or ROOT) / HISTORY_REL


def runtime_daily_path(root: Path | None = None) -> Path:
    return Path(root or ROOT) / RUNTIME_DAILY_REL


def month_key_from_date(date_str: str) -> str | None:
    m = DATE_RE.match(str(date_str or "").strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def month_file_path(root: Path | None, month_key: str) -> Path:
    return history_dir(root) / f"{month_key}.jsonl"


def missing_updatetime_fallback(date_str: str) -> str:
    """Deterministic updateTime when the source row omits it."""
    day = str(date_str or "").strip()[:10]
    if not DATE_RE.match(day):
        raise CanonicalHistoryError(f"invalid observation date for updateTime fallback: {date_str!r}")
    return f"{day}{MISSING_UPDATETIME_FALLBACK_SUFFIX}"


def normalize_update_time(row: dict | None, *, date_str: str | None = None) -> str | None:
    if not isinstance(row, dict):
        return None
    raw = row.get("updateTime") if row.get("updateTime") not in (None, "") else row.get("Update Time")
    if raw not in (None, ""):
        s = str(raw).strip()
        if s:
            return s
    day = date_str or str(row.get("date") or row.get("Date") or "").strip()[:10]
    if not DATE_RE.match(day):
        return None
    return missing_updatetime_fallback(day)


def _analysts_of(row: dict) -> Any:
    if "analysts" in row:
        val = row.get("analysts")
    elif "analystCount" in row:
        val = row.get("analystCount")
    else:
        val = None
    if val is None or val == "":
        return None
    n = to_num(val)
    if n is None:
        return None
    if abs(n - round(n)) < 1e-9:
        return int(round(n))
    return n


def observation_identity(row: dict | None) -> tuple[str, str, str, str] | None:
    """Identity tuple; None if the row cannot be admitted.

    Does not use mappedYear / slot.
    """
    if not isinstance(row, dict):
        return None
    ticker = row.get("ticker") or row.get("Ticker")
    fiscal = (
        row.get("reportedFiscalPeriodEnding")
        or row.get("reportedFiscalLabel")
        or row.get("fiscalKey")
        or row.get("Fiscal Year")
        or row.get("reported_fiscal_label")
    )
    key = fiscal_identity_key(ticker, fiscal)
    date_str = str(row.get("date") or row.get("Date") or "").strip()[:10]
    if not key or not DATE_RE.match(date_str):
        return None
    ut = normalize_update_time(row, date_str=date_str)
    if not ut:
        return None
    return (key[0], key[1], date_str, ut)


def is_admitted_source(row: dict) -> bool:
    src = str(row.get("source") or "").strip()
    if src in SEED_SOURCES:
        return False
    if row.get("collectionFailed") or row.get("usingLastKnownGood"):
        return False
    return True


def canonical_observation_from_row(row: dict | None) -> dict | None:
    """Admit a quality-gate observation into canonical schema, or None.

    Rejects: missing identity, null/non-finite consensus, bad date, seed rows.
    Does not invent or repair EPS. Strips mappedYear/slot/display fields.
    """
    if not isinstance(row, dict):
        return None
    if not is_admitted_source(row):
        return None
    ident = observation_identity(row)
    if ident is None:
        return None
    ticker, fiscal, date_str, update_time = ident
    cons = to_num(row.get("consensus") if "consensus" in row else row.get("eps"))
    if cons is None:
        return None
    return {
        "date": date_str,
        "ticker": ticker,
        "reportedFiscalPeriodEnding": fiscal,
        "consensus": cons,
        "analysts": _analysts_of(row),
        "updateTime": update_time,
    }


def admitted_observations(rows: Iterable[dict] | None) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        obs = canonical_observation_from_row(row)
        if obs is not None:
            out.append(obs)
    return out


def dumps_canonical_row(obs: dict) -> str:
    payload = {k: obs.get(k) for k in CANONICAL_FIELDS}
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def parse_canonical_line(line: str, *, strict: bool = False) -> dict | None:
    """Parse one JSONL line. Malformed → None (or raise if strict). Never invents EPS."""
    s = line.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except Exception as exc:
        if strict:
            raise CanonicalHistoryError(f"malformed JSONL: {exc}") from exc
        return None
    if not isinstance(obj, dict):
        if strict:
            raise CanonicalHistoryError("canonical row is not an object")
        return None
    obs = canonical_observation_from_row(obj)
    if obs is None:
        if strict:
            raise CanonicalHistoryError(f"rejected canonical row: {s[:200]}")
        return None
    return obs


def load_jsonl_rows(path: Path, *, strict: bool = False) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        obs = parse_canonical_line(line, strict=strict)
        if obs is not None:
            rows.append(obs)
    return rows


def identity_key(obs: dict) -> tuple[str, str, str, str]:
    ident = observation_identity(obs)
    if ident is None:
        raise CanonicalHistoryError("canonical row missing identity")
    return ident


def sort_canonical_rows(rows: list[dict]) -> list[dict]:
    """Stable deterministic order: date, ticker, fiscal, updateTime, then original index."""
    decorated = []
    for i, row in enumerate(rows):
        ident = observation_identity(row) or ("", "", "", "")
        decorated.append((ident[2], ident[0], ident[1], ident[3], i, row))
    decorated.sort()
    return [d[-1] for d in decorated]


def merge_observations(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    """Append new identities only. Identical replay = no-op. Stable order."""
    seen = {identity_key(r) for r in existing}
    merged = list(existing)
    n_new = 0
    for row in incoming:
        obs = canonical_observation_from_row(row) or row
        try:
            ident = identity_key(obs)
        except CanonicalHistoryError:
            continue
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(obs)
        n_new += 1
    return sort_canonical_rows(merged), n_new


def format_jsonl(rows: list[dict]) -> str:
    if not rows:
        return ""
    return "".join(dumps_canonical_row(r) + "\n" for r in rows)


def list_month_files(root: Path | None = None) -> list[Path]:
    d = history_dir(root)
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and MONTH_FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def load_all_canonical_rows(root: Path | None = None, *, strict: bool = False) -> list[dict]:
    rows: list[dict] = []
    for path in list_month_files(root):
        rows.extend(load_jsonl_rows(path, strict=strict))
    return sort_canonical_rows(rows)


def build_generation_month_payloads(
    root: Path | None,
    incoming_rows: Iterable[dict] | None,
) -> dict[str, str]:
    """Build updated monthly JSONL text for months that gained new observations.

    Reads live canonical files; does not write them. Empty dict = exact replay no-op.
    Does not copy untouched months (generations must not hold full history).
    """
    admitted = admitted_observations(list(incoming_rows or []))
    if not admitted:
        return {}
    by_month: dict[str, list[dict]] = {}
    for obs in admitted:
        mk = month_key_from_date(obs["date"])
        if not mk:
            continue
        by_month.setdefault(mk, []).append(obs)
    payloads: dict[str, str] = {}
    for mk, new_rows in by_month.items():
        path = month_file_path(root, mk)
        existing = load_jsonl_rows(path)
        merged, n_new = merge_observations(existing, new_rows)
        if n_new == 0:
            continue
        payloads[mk] = format_jsonl(merged)
    return payloads


def write_month_payloads(dest_dir: Path, payloads: dict[str, str]) -> list[Path]:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for mk in sorted(payloads):
        path = dest_dir / f"{mk}.jsonl"
        atomic_write_text(path, payloads[mk])
        written.append(path)
    return written


def materialize_runtime_daily(
    root: Path | None = None,
    *,
    strict: bool = False,
) -> Path:
    """Deterministic materializer: canonical monthly JSONL → runtime daily.jsonl.

    Atomic write. Malformed rows are rejected (skipped unless strict).
    Does not invent or repair EPS. Ordering is stable.
    """
    root = Path(root or ROOT)
    rows = load_all_canonical_rows(root, strict=strict)
    text = format_jsonl(rows)
    dest = runtime_daily_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, text)
    return dest


def apply_payloads_to_live(root: Path | None, payloads: dict[str, str]) -> list[Path]:
    """Atomically replace live monthly files listed in payloads. Untouched months stay."""
    return write_month_payloads(history_dir(root), payloads)


def pending_rows_from_stage(stage_dir: Path) -> list[dict]:
    path = Path(stage_dir) / "pending_daily_rows.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Canonical EPS daily history materializer")
    ap.add_argument("--root", type=Path, default=None, help="Project root (default: repo root)")
    ap.add_argument(
        "--materialize",
        action="store_true",
        help="Rebuild data/daily_eps_snapshots/daily.jsonl from data/history/eps_daily/*.jsonl",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Fail closed on malformed canonical rows (default: skip malformed)",
    )
    args = ap.parse_args(argv)
    root = args.root or ROOT
    if args.materialize:
        dest = materialize_runtime_daily(root, strict=args.strict)
        n = 0
        if dest.exists() and dest.stat().st_size:
            n = sum(1 for line in dest.read_text(encoding="utf-8").splitlines() if line.strip())
        print(f"materialized {dest} rows={n}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

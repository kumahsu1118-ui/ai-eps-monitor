#!/usr/bin/env python3
"""Deterministic alert engine for ai-eps-monitor.

Rules (documented, no ML / no invented thresholds beyond these):
  1. Single consensus EPS revision |revisionPct| > 2% (from revision events;
     baselines / n/a skipped). Computes pct from previous→current when missing.
     2. Cumulative INTERNAL 30D revision from daily.jsonl (same ticker +
     reportedFiscalPeriodEnding) via shared tools/revision_windows.py:
     latest-observation anchor, schedule-aware weekday-gap baseline (Friday→Monday
     valid; ancient obs cannot fake 30D). Stateful hysteresis: |pct|>=5% Open;
     stays >=5% Update same alert ID; |pct|<4% Resolve. History only Open /
     material Update / Resolved (no daily spam).
     If <2 usable daily points in window → alertDiagnostics only (NOT alertHistory).
     NEVER reconstruct Internal 30D from revision-event history when daily.jsonl is empty.
     Missing/unavailable daily history must not mint a new Internal 30D and must not
     resolve a prior open (data loss ≠ abs(pct)<4%). True Resolved requires a valid
     computed Internal 30D with |pct|<4%.
  2b. Source-reported SA 1M |rev1M|>=5% → source_reported_1m_revision
     labeled "Source window: Seeking Alpha 1M" (NEVER Internal 30D).
  3. Results vs consensus (resultsVsConsensus) and guidance vs consensus
     (guidanceVsConsensus) are separate. Guidance alert ONLY when
     guidanceDetail.vsConsensus in {above, below}; unknown → no guidance alert.
  4. gross_margin_pressure (result/pressure) vs gross_margin_guidance_revision
     (requires previousGuidance + currentGuidance). Legacy GM >200bps pressure
     when parsable.
  5. Driver status → improving/deteriorating with reason. Multiple transitions
     for the same driver (different eventDate) ALL remain in alertHistory.
     Dedupe ONLY by full unique Alert ID — never collapse by (ticker, driver).

Alert ID = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey
(stable, unique — never collapse multiple drivers onto :na).

Lifecycle:
  - One-shot events expire from activeAlerts after ONE_SHOT_ACTIVE_DAYS (21).
  - Cumulative alerts are non-oneshot (stateful) while Open/Updated.
  - alertHistory retains material evaluated alerts (long retention).
  - insufficient-history records go to alertDiagnostics only (no history/daily pollution).
  - activeAlerts = history items still within active window (or stateful open).
  - homepageAttentionQueue: ranked Important Alerts (max 2/ticker, max 5).

Writes data/alerts/index.json with activeAlerts + alertHistory (+ legacy alerts alias).
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if not (ROOT / "data" / "snapshots").exists():
    cand = Path(__file__).resolve().parent
    for _ in range(5):
        if (cand / "data" / "snapshots").exists():
            ROOT = cand
            break
        cand = cand.parent

if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from revision_windows import (  # noqa: E402
    compute_internal_window,
    evaluate_internal_window,
    fiscal_identity_key,
    group_daily_by_fiscal_identity,
)

REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
EARNINGS_DIR = ROOT / "data" / "earnings"
DRIVERS_DIR = ROOT / "data" / "drivers"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
UNIVERSE_PATH = ROOT / "data" / "universe.json"
DAILY_JSONL = ROOT / "data" / "daily_eps_snapshots" / "daily.jsonl"

TICKERS_DEFAULT = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]

# One-shot homepage window (within 14–30 days). Documented choice: 21 days.
ONE_SHOT_ACTIVE_DAYS = 21
CUMULATIVE_LOOKBACK_DAYS = 30
CUMULATIVE_OPEN_PCT = 5.0
CUMULATIVE_RESOLVE_PCT = 4.0  # hysteresis: resolve only below 4%
CUMULATIVE_MATERIAL_PCT_DELTA = 1.0  # material update threshold (pp)
SOURCE_1M_ALERT_PCT = 5.0
SOURCE_1M_RESOLVE_PCT = 4.0  # hysteresis: resolve when |1M| < 4%
SOURCE_1M_MATERIAL_PCT_DELTA = 1.0


def consensus_confidence(analyst_count) -> str:
    """Deterministic coverage confidence label."""
    n = to_num(analyst_count)
    if n is None:
        return "Unknown"
    n = int(n)
    if n >= 10:
        return "High"
    if n >= 5:
        return "Medium"
    if n >= 2:
        return "Low"
    if n == 1:
        return "Single estimate"
    return "Unknown"


def severity_for_source_1m(rev_pct, analyst_count, *, far_forward: bool = False) -> str:
    """Cap severity when coverage is thin or analystCount unknown.

    - analystCount missing/unknown → never High (cap at Medium)
    - analystCount <5 on far-forward → not High
    """
    n = to_num(analyst_count)
    conf = consensus_confidence(n)
    abs_rev = abs(to_num(rev_pct) or 0)
    if n is None:
        # Unknown coverage must not map to High severity
        return "medium" if abs_rev >= SOURCE_1M_ALERT_PCT else "low"
    if n < 5 and far_forward:
        return "medium" if abs_rev >= 10 else "low"
    if conf in {"Low", "Single estimate", "Unknown"}:
        return "medium" if abs_rev >= SOURCE_1M_ALERT_PCT else "low"
    return "high"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_num(x):
    """Parse number; reject non-finite (NaN/Infinity) — math.isfinite only."""
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "n/a (baseline)", "data unavailable", "null", "none", "nan", "inf", "-inf", "+inf", "infinity", "-infinity"}:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def load_tickers() -> list[str]:
    if UNIVERSE_PATH.exists():
        u = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        return list(u.get("tickers") or TICKERS_DEFAULT)
    return list(TICKERS_DEFAULT)


def load_history() -> list[dict]:
    if not REV_PATH.exists():
        return []
    rows = []
    for line in REV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            if fmt.endswith("%z") and s.endswith("Z"):
                s2 = s[:-1] + "+0000"
                return datetime.strptime(s2[:19] + "+0000", "%Y-%m-%dT%H:%M:%S%z")
            return datetime.strptime(
                s[: len("2026-09-15T01:36:00") if "T" in fmt else 10],
                fmt.replace("%z", ""),
            ).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_baseline(row: dict) -> bool:
    reason = str(row.get("Reason") or "").lower()
    prev = str(row.get("Previous EPS") or "").lower()
    rev = str(row.get("Revision %") or "").lower()
    if "baseline" in reason or "baseline" in prev:
        return True
    if rev in {"n/a", "n/a (baseline)", "na", ""}:
        if to_num(row.get("Previous EPS")) is None:
            return True
    return False


def revision_pct_of(row: dict) -> float | None:
    rp = to_num(row.get("Revision %"))
    if rp is not None:
        return rp
    prev = to_num(row.get("Previous EPS"))
    cur = to_num(row.get("Current EPS"))
    if prev is None or cur is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def _sanitize_id_part(x) -> str:
    if x is None:
        return "na"
    s = str(x).strip()
    if not s:
        return "na"
    s = re.sub(r"\s+", "_", s)
    s = s.replace(":", "_").replace("/", "-")
    return s[:80] or "na"


def make_alert_id(
    rule_id: str,
    ticker: str,
    period: str | None = None,
    event_date: str | None = None,
    event_key: str | None = None,
) -> str:
    """Stable unique id: rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey."""
    return ":".join(
        [
            _sanitize_id_part(rule_id),
            _sanitize_id_part(ticker),
            _sanitize_id_part(period),
            _sanitize_id_part(event_date),
            _sanitize_id_part(event_key),
        ]
    )


def alert(
    rule_id: str,
    severity: str,
    ticker: str,
    message: str,
    *,
    period: str | None = None,
    event_date: str | None = None,
    event_key: str | None = None,
    event_at: str | None = None,
    oneshot: bool = True,
    **extra,
) -> dict:
    """Build alert with unique id and lifecycle fields."""
    # Prefer explicit fiscal/period from extra
    fiscal = period or extra.get("fiscal") or extra.get("slot") or extra.get("eventPeriod")
    date_part = event_date or extra.get("date") or (event_at or "")[:10] or None
    key_part = event_key or extra.get("driver") or extra.get("driverName") or extra.get("eventKey")
    aid = make_alert_id(rule_id, ticker, fiscal, date_part, key_part)

    created = now_utc_iso()
    # NEVER substitute today as a business eventAt when the caller marked missingEventDate
    # or explicitly passed event_at=None with no date_part.
    if extra.get("missingEventDate") and not date_part and not event_at:
        ev_at = None
    else:
        ev_at = event_at or (f"{date_part}T00:00:00Z" if date_part else created)
    # Lifecycle clock may use created when business eventAt is missing
    ev_dt = parse_date(ev_at) or parse_date(created) or datetime.now(timezone.utc)
    expires = None
    if oneshot:
        expires = (ev_dt + timedelta(days=ONE_SHOT_ACTIVE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = {
        "id": aid,
        "rule": rule_id,
        "severity": severity,
        "ticker": ticker,
        "message": message,
        "title": message,
        "fiscalPeriod": fiscal,
        "eventPeriod": fiscal,
        "eventDate": date_part,
        "eventKey": key_part,
        "eventAt": ev_at,
        "createdAt": created,
        "expiresAt": expires,
        "activeUntil": expires,
        "oneshot": oneshot,
    }
    for k, v in extra.items():
        if v is not None and k not in out:
            out[k] = v
    return out


def age_days(alert_obj: dict, now: datetime | None = None) -> int:
    """Homepage age uses lastMaterialChangeAt (fallback openedAt), never snapshot Hold refresh."""
    now = now or datetime.now(timezone.utc)
    ev = parse_date(
        alert_obj.get("lastMaterialChangeAt")
        or alert_obj.get("lastMaterialUpdateAt")
        or alert_obj.get("openedAt")
        or alert_obj.get("eventAt")
        or alert_obj.get("eventDate")
        or alert_obj.get("createdAt")
    )
    if ev is None:
        return 0
    return max(0, int((now - ev).total_seconds() // 86400))


def is_active(alert_obj: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    st = str(alert_obj.get("status") or alert_obj.get("lifecycleStatus") or "").lower()
    if st == "resolved":
        return False
    if alert_obj.get("lifecycleEvent") == "Resolved":
        return False
    until = parse_date(alert_obj.get("expiresAt") or alert_obj.get("activeUntil"))
    if until is not None:
        return now <= until
    # Non-expiring stateful (cumulative Open/Updated): keep active
    if alert_obj.get("oneshot") is False:
        return True
    # Legacy alerts without eventAt/createdAt cannot prove freshness → not active
    if not (alert_obj.get("eventAt") or alert_obj.get("eventDate") or alert_obj.get("createdAt")):
        return False
    # Fallback: age-based
    return age_days(alert_obj, now) <= ONE_SHOT_ACTIVE_DAYS


def rule1_single_revision(history: list[dict]) -> list[dict]:
    """|revisionPct| > 2% on a single non-baseline event."""
    out = []
    for row in history:
        if is_baseline(row):
            continue
        ticker = row.get("Ticker")
        if not ticker:
            continue
        pct = revision_pct_of(row)
        if pct is None:
            continue
        if abs(pct) > 2.0:
            direction = "upgrade" if pct > 0 else "downgrade"
            fiscal = row.get("Fiscal Year") or row.get("Calendar Alignment")
            date = row.get("Date") or (row.get("Update Time") or "")[:10]
            out.append(
                alert(
                    "single_revision_gt_2pct",
                    "high",
                    ticker,
                    f"{ticker} {fiscal}: single consensus EPS {direction} {pct:+.2f}% (>2%)",
                    period=fiscal,
                    event_date=date,
                    event_key=f"rev_{pct:+.2f}",
                    event_at=row.get("Update Time") or (f"{date}T00:00:00Z" if date else None),
                    revisionPct=pct,
                    date=date,
                    fiscal=fiscal,
                )
            )
    return out



def load_daily_jsonl() -> list[dict]:
    """Load append-only daily consensus observations."""
    if not DAILY_JSONL.exists():
        return []
    rows = []
    for line in DAILY_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def rule2_cumulative(
    history: list[dict] | None = None,
    lookback_days: int = CUMULATIVE_LOOKBACK_DAYS,
    daily_rows: list[dict] | None = None,
    now: datetime | None = None,
    prior_history: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Cumulative first→last revision in TRUE lookback window — stateful.

    Primary source: data/daily_eps_snapshots/daily.jsonl grouped by
    (ticker, reportedFiscalPeriodEnding) via shared revision_windows.py.
    Latest-observation anchor; schedule-aware weekday-gap baseline (not 2-day
    calendar slack). Friday→Monday is valid; ancient obs cannot fake 30D.
    NEVER substitute revision-event history when daily.jsonl has no groups —
    Internal 30D is fail-closed without daily observations (exporter has no
    equivalent fallback). Missing/unavailable daily history also must not
    resolve a prior open: empty groups or an unevaluable window is not
    evidence that |pct| fell below 4%. Preserve the prior open (diagnostics
    only) until a valid window can be computed.

    Stateful hysteresis (Internal 30D — never SA 1M):
      |pct| >= 5% → Open (or Update same active alert ID while stays >=5%)
      |pct| < 4%  → Resolve active alert (valid computed window only)
      4% <= |pct| < 5% → hold prior Open/Updated (hysteresis band)
      window unavailable / daily missing → no new Open, no Resolved

    History only records Open / material Update / Resolved — no daily spam.
    Stable alert ID per open episode (no daily minting).

    Returns (alerts_for_merge, diagnostics). Diagnostics carry insufficient-history
    and must NOT enter alertHistory / activeAlerts / daily pollution.
    NEVER fall back to all-history while claiming a 30D window.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    rows = daily_rows if daily_rows is not None else load_daily_jsonl()
    groups = group_daily_by_fiscal_identity(rows)
    # history (revision events) is intentionally unused for Internal 30D.
    # Exporter has no revision-event fallback; labeling a history-derived
    # move "Internal 30D" would disagree with the UI on a lost daily.jsonl.
    _ = history

    # Index prior open cumulative alerts by normalized (ticker, fiscal)
    prior_open: dict[tuple, dict] = {}
    for a in prior_history or []:
        if not isinstance(a, dict):
            continue
        if a.get("rule") != "cumulative_revision_gt_5pct":
            continue
        st = str(a.get("status") or a.get("lifecycleStatus") or "").lower()
        if st == "resolved":
            continue
        ident = fiscal_identity_key(
            a.get("ticker"),
            a.get("fiscalPeriod") or a.get("period") or a.get("fiscal"),
        )
        if not ident:
            continue
        key = ident
        # Prefer most recently updated / opened
        prev = prior_open.get(key)
        if prev is None:
            prior_open[key] = a
        else:
            def _ts(x):
                return parse_date(x.get("updatedAt") or x.get("eventAt") or x.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)
            if _ts(a) >= _ts(prev):
                prior_open[key] = a

    out: list[dict] = []
    diagnostics: list[dict] = []

    def _stable_cum_id(ticker: str, fiscal: str, open_date: str) -> str:
        return make_alert_id(
            "cumulative_revision_gt_5pct",
            ticker,
            fiscal,
            open_date,
            "internal_30d",
        )

    def _material(old_pct, new_pct, old_dir, new_dir) -> bool:
        if old_pct is None:
            return True
        if old_dir and new_dir and old_dir != new_dir:
            return True
        try:
            return abs(float(new_pct) - float(old_pct)) >= CUMULATIVE_MATERIAL_PCT_DELTA
        except Exception:
            return True

    def _insufficient(ticker: str, fiscal: str, pts: list, message: str) -> None:
        prior = prior_open.get((ticker, fiscal))
        diagnostics.append(
            {
                "rule": "cumulative_revision_insufficient_history",
                "severity": "info",
                "ticker": ticker,
                "period": fiscal,
                "message": (
                    message
                    if prior is None
                    else f"{message}; prior open Internal 30D preserved (window unavailable is not a resolve)"
                ),
                "lookbackDays": lookback_days,
                "windowPointCount": len(pts),
                "status": "insufficient history",
                "eventAt": now_utc_iso(),
                "diagnostic": True,
                "priorAlertId": (prior or {}).get("id"),
            }
        )
        # Fail closed: an unevaluable/missing window is not abs(pct)<4%.
        # Do not mint a new Internal 30D and do not resolve a prior open.

    evaluated_keys = set()
    for (ticker, fiscal), pts in groups.items():
        evaluated_keys.add((ticker, fiscal))
        ev = evaluate_internal_window(pts, as_of=now, window_days=lookback_days)
        w = ev["window"]
        latest = ev["latest"]
        start_pt = ev["start"]
        if w.get("status") != "ok" or latest is None or start_pt is None:
            _insufficient(
                ticker,
                fiscal,
                pts,
                (
                    f"{ticker} {fiscal}: insufficient history for {lookback_days}D cumulative "
                    f"({w.get('reason') or 'no valid observation at/before window start'})"
                ),
            )
            continue

        first = w.get("startEps")
        last = w.get("endEps")
        cum_pct = w.get("revisionPct")
        if first is None or last is None or cum_pct is None:
            _insufficient(
                ticker,
                fiscal,
                pts,
                f"{ticker} {fiscal}: insufficient history for {lookback_days}D cumulative (unusable EPS)",
            )
            continue
        abs_pct = abs(cum_pct)
        direction = "upgrade" if cum_pct > 0 else "downgrade"
        last_date = w.get("endDate") or latest[0].strftime("%Y-%m-%d")
        start_date = w.get("startDate") or start_pt[0].strftime("%Y-%m-%d")
        latest_iso = latest[0].strftime("%Y-%m-%dT%H:%M:%SZ")
        prior = prior_open.get((ticker, fiscal))

        if abs_pct >= CUMULATIVE_OPEN_PCT:
            if prior is None:
                # Open new episode — stable ID uses open date (not daily mint)
                open_date = last_date
                a = alert(
                    "cumulative_revision_gt_5pct",
                    "high",
                    ticker,
                    f"{ticker} {fiscal}: Internal 30D cumulative {direction} {cum_pct:+.2f}% (≥5%) [Open]",
                    period=fiscal,
                    event_date=open_date,
                    event_key="internal_30d",
                    event_at=latest_iso,
                    cumulativePct=cum_pct,
                    lookbackDays=lookback_days,
                    fiscal=fiscal,
                    startEps=first,
                    endEps=last,
                    startDate=start_date,
                    oneshot=False,
                    status="Open",
                    lifecycleStatus="Open",
                    lifecycleEvent="Open",
                    windowLabel="Internal 30D",
                    openedAt=latest_iso,
                    updatedAt=latest_iso,
                    direction=direction,
                )
                a["id"] = _stable_cum_id(ticker, fiscal, open_date)
                a["expiresAt"] = None
                a["activeUntil"] = None
                out.append(a)
            else:
                # Update same active alert ID (no new daily ID)
                a = dict(prior)
                old_pct = to_num(prior.get("cumulativePct"))
                old_dir = prior.get("direction")
                material = _material(old_pct, cum_pct, old_dir, direction)
                a["cumulativePct"] = cum_pct
                a["endEps"] = last
                a["startEps"] = first
                a["startDate"] = start_date
                a["direction"] = direction
                a["lookbackDays"] = lookback_days
                a["oneshot"] = False
                a["expiresAt"] = None
                a["activeUntil"] = None
                a["windowLabel"] = "Internal 30D"
                a["updatedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                a["status"] = "Updated" if material else (prior.get("status") or "Open")
                a["lifecycleStatus"] = a["status"]
                if material:
                    a["lifecycleEvent"] = "Updated"
                    a["message"] = (
                        f"{ticker} {fiscal}: Internal 30D cumulative {direction} {cum_pct:+.2f}% (≥5%) [Updated]"
                    )
                    a["title"] = a["message"]
                    a["lastMaterialUpdateAt"] = a["updatedAt"]
                    a["lastMaterialPct"] = cum_pct
                else:
                    # Non-material: keep prior message / lifecycleEvent; still return for active merge
                    a["lifecycleEvent"] = "Hold"
                    a["_skipHistoryAppend"] = True
                # Preserve stable id / createdAt / openedAt
                a["id"] = prior.get("id") or _stable_cum_id(
                    ticker, fiscal, (prior.get("eventDate") or last_date)
                )
                out.append(a)
        elif abs_pct < CUMULATIVE_RESOLVE_PCT:
            if prior is not None:
                a = dict(prior)
                a["status"] = "Resolved"
                a["lifecycleStatus"] = "Resolved"
                a["lifecycleEvent"] = "Resolved"
                a["resolvedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                a["cumulativePct"] = cum_pct
                a["direction"] = direction
                a["oneshot"] = False
                a["expiresAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                a["activeUntil"] = a["expiresAt"]
                a["windowLabel"] = "Internal 30D"
                a["message"] = (
                    f"{ticker} {fiscal}: Internal 30D cumulative resolved "
                    f"({cum_pct:+.2f}% < {CUMULATIVE_RESOLVE_PCT}%)"
                )
                a["title"] = a["message"]
                out.append(a)
            # else: never opened — silence (no spam)
        else:
            # Hysteresis band 4–5%: hold prior open without new history spam
            if prior is not None:
                a = dict(prior)
                a["cumulativePct"] = cum_pct
                a["direction"] = direction
                a["updatedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                a["oneshot"] = False
                a["expiresAt"] = None
                a["activeUntil"] = None
                a["lifecycleEvent"] = "Hold"
                a["_skipHistoryAppend"] = True
                a["windowLabel"] = "Internal 30D"
                out.append(a)

    # Keys not in groups (empty/missing daily.jsonl, or this fiscal identity
    # absent this run) are NOT a true resolve. Empty daily is data loss, not
    # abs(pct)<4%, and is not independently proven fiscal-identity removal.
    # Preserve prior opens until a valid Internal 30D window can be evaluated.
    for key, prior in prior_open.items():
        if key in evaluated_keys:
            continue
        reason = (
            "daily history unavailable"
            if not groups
            else "fiscal identity not in daily history"
        )
        diagnostics.append(
            {
                "rule": "cumulative_revision_insufficient_history",
                "severity": "info",
                "ticker": key[0],
                "period": key[1],
                "message": (
                    f"{key[0]} {key[1]}: Internal 30D cannot be evaluated "
                    f"({reason}); prior open preserved"
                ),
                "lookbackDays": lookback_days,
                "windowPointCount": 0,
                "status": "insufficient history",
                "eventAt": now_utc_iso(),
                "diagnostic": True,
                "priorAlertId": prior.get("id"),
            }
        )

    return out, diagnostics



def _norm_vs(val) -> str | None:
    if not isinstance(val, str):
        return None
    c = val.strip().lower()
    if c in {"above", "below"}:
        return c
    return None



def _hostname_of(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        host = urlparse(str(url)).hostname
    except Exception:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def _is_seekingalpha_hostname(host: str | None) -> bool:
    """Strict: host == seekingalpha.com or endswith .seekingalpha.com (not evilseekingalpha.com)."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "seekingalpha.com" or h.endswith(".seekingalpha.com")


def _url_is_seekingalpha(url: str | None) -> bool:
    return _is_seekingalpha_hostname(_hostname_of(url))


def _consensus_comparison_source(dig: dict) -> tuple[str | None, int | None]:
    """Field-level provenance for results-vs-consensus.

    Prefer consensusComparison.sourceUrl/sourceTier (e.g. SA Tier 4),
    NOT actuals / results IR release URLs.
    """
    cc = dig.get("consensusComparison")
    if isinstance(cc, dict):
        url = cc.get("sourceUrl") or cc.get("url")
        tier = cc.get("sourceTier") if cc.get("sourceTier") is not None else cc.get("tier")
        if url or tier is not None:
            if url and not _url_is_seekingalpha(url):
                host = _hostname_of(url)
                if host and "seekingalpha" in host:
                    url = None
                    if tier == 4:
                        tier = None
            return url, tier
    # Heuristic: Seeking Alpha sources in digest.sources list
    for s in dig.get("sources") or []:
        if not isinstance(s, dict):
            continue
        tier = s.get("sourceTier") if s.get("sourceTier") is not None else s.get("tier")
        attr = str(s.get("attribution") or s.get("title") or "").lower()
        url = s.get("url") or s.get("sourceUrl")
        if _url_is_seekingalpha(url):
            return url, tier if tier is not None else 4
        if (tier == 4 or "seeking alpha" in attr) and not url:
            return None, 4
    # results.notes often cite SA beat/miss — still do not use results.sourceUrl (IR)
    notes = str(((dig.get("results") or {}) if isinstance(dig.get("results"), dict) else {}).get("notes") or "")
    if "seeking alpha" in notes.lower() or "sa earnings" in notes.lower():
        # Find any validated SA URL in sources; else leave url None but tier 4
        for s in dig.get("sources") or []:
            if isinstance(s, dict) and _url_is_seekingalpha(s.get("url") or s.get("sourceUrl")):
                return s.get("url") or s.get("sourceUrl"), 4
        return None, 4
    return None, None


def rule3_results_and_guidance(tickers: list[str]) -> list[dict]:
    """Split resultsVsConsensus vs guidanceVsConsensus.

    Guidance alert ONLY if guidanceDetail.vsConsensus in {above, below}.
    results_vs_consensus alert carries consensusComparison provenance (SA Tier 4),
    not company IR actuals source.
    """
    out = []
    for t in tickers:
        dig = load_json(EARNINGS_DIR / f"{t}.json")
        if not dig or not dig.get("hasDigest"):
            continue
        period = dig.get("periodLabel") or "na"
        report_date = dig.get("reportDate") or (dig.get("updatedAt") or "")[:10]

        results_vs = _norm_vs(
            (dig.get("results") or {}).get("vsConsensus")
            or dig.get("resultsVsConsensus")
            or ((dig.get("consensusComparison") or {}) if isinstance(dig.get("consensusComparison"), dict) else {}).get("vsConsensus")
        )
        # Legacy dig.comparison often meant results; keep as results fallback only
        if results_vs is None:
            results_vs = _norm_vs(dig.get("comparison"))

        if results_vs in {"above", "below"}:
            label = "Above" if results_vs == "above" else "Below"
            src_url, src_tier = _consensus_comparison_source(dig)
            kwargs = dict(
                comparison=label,
                resultsVsConsensus=label,
            )
            if src_url:
                kwargs["sourceUrl"] = src_url
            if src_tier is not None:
                kwargs["sourceTier"] = src_tier
            # Explicitly avoid leaking actuals/IR URL onto this alert
            actuals = dig.get("actuals") if isinstance(dig.get("actuals"), dict) else None
            results = dig.get("results") if isinstance(dig.get("results"), dict) else None
            if actuals:
                kwargs["actualsSourceUrl"] = actuals.get("sourceUrl")
                if actuals.get("sourceTier") is not None:
                    kwargs["actualsSourceTier"] = actuals.get("sourceTier")
            elif results and results.get("sourceUrl"):
                kwargs["actualsSourceUrl"] = results.get("sourceUrl")
                if results.get("sourceTier") is not None:
                    kwargs["actualsSourceTier"] = results.get("sourceTier")
            out.append(
                alert(
                    "results_vs_consensus",
                    "medium",
                    t,
                    f"{t}: results vs consensus marked {label}",
                    period=period,
                    event_date=report_date,
                    event_key=f"results_{results_vs}",
                    event_at=f"{report_date}T00:00:00Z" if report_date else None,
                    **kwargs,
                )
            )

        gd = dig.get("guidanceDetail") or {}
        guidance_vs = _norm_vs(gd.get("vsConsensus") if isinstance(gd, dict) else None)
        guidance_vs = guidance_vs or _norm_vs(dig.get("guidanceVsConsensus"))
        if guidance_vs in {"above", "below"}:
            label = "Above" if guidance_vs == "above" else "Below"
            out.append(
                alert(
                    "guidance_vs_consensus",
                    "medium",
                    t,
                    f"{t}: guidance vs consensus marked {label}",
                    period=period,
                    event_date=report_date,
                    event_key=f"guidance_{guidance_vs}",
                    event_at=f"{report_date}T00:00:00Z" if report_date else None,
                    comparison=label,
                    guidanceVsConsensus=label,
                )
            )
        # unknown / missing → no guidance alert
    return out


def _extract_gm_numbers(text: str) -> list[float]:
    if not text:
        return []
    nums = []
    for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%", text):
        v = float(m.group(1))
        if 20 <= v <= 95:
            nums.append(v)
    return nums


def _extract_bps_change(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(down|decline[d]?|fell|lower(?:ed)?)\s+(?:by\s+)?(\d{2,4})\s*bps", text, re.I)
    if m:
        return -float(m.group(2))
    m = re.search(r"(up|increase[d]?|rose|higher)\s+(?:by\s+)?(\d{2,4})\s*bps", text, re.I)
    if m:
        return float(m.group(2))
    m = re.search(r"([+-]?\d{2,4})\s*bps", text, re.I)
    if m:
        return float(m.group(1))
    return None


def rule4_gm(tickers: list[str]) -> list[dict]:
    """gross_margin_pressure vs gross_margin_guidance_revision.

    Guidance revision requires previousGuidance + currentGuidance.
    Pressure uses parsable bps / reported-vs-guide deltas > 200 bps.
    """
    out = []
    for t in tickers:
        dig = load_json(EARNINGS_DIR / f"{t}.json")
        if not dig or not dig.get("hasDigest"):
            continue
        period = dig.get("periodLabel") or "na"
        report_date = dig.get("reportDate") or (dig.get("updatedAt") or "")[:10]
        gd = dig.get("guidanceDetail") if isinstance(dig.get("guidanceDetail"), dict) else {}

        prev_g = gd.get("previousGuidance") or dig.get("previousGuidance")
        cur_g = gd.get("currentGuidance") or gd.get("grossMargin") or dig.get("currentGuidance")
        if prev_g and cur_g and str(prev_g).strip() != str(cur_g).strip():
            # Explicit guidance revision path
            prev_nums = _extract_gm_numbers(str(prev_g))
            cur_nums = _extract_gm_numbers(str(cur_g))
            bps = None
            if prev_nums and cur_nums:
                bps = (cur_nums[0] - prev_nums[0]) * 100.0
            out.append(
                alert(
                    "gross_margin_guidance_revision",
                    "high" if bps is not None and abs(bps) > 200 else "medium",
                    t,
                    f"{t}: GM guidance revision (previous → current)",
                    period=period,
                    event_date=report_date,
                    event_key="gm_guidance_rev",
                    event_at=f"{report_date}T00:00:00Z" if report_date else None,
                    previousGuidance=prev_g,
                    currentGuidance=cur_g,
                    bpsChange=bps,
                )
            )

        blob_parts = [
            dig.get("guidance") or "",
            json.dumps(gd or {}),
            " ".join(
                (n.get("text") if isinstance(n, dict) else str(n))
                for n in (dig.get("negatives") or [])
            ),
            " ".join(
                (p.get("text") if isinstance(p, dict) else str(p))
                for p in (dig.get("positives") or [])
            ),
        ]
        text = " ".join(str(x) for x in blob_parts)
        bps = _extract_bps_change(text)
        if bps is not None and abs(bps) > 200:
            out.append(
                alert(
                    "gross_margin_pressure",
                    "high",
                    t,
                    f"{t}: GM pressure {bps:+.0f} bps (>200 bps)",
                    period=period,
                    event_date=report_date,
                    event_key=f"gm_pressure_{bps:+.0f}",
                    event_at=f"{report_date}T00:00:00Z" if report_date else None,
                    bpsChange=bps,
                )
            )
            continue
        margins = dig.get("margins") or []
        reported = None
        for m in margins:
            if isinstance(m, dict) and "gross" in str(m.get("label") or "").lower():
                nums = _extract_gm_numbers(str(m.get("value") or ""))
                if nums:
                    reported = nums[0]
                    break
        guide_nums = _extract_gm_numbers(str(gd.get("grossMargin") or dig.get("guidance") or ""))
        if reported is not None and guide_nums:
            guided = guide_nums[0]
            delta_bps = (guided - reported) * 100.0
            if abs(delta_bps) > 200:
                out.append(
                    alert(
                        "gross_margin_pressure",
                        "high",
                        t,
                        f"{t}: GM guide {guided:.1f}% vs reported {reported:.1f}% ({delta_bps:+.0f} bps, >200)",
                        period=period,
                        event_date=report_date,
                        event_key=f"gm_pressure_{delta_bps:+.0f}",
                        event_at=f"{report_date}T00:00:00Z" if report_date else None,
                        bpsChange=delta_bps,
                        reportedGm=reported,
                        guidedGm=guided,
                    )
                )
    return out



def _driver_event_date(driver: dict, file_obj: dict) -> tuple[str | None, bool]:
    """Event date priority: driver.changedAt → driver.eventDate → driver.asOf
    → file.updated → file.updatedAt.

    Returns (date_yyyy_mm_dd_or_None, missing_event_date).
    NEVER falls back to today as a business eventAt.
    """
    for key in ("changedAt", "eventDate", "asOf"):
        v = driver.get(key) if isinstance(driver, dict) else None
        if v:
            s = str(v).strip()
            if s and s.lower() not in {"null", "none", "n/a", "na"}:
                return s[:10], False
    for key in ("updated", "updatedAt", "asOf"):
        v = file_obj.get(key) if isinstance(file_obj, dict) else None
        if v:
            s = str(v).strip()
            if s and s.lower() not in {"null", "none", "n/a", "na"}:
                return s[:10], False
    return None, True


def rule5_driver_or_digest_flags(tickers: list[str]) -> list[dict]:
    """Driver status transitions with reason.

    Emits one alert per distinct transition (previous→current + eventDate).
    If driver.statusHistory / transitions[] is present, emit ALL entries.
    Current Driver State remains the latest transition only (currentStatus).
    Unique Alert IDs include eventDate — NEVER collapse by (ticker, driver).
    """
    out = []

    def _emit(t, name, prev, cur, reason, event_date, missing, source_url=None, fiscal=None):
        if cur not in {"improving", "deteriorating"} or not reason:
            return
        extra = {}
        if missing:
            extra["missingEventDate"] = True
        event_at = f"{event_date}T00:00:00Z" if event_date else None
        if prev and prev != cur:
            msg = f"{t} driver '{name}': {prev} → {cur} — {reason}"
        else:
            msg = f"{t} driver '{name}': {cur} — {reason}"
            prev = prev or None
        out.append(
            alert(
                "driver_status_change",
                "medium" if cur == "improving" else "high",
                t,
                msg,
                period=fiscal or "na",
                event_date=event_date,
                event_key=name,
                event_at=event_at,
                driver=name,
                driverName=name,
                previousStatus=prev,
                currentStatus=cur,
                sourceUrl=source_url,
                **extra,
            )
        )

    for t in tickers:
        drv = load_json(DRIVERS_DIR / f"{t}.json")
        if not drv:
            continue
        for d in drv.get("drivers") or []:
            if not isinstance(d, dict):
                continue
            name = d.get("name") or "unnamed"
            reason_default = d.get("reason") or d.get("note")
            fiscal = d.get("fiscalPeriod")
            source_url = d.get("sourceUrl")
            transitions = d.get("statusHistory") or d.get("transitions") or []
            if isinstance(transitions, list) and transitions:
                for tr in transitions:
                    if not isinstance(tr, dict):
                        continue
                    prev = str(tr.get("previousStatus") or tr.get("from") or tr.get("prev") or "").lower()
                    cur = str(tr.get("currentStatus") or tr.get("to") or tr.get("status") or "").lower()
                    reason = tr.get("reason") or tr.get("note") or reason_default
                    event_date, missing = _driver_event_date(tr, drv)
                    if not event_date:
                        event_date, missing = _driver_event_date(d, drv)
                    _emit(t, name, prev, cur, reason, event_date, missing, source_url, fiscal)
            else:
                cur = str(d.get("currentStatus") or d.get("status") or "").lower()
                prev = str(d.get("previousStatus") or "").lower()
                event_date, missing = _driver_event_date(d, drv)
                if cur in {"improving", "deteriorating"} and reason_default:
                    if prev and prev != cur:
                        _emit(t, name, prev, cur, reason_default, event_date, missing, source_url, fiscal)
                    elif not prev:
                        _emit(t, name, prev, cur, reason_default, event_date, missing, source_url, fiscal)
    return out


def rule6_source_reported_1m(
    tickers: list[str] | None = None,
    prior_history: list | None = None,
    now: datetime | None = None,
    current_snapshot: dict | None = None,
    display_years: list[str] | None = None,
) -> list[dict]:
    """SA-supplied 1M revision — stateful Open / Update / Resolve (no daily spam).

    ID must NOT include snapshot date as daily spam key.
    Open when |1M| >= 5%; stay open with Update while |1M| >= 5%; Resolve when |1M| < 4%.
    Label: "Source window: Seeking Alpha 1M". NEVER call this Internal 30D.

    Must use THIS RUN's Quality-Gated snapshot when provided — do NOT rediscover
    current generation from snapshots dir (manifest.json lexicographic win breaks Source 1M).
    """
    now = now or datetime.now(timezone.utc)
    out = []
    snap = current_snapshot
    if snap is None:
        # Fallback for standalone CLI: newest validated dated snapshot only
        # (exclude manifest.json / latest.json / raw_ / quarantine)
        snap_dir = ROOT / "data" / "snapshots"
        if snap_dir.exists():
            try:
                import snapshot_quality as sq
                files = sq.list_validated_snapshots(snap_dir)
            except Exception:
                files = sorted(
                    [
                        p for p in snap_dir.glob("20*.json")
                        if not p.name.startswith("raw_")
                        and p.name not in {"latest.json", "manifest.json"}
                    ],
                    key=lambda p: p.name,
                )
            for p in reversed(files):
                try:
                    snap = load_json(p)
                    if snap:
                        break
                except Exception:
                    continue
    if not snap:
        return out
    tickers = tickers or load_tickers()
    year_keys = list(display_years) if display_years else []
    if not year_keys:
        try:
            # Prefer meta display years when available — but NEVER for rollover
            # when caller already provided this-run display_years.
            meta_p = ROOT / "web" / "data" / "meta.json"
            if meta_p.exists():
                year_keys = (json.loads(meta_p.read_text(encoding="utf-8")) or {}).get("displayMappedYears") or []
        except Exception:
            year_keys = []

    # Index prior open source_1m alerts by (ticker, slot)
    prior_open: dict[tuple, dict] = {}
    for a in prior_history or []:
        if not isinstance(a, dict):
            continue
        if a.get("rule") != "source_reported_1m_revision":
            continue
        st = str(a.get("status") or a.get("lifecycleStatus") or "").lower()
        if st == "resolved":
            continue
        slot = a.get("slot") or a.get("eventKey") or a.get("fiscalPeriod")
        key = (a.get("ticker"), slot)
        if not key[0] or not key[1]:
            continue
        prev = prior_open.get(key)
        if prev is None:
            prior_open[key] = a
        else:
            def _ts(x):
                return parse_date(x.get("updatedAt") or x.get("eventAt") or x.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)
            if _ts(a) >= _ts(prev):
                prior_open[key] = a

    evaluated = set()
    for t in tickers:
        td = (snap.get("tickers") or {}).get(t) or {}
        eps = td.get("eps") or {}
        slots = list(eps.keys())
        # Far-forward = last display year when known, else last slot
        far_slot = None
        if year_keys:
            far_slot = year_keys[-1] if year_keys else None
        elif slots:
            far_slot = sorted(slots)[-1]

        for slot, row in eps.items():
            if not isinstance(row, dict):
                continue
            rev = to_num(row.get("rev_1M_pct") if "rev_1M_pct" in row else row.get("rev1M"))
            if rev is None:
                continue
            fiscal = row.get("reported_fiscal_label") or row.get("reportedFiscalLabel") or slot
            analysts = to_num(row.get("analysts") if "analysts" in row else row.get("analystCount"))
            high = to_num(row.get("high"))
            low = to_num(row.get("low"))
            cons = to_num(row.get("consensus"))
            disp = None
            if high is not None and low is not None and cons not in (None, 0):
                disp = (high - low) / abs(cons)
            conf = consensus_confidence(analysts)
            far_forward = bool(far_slot and slot == far_slot)
            direction = "upgrade" if rev > 0 else "downgrade"
            abs_rev = abs(rev)
            key = (t, f"sa1m_{slot}")
            evaluated.add(key)
            # Prefer eventKey style matching prior
            prior = prior_open.get((t, f"sa1m_{slot}")) or prior_open.get((t, slot)) or prior_open.get((t, fiscal))

            coverage_kwargs = dict(
                analystCount=int(analysts) if analysts is not None else None,
                consensusLow=low,
                consensusHigh=high,
                consensus=cons,
                dispersion=disp,
                confidence=conf,
                farForward=far_forward,
                revisionPct=rev,
                rev1M=rev,
                slot=slot,
                fiscal=fiscal,
                sourceWindow="Seeking Alpha 1M",
                windowLabel="Source window: Seeking Alpha 1M",
                neverInternal30D=True,
                oneshot=False,
            )

            if abs_rev >= SOURCE_1M_ALERT_PCT:
                sev = severity_for_source_1m(rev, analysts, far_forward=far_forward)
                if prior is None:
                    # Open — stable ID WITHOUT snapshot date (use slot key only via event_date=None sentinel "open")
                    opened = snap.get("snapshot_utc") or now_utc_iso()
                    a = alert(
                        "source_reported_1m_revision",
                        sev,
                        t,
                        (
                            f"{t} {fiscal}: Seeking Alpha 1M revision {direction} {rev:+.2f}% (≥5%) [Open] "
                            f"— Source window: Seeking Alpha 1M"
                            + (f" · Coverage: {conf}" if conf else "")
                        ),
                        period=fiscal,
                        event_date="stateful",  # NOT snapshot date — prevents daily spam IDs
                        event_key=f"sa1m_{slot}",
                        event_at=opened,
                        status="Open",
                        lifecycleStatus="Open",
                        lifecycleEvent="Open",
                        openedAt=opened,
                        lastMaterialChangeAt=opened,
                        lastObservedAt=opened,
                        **coverage_kwargs,
                    )
                    out.append(a)
                else:
                    # Update same ID — material change or heartbeat Hold
                    a = dict(prior)
                    old_pct = to_num(prior.get("revisionPct") or prior.get("rev1M"))
                    material = old_pct is None or abs(old_pct - rev) >= SOURCE_1M_MATERIAL_PCT_DELTA
                    observed = snap.get("snapshot_utc") or now_utc_iso()
                    a.update({k: v for k, v in coverage_kwargs.items() if v is not None})
                    a["severity"] = sev
                    a["status"] = "Open"
                    a["lifecycleStatus"] = "Open"
                    a["lifecycleEvent"] = "Updated" if material else "Hold"
                    a["updatedAt"] = observed
                    a["lastObservedAt"] = observed
                    a["oneshot"] = False
                    a["expiresAt"] = None
                    a["activeUntil"] = None
                    # Preserve openedAt / business eventAt on Hold — never refresh to snapshot time
                    if not a.get("openedAt"):
                        a["openedAt"] = prior.get("openedAt") or prior.get("eventAt") or observed
                    if material:
                        a["lastMaterialChangeAt"] = observed
                        a["eventAt"] = observed  # material business event time advances
                        a["message"] = (
                            f"{t} {fiscal}: Seeking Alpha 1M revision {direction} {rev:+.2f}% (≥5%) "
                            f"[Updated] — Source window: Seeking Alpha 1M"
                            + (f" · Coverage: {conf}" if conf else "")
                        )
                        a["title"] = a["message"]
                    else:
                        # Hold: only lastObservedAt; keep eventAt / lastMaterialChangeAt
                        a["eventAt"] = prior.get("eventAt") or prior.get("openedAt") or a.get("eventAt")
                        if prior.get("lastMaterialChangeAt"):
                            a["lastMaterialChangeAt"] = prior["lastMaterialChangeAt"]
                        elif not a.get("lastMaterialChangeAt"):
                            a["lastMaterialChangeAt"] = a.get("openedAt") or a.get("eventAt")
                        a["message"] = (
                            f"{t} {fiscal}: Seeking Alpha 1M revision {direction} {rev:+.2f}% (≥5%) "
                            f"[Hold] — Source window: Seeking Alpha 1M"
                            + (f" · Coverage: {conf}" if conf else "")
                        )
                        a["title"] = a["message"]
                        a["_skipHistoryAppend"] = True
                    # Preserve stable id / createdAt / openedAt
                    out.append(a)
            elif abs_rev < SOURCE_1M_RESOLVE_PCT and prior is not None:
                resolved = dict(prior)
                resolved["status"] = "Resolved"
                resolved["lifecycleStatus"] = "Resolved"
                resolved["lifecycleEvent"] = "Resolved"
                resolved["resolvedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                resolved["oneshot"] = False
                resolved["expiresAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                resolved["activeUntil"] = resolved["expiresAt"]
                resolved["revisionPct"] = rev
                resolved["rev1M"] = rev
                resolved["message"] = (
                    f"{t} {fiscal}: Seeking Alpha 1M revision resolved (|1M|={rev:+.2f}% <4%) "
                    f"— Source window: Seeking Alpha 1M"
                )
                resolved["title"] = resolved["message"]
                resolved["windowLabel"] = "Source window: Seeking Alpha 1M"
                out.append(resolved)
            # else: between 4 and 5 → hold prior open without spam if any; ignore if none

    # Resolve prior opens whose slots disappeared / no longer evaluated
    for key, prior in prior_open.items():
        ek = (prior.get("ticker"), prior.get("eventKey") or f"sa1m_{prior.get('slot')}")
        if ek in evaluated or (prior.get("ticker"), prior.get("eventKey")) in evaluated:
            continue
        # Keep open until resolve threshold observed — do not auto-resolve on missing row
        pass

    return out



# Material rules shown on homepage (exclude informational insufficient-history)
HOMEPAGE_RULES = {
    "single_revision_gt_2pct",
    "cumulative_revision_gt_5pct",
    "source_reported_1m_revision",
    "results_vs_consensus",
    "guidance_vs_consensus",
    "gross_margin_pressure",
    "gross_margin_guidance_revision",
    "driver_status_change",
    # legacy name kept if any residual
    "gm_guidance_change_gt_200bps",
}

# Deterministic homepage Attention Queue ranking (lower = higher priority)
ATTENTION_PRIORITY = {
    "driver_deteriorating": 0,
    "revision_downside": 1,
    "guidance_downside": 2,
    "margin_pressure": 3,
    "driver_improving": 4,
    "results_beat": 5,
    "other": 9,
}


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}



def attention_rank(alert_obj: dict) -> tuple:
    """Lower tuple sorts first for homepage Attention Queue."""
    rule = alert_obj.get("rule") or ""
    cur = str(alert_obj.get("currentStatus") or "").lower()
    msg = str(alert_obj.get("message") or "").lower()
    pct = to_num(alert_obj.get("revisionPct") if alert_obj.get("revisionPct") is not None else alert_obj.get("cumulativePct"))
    bucket = "other"
    if rule == "driver_status_change" and cur == "deteriorating":
        bucket = "driver_deteriorating"
    elif rule in {"single_revision_gt_2pct", "cumulative_revision_gt_5pct", "source_reported_1m_revision"}:
        if pct is not None and pct < 0:
            bucket = "revision_downside"
        elif "downgrade" in msg or "deteriorat" in msg:
            bucket = "revision_downside"
        else:
            bucket = "revision_downside" if (pct is not None and pct < 0) else "other"
            if pct is not None and pct >= 0:
                bucket = "other"
    elif rule == "guidance_vs_consensus" and (
        "below" in msg or str(alert_obj.get("vsConsensus") or "").lower() == "below"
    ):
        bucket = "guidance_downside"
    elif rule in {"gross_margin_pressure", "gross_margin_guidance_revision", "gm_guidance_change_gt_200bps"}:
        bucket = "margin_pressure"
    elif rule == "driver_status_change" and cur == "improving":
        bucket = "driver_improving"
    elif rule == "results_vs_consensus" and (
        "above" in msg or "beat" in msg or str(alert_obj.get("vsConsensus") or "").lower() == "above"
    ):
        bucket = "results_beat"
    pri = ATTENTION_PRIORITY.get(bucket, 9)
    sev = SEVERITY_RANK.get(str(alert_obj.get("severity") or "").lower(), 9)
    ts = parse_date(alert_obj.get("eventAt") or alert_obj.get("eventDate")) or datetime.min.replace(tzinfo=timezone.utc)
    return (pri, sev, -ts.timestamp())


def build_attention_queue(active: list[dict], max_total: int = 5, max_per_ticker: int = 2) -> list[dict]:
    """Deterministic Important Alerts ranking — NOT first-N of activeAlerts."""
    ranked = sorted(active, key=attention_rank)
    out = []
    per: dict[str, int] = {}
    for a in ranked:
        t = a.get("ticker") or "?"
        if per.get(t, 0) >= max_per_ticker:
            continue
        out.append(a)
        per[t] = per.get(t, 0) + 1
        if len(out) >= max_total:
            break
    return out


def events_since_last_collection(
    history_all: list[dict],
    last_collection: str | None,
) -> list[dict]:
    """WHAT CHANGED SINCE LAST COLLECTION — only events after last successful collection."""
    if not last_collection:
        return []
    cutoff = parse_date(last_collection)
    if cutoff is None:
        return []
    out = []
    for a in history_all:
        if a.get("diagnostic") or a.get("_skipHistoryAppend"):
            continue
        if a.get("lifecycleEvent") == "Hold":
            continue
        ev = parse_date(a.get("eventAt") or a.get("updatedAt") or a.get("createdAt") or a.get("eventDate"))
        if ev is None:
            continue
        if ev > cutoff:
            out.append(a)
    out.sort(
        key=lambda x: (
            -(parse_date(x.get("eventAt") or x.get("updatedAt") or x.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
        )
    )
    return out


def evaluate_alerts(
    now: datetime | None = None,
    current_snapshot: dict | None = None,
    snapshot_utc: str | None = None,
    display_years: list[str] | None = None,
) -> dict:
    """Evaluate alerts against THIS RUN's Quality-Gated snapshot context.

    Alert Engine must NOT guess current generation from snapshots dir
    (manifest.json would win lexicographic reverse sort → Source 1M Alerts=0).
    Pass current_snapshot + display_years from the gated ingest/export path.
    """
    now = now or datetime.now(timezone.utc)
    tickers = load_tickers()
    history = load_history()
    daily_rows = load_daily_jsonl()
    prior = load_json(ALERTS_PATH) or {}
    prior_hist = prior.get("alertHistory") or prior.get("alerts") or []

    generated: list[dict] = []
    diagnostics: list[dict] = []
    generated.extend(rule1_single_revision(history))
    cum_alerts, cum_diag = rule2_cumulative(
        history, daily_rows=daily_rows, now=now, prior_history=prior_hist
    )
    generated.extend(cum_alerts)
    diagnostics.extend(cum_diag)
    generated.extend(rule3_results_and_guidance(tickers))
    generated.extend(rule4_gm(tickers))
    generated.extend(rule5_driver_or_digest_flags(tickers))
    generated.extend(
        rule6_source_reported_1m(
            tickers,
            prior_history=prior_hist,
            now=now,
            current_snapshot=current_snapshot,
            display_years=display_years,
        )
    )

    # Deduplicate ONLY by full unique Alert ID (keep first / prefer newer generated)
    seen = set()
    uniq = []
    for a in generated:
        aid = a.get("id")
        if aid in seen:
            continue
        seen.add(aid)
        uniq.append(a)

    # Merge with prior history (long retention) — exclude diagnostics / insufficient-history
    # NO cross-event collapse of driver_status_change by (ticker, driver).
    hist_by_id = {}
    for a in prior_hist:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if a.get("rule") == "cumulative_revision_insufficient_history" or a.get("diagnostic"):
            continue
        if a.get("status") == "insufficient history":
            continue
        hist_by_id[a.get("id")] = a

    for a in uniq:
        if a.get("diagnostic") or a.get("rule") == "cumulative_revision_insufficient_history":
            continue
        if a.get("_skipHistoryAppend") and a.get("lifecycleEvent") == "Hold":
            # Still refresh active copy in hist_by_id without new history spam semantics
            aid = a.get("id")
            if aid and aid in hist_by_id:
                old = hist_by_id[aid]
                merged = dict(old)
                for k, v in a.items():
                    if k.startswith("_"):
                        continue
                    if k in {"createdAt", "openedAt", "id", "eventAt", "lastMaterialChangeAt"}:
                        continue  # never refresh business event time on Hold
                    if v is not None:
                        merged[k] = v
                merged["lastObservedAt"] = a.get("lastObservedAt") or merged.get("lastObservedAt")
                # Keep prior lifecycleEvent if Hold
                if old.get("lifecycleEvent") in {"Open", "Updated"}:
                    merged["lifecycleEvent"] = old.get("lifecycleEvent")
                hist_by_id[aid] = merged
            elif aid:
                b = {k: v for k, v in a.items() if not k.startswith("_")}
                hist_by_id[aid] = b
            continue
        aid = a.get("id")
        if aid in hist_by_id:
            old = hist_by_id[aid]
            if old.get("createdAt"):
                a["createdAt"] = old["createdAt"]
            if old.get("openedAt") and not a.get("openedAt"):
                a["openedAt"] = old["openedAt"]
            if old.get("expiresAt") and a.get("expiresAt") is None and a.get("oneshot") is not False:
                a["expiresAt"] = old["expiresAt"]
                a["activeUntil"] = old.get("activeUntil") or old["expiresAt"]
        clean = {k: v for k, v in a.items() if not k.startswith("_")}
        hist_by_id[aid] = clean

    history_all = list(hist_by_id.values())

    # Stamp ageDays (operational — UI may also compute from eventAt)
    for a in history_all:
        a["ageDays"] = age_days(a, now)

    active = []
    for a in history_all:
        if a.get("rule") not in HOMEPAGE_RULES and a.get("severity") == "info":
            continue
        if a.get("diagnostic"):
            continue
        if is_active(a, now) and a.get("rule") in HOMEPAGE_RULES:
            a["ageDays"] = age_days(a, now)
            active.append(a)

    active.sort(
        key=lambda x: (
            SEVERITY_RANK.get(str(x.get("severity") or "").lower(), 9),
            -(parse_date(x.get("eventAt")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        )
    )

    attention = build_attention_queue(active, max_total=5, max_per_ticker=2)

    # What-Changed uses comparisonCheckpoint (previous successful export), not wall clock.
    # Checkpoint is advanced by export_web_data AFTER successful collection/export.
    last_coll = None
    cp_path = ROOT / "data" / "comparison_checkpoint.json"
    if cp_path.exists():
        try:
            cp = json.loads(cp_path.read_text(encoding="utf-8")) or {}
            last_coll = cp.get("comparisonCheckpoint") or cp.get("snapUtc")
        except Exception:
            last_coll = None
    if not last_coll:
        last_coll = prior.get("comparisonCheckpoint") or prior.get("lastSuccessfulCollection")
    if not last_coll:
        meta_web = ROOT / "web" / "data" / "meta.json"
        if meta_web.exists():
            try:
                last_coll = (json.loads(meta_web.read_text(encoding="utf-8")) or {}).get(
                    "lastSuccessfulCollection"
                )
            except Exception:
                last_coll = None
    changed_since = events_since_last_collection(history_all, last_coll)

    # Merge prior diagnostics (keep recent) + new
    prior_diag = prior.get("alertDiagnostics") or []
    diag_by_key = {}
    for d in list(prior_diag) + diagnostics:
        if not isinstance(d, dict):
            continue
        key = (d.get("rule"), d.get("ticker"), d.get("period"), d.get("status"))
        diag_by_key[key] = d
    alert_diagnostics = list(diag_by_key.values())

    return {
        "activeAlerts": active,
        "alertHistory": history_all,
        "alertDiagnostics": alert_diagnostics,
        "homepageAttentionQueue": attention,
        "changedSinceLastCollection": changed_since,
        # Legacy alias for older consumers during transition
        "alerts": active,
        "alertEngineLastEvaluated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alertEngineLastAttempt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alertEngineLastSuccessfulEvaluation": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alertEngineStatus": "ok",
        "alertEngineError": None,
        "oneShotActiveDays": ONE_SHOT_ACTIVE_DAYS,
        "lastSuccessfulCollection": last_coll,
        "comparisonCheckpoint": last_coll,
        "rules": [
            "single_revision_gt_2pct: |revisionPct| > 2% (revision events)",
            "cumulative_revision_gt_5pct: Internal 30D stateful (≥5% Open / Update same ID; <4% Resolve); diagnostics for insufficient history",
            "source_reported_1m_revision: SA |1M|≥5% stateful Open/Update/Resolve (ID without snapshot date); Source window: Seeking Alpha 1M; coverage from analystCount (not consensus confidence)",
            "results_vs_consensus / guidance_vs_consensus: split; guidance only if above/below; results uses consensusComparison provenance",
            "gross_margin_pressure / gross_margin_guidance_revision (prev+current guidance)",
            "driver_status_change: all transitions retained by unique Alert ID (no ticker+driver collapse); eventAt from changedAt priority",
            f"lifecycle: one-shot active {ONE_SHOT_ACTIVE_DAYS}d; cumulative stateful; history retained; diagnostics excluded",
            "homepageAttentionQueue: deterioration→revision→guidance→margin→improving→beat; max 2/ticker, max 5",
        ],
    }



def write_alerts(payload: dict) -> Path:
    """Atomic write only — fail-closed, no silent non-atomic fallback."""
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import atomic_write_json
    atomic_write_json(ALERTS_PATH, payload)
    return ALERTS_PATH


def main() -> int:
    try:
        payload = evaluate_alerts()
        write_alerts(payload)
        print(
            f"Wrote {ALERTS_PATH}: active={len(payload['activeAlerts'])} "
            f"history={len(payload['alertHistory'])} status={payload['alertEngineStatus']} "
            f"oneShotActiveDays={ONE_SHOT_ACTIVE_DAYS}"
        )
        return 0
    except Exception as exc:
        prior = load_json(ALERTS_PATH) or {}
        prior_active = prior.get("activeAlerts") or prior.get("alerts") or []
        prior_hist = prior.get("alertHistory") or prior_active
        now = now_utc_iso()
        err = {
            "activeAlerts": list(prior_active),
            "alertHistory": list(prior_hist),
            "alerts": list(prior_active),
            "alertDiagnostics": prior.get("alertDiagnostics") or [],
            "homepageAttentionQueue": prior.get("homepageAttentionQueue") or [],
            "changedSinceLastCollection": prior.get("changedSinceLastCollection") or [],
            "alertEngineLastEvaluated": prior.get("alertEngineLastEvaluated"),
            "alertEngineLastAttempt": now,
            "alertEngineLastSuccessfulEvaluation": prior.get("alertEngineLastSuccessfulEvaluation")
            or prior.get("alertEngineLastEvaluated"),
            "alertEngineStatus": "error",
            "alertEngineError": f"{type(exc).__name__}: {exc}",
            "oneShotActiveDays": prior.get("oneShotActiveDays") or ONE_SHOT_ACTIVE_DAYS,
            "lastSuccessfulCollection": prior.get("lastSuccessfulCollection"),
        }
        try:
            write_alerts(err)
        except Exception:
            pass
        print(f"ALERT ENGINE ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

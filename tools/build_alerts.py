#!/usr/bin/env python3
"""Deterministic alert engine for ai-eps-monitor.

Rules (documented, no ML / no invented thresholds beyond these):
  1. Single consensus EPS revision |revisionPct| > 2% (from revision events;
     baselines / n/a skipped). Computes pct from previous→current when missing.
  2. Cumulative 30D revision — STATEFUL with hysteresis:
       Open/Update when |pct| >= 5%; Resolve when |pct| < 4%.
       Stable Alert ID (no calendar-day / exact-pct spam).
       Primary source: daily.jsonl nearest obs at window start vs latest.
       If <2 usable daily points in window → alertDiagnostics only (NOT alertHistory).
  3. Results vs consensus (resultsVsConsensus) and guidance vs consensus
     (guidanceVsConsensus) are separate. Guidance alert ONLY when
     guidanceDetail.vsConsensus in {above, below}; unknown → no guidance alert.
  4. gross_margin_pressure (result/pressure) vs gross_margin_guidance_revision
     (requires previousGuidance + currentGuidance). Legacy GM >200bps pressure
     when parsable.
  5. Driver status → improving/deteriorating with reason.
     Dedupe by FULL Alert ID only — never collapse (ticker, driver).
     Multiple transitions (Aug 26 improving + Oct 30 deteriorating) all stay
     in alertHistory.
  6. Seeking Alpha source-reported |1M| >= 5% → source_reported_1m_revision
     labeled "Seeking Alpha 1M" (NOT Internal 30D).

Alert ID = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey
(stable, unique — never collapse multiple drivers onto :na).

Lifecycle:
  - One-shot events expire from activeAlerts after ONE_SHOT_ACTIVE_DAYS (21).
  - Stateful cumulative / SA-1M alerts stay active until hysteresis resolve.
  - alertHistory retains material evaluated alerts (long retention).
  - insufficient-history records go to alertDiagnostics only (no history/daily pollution).
  - activeAlerts = history items still within active window.
  - attentionQueue = homepage ranking (downside first, max 2/ticker, max 5).

Writes data/alerts/index.json with activeAlerts + alertHistory (+ legacy alerts alias)
via flock + atomic rename.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_text
except ImportError:
    _sys_path_here = Path(__file__).resolve().parent
    if str(_sys_path_here) not in sys.path:
        sys.path.insert(0, str(_sys_path_here))
    from atomic_io import atomic_write_text

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if not (ROOT / "data" / "snapshots").exists():
    cand = Path(__file__).resolve().parent
    for _ in range(5):
        if (cand / "data" / "snapshots").exists():
            ROOT = cand
            break
        cand = cand.parent

REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
EARNINGS_DIR = ROOT / "data" / "earnings"
DRIVERS_DIR = ROOT / "data" / "drivers"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
UNIVERSE_PATH = ROOT / "data" / "universe.json"
DAILY_JSONL = ROOT / "data" / "daily_eps_snapshots" / "daily.jsonl"
SNAP_DIR = ROOT / "data" / "snapshots"

TICKERS_DEFAULT = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]

# One-shot homepage window (within 14–30 days). Documented choice: 21 days.
ONE_SHOT_ACTIVE_DAYS = 21
CUMULATIVE_LOOKBACK_DAYS = 30
# Stateful cumulative 30D hysteresis (percent points, same units as daily.jsonl math).
CUMULATIVE_OPEN_PCT = 5.0
CUMULATIVE_RESOLVE_PCT = 4.0
# SA source-reported 1M revision threshold (Seeking Alpha 1M, not Internal 30D).
SA_1M_ALERT_PCT = 5.0
ATTENTION_QUEUE_MAX = 5
ATTENTION_QUEUE_MAX_PER_TICKER = 2


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("%", "").replace("$", "")
    if s.lower() in {"", "n/a", "na", "n/a (baseline)", "data unavailable", "null", "none"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


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
    now = now or datetime.now(timezone.utc)
    ev = parse_date(alert_obj.get("eventAt") or alert_obj.get("eventDate") or alert_obj.get("createdAt"))
    if ev is None:
        return 0
    return max(0, int((now - ev).total_seconds() // 86400))


def is_active(alert_obj: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if str(alert_obj.get("lifecycleState") or "").lower() == "resolved":
        return False
    until = parse_date(alert_obj.get("expiresAt") or alert_obj.get("activeUntil"))
    if until is not None:
        return now <= until
    # Non-expiring stateful alerts (cumulative / SA 1M) stay active until resolved
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


def compute_cumulative_windows(
    history: list[dict] | None = None,
    lookback_days: int = CUMULATIVE_LOOKBACK_DAYS,
    daily_rows: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """True lookback-window first→last revision measurements.

    Returns (windows, diagnostics). Each window has ticker, fiscal, cumulativePct,
    start/end EPS+dates. Diagnostics carry insufficient-history (NOT alerts).
    NEVER fall back to all-history while claiming a 30D window.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_start = now - timedelta(days=lookback_days)

    rows = daily_rows if daily_rows is not None else load_daily_jsonl()
    groups: dict[tuple, list] = {}
    for row in rows:
        ticker = row.get("ticker") or row.get("Ticker")
        fiscal = (
            row.get("reportedFiscalLabel")
            or row.get("reportedFiscalPeriodEnding")
            or row.get("fiscalKey")
            or row.get("Fiscal Year")
        )
        if not ticker or not fiscal:
            continue
        cons = to_num(row.get("consensus") if "consensus" in row else row.get("Current EPS"))
        if cons is None:
            continue
        dt = parse_date(row.get("date") or row.get("Date") or row.get("updateTime") or row.get("Update Time"))
        if dt is None:
            continue
        groups.setdefault((ticker, fiscal), []).append((dt, cons, row))

    if not groups and history:
        for row in history:
            ticker = row.get("Ticker")
            fiscal = row.get("Fiscal Year") or row.get("Calendar Alignment")
            if not ticker or not fiscal:
                continue
            cur = to_num(row.get("Current EPS"))
            if cur is None:
                continue
            dt = parse_date(row.get("Date") or row.get("Update Time"))
            if dt is None:
                continue
            groups.setdefault((ticker, fiscal), []).append((dt, cur, row))

    windows: list[dict] = []
    diagnostics: list[dict] = []
    for (ticker, fiscal), pts in groups.items():
        pts_sorted = sorted(pts, key=lambda x: x[0])
        before = [p for p in pts_sorted if p[0] <= window_start]
        latest_candidates = [p for p in pts_sorted if p[0] <= now]
        if not latest_candidates:
            continue
        latest = latest_candidates[-1]

        start_pt = before[-1] if before else None
        MAX_START_SLACK_DAYS = 2
        if start_pt is not None and (window_start - start_pt[0]).days > MAX_START_SLACK_DAYS:
            start_pt = None
        if start_pt is None:
            diagnostics.append(
                {
                    "rule": "cumulative_revision_insufficient_history",
                    "severity": "info",
                    "ticker": ticker,
                    "period": fiscal,
                    "message": (
                        f"{ticker} {fiscal}: insufficient history for {lookback_days}D cumulative "
                        "(no valid observation at/before window start)"
                    ),
                    "lookbackDays": lookback_days,
                    "windowPointCount": len([p for p in pts_sorted if window_start <= p[0] <= now]),
                    "status": "insufficient history",
                    "eventAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "diagnostic": True,
                }
            )
            continue

        if latest[0] <= start_pt[0] or latest[1] is None:
            diagnostics.append(
                {
                    "rule": "cumulative_revision_insufficient_history",
                    "severity": "info",
                    "ticker": ticker,
                    "period": fiscal,
                    "message": (
                        f"{ticker} {fiscal}: insufficient history for {lookback_days}D cumulative "
                        "(<2 distinct points spanning window)"
                    ),
                    "lookbackDays": lookback_days,
                    "windowPointCount": 1,
                    "status": "insufficient history",
                    "eventAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "diagnostic": True,
                }
            )
            continue

        first = start_pt[1]
        last = latest[1]
        if first is None or last is None or first == 0:
            continue
        cum_pct = (last - first) / abs(first) * 100.0
        windows.append(
            {
                "ticker": ticker,
                "fiscal": fiscal,
                "cumulativePct": cum_pct,
                "lookbackDays": lookback_days,
                "startEps": first,
                "endEps": last,
                "startDate": start_pt[0].strftime("%Y-%m-%d"),
                "endDate": latest[0].strftime("%Y-%m-%d"),
                "eventAt": latest[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return windows, diagnostics


def cumulative_stable_id(ticker: str, fiscal: str) -> str:
    """Stable ID — window identity only. No calendar day, no exact pct (no daily spam)."""
    return make_alert_id("cumulative_revision_gt_5pct", ticker, fiscal, "30d", "cum30d")


def _cumulative_alert_from_window(w: dict, *, lifecycle_state: str, oneshot: bool = False) -> dict:
    cum_pct = w["cumulativePct"]
    ticker = w["ticker"]
    fiscal = w["fiscal"]
    lookback_days = w.get("lookbackDays") or CUMULATIVE_LOOKBACK_DAYS
    direction = "upgrade" if cum_pct > 0 else "downgrade"
    a = alert(
        "cumulative_revision_gt_5pct",
        "high",
        ticker,
        f"{ticker} {fiscal}: cumulative {lookback_days}D revision {direction} {cum_pct:+.2f}% "
        f"({'≥5% Open/Update' if abs(cum_pct) >= CUMULATIVE_OPEN_PCT else 'hysteresis'})",
        period=fiscal,
        event_date="30d",
        event_key="cum30d",
        event_at=w.get("eventAt"),
        cumulativePct=cum_pct,
        lookbackDays=lookback_days,
        fiscal=fiscal,
        startEps=w.get("startEps"),
        endEps=w.get("endEps"),
        startDate=w.get("startDate"),
        oneshot=oneshot,
        lifecycleState=lifecycle_state,
        windowIdentity="30d",
    )
    a["id"] = cumulative_stable_id(ticker, fiscal)
    if lifecycle_state == "resolved":
        a["expiresAt"] = w.get("eventAt") or now_utc_iso()
        a["activeUntil"] = a["expiresAt"]
    else:
        a["expiresAt"] = None
        a["activeUntil"] = None
        a["oneshot"] = False
    return a


def apply_cumulative_hysteresis(
    windows: list[dict],
    prior_hist: list[dict] | None,
    now: datetime | None = None,
) -> list[dict]:
    """Open/Update |pct|>=5%; Resolve |pct|<4%; keep same Alert ID (no daily spam)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    prior_by_id = {}
    for a in prior_hist or []:
        if isinstance(a, dict) and a.get("rule") == "cumulative_revision_gt_5pct" and a.get("id"):
            prior_by_id[a["id"]] = a

    out: list[dict] = []
    seen = set()
    for w in windows:
        aid = cumulative_stable_id(w["ticker"], w["fiscal"])
        seen.add(aid)
        pct = abs(float(w.get("cumulativePct") or 0))
        prior = prior_by_id.get(aid)
        prior_state = str((prior or {}).get("lifecycleState") or "").lower()
        prior_open = bool(prior) and prior_state != "resolved" and (
            prior_state == "open" or (prior.get("oneshot") is False and is_active(prior, now))
            or (prior_state == "" and is_active(prior, now))
        )
        if prior_open:
            if pct >= CUMULATIVE_RESOLVE_PCT:
                a = _cumulative_alert_from_window(w, lifecycle_state="open")
                if prior.get("createdAt"):
                    a["createdAt"] = prior["createdAt"]
                a["lifecycleState"] = "open"
                out.append(a)
            else:
                a = _cumulative_alert_from_window(w, lifecycle_state="resolved")
                if prior.get("createdAt"):
                    a["createdAt"] = prior["createdAt"]
                a["resolvedAt"] = now_iso
                a["lifecycleState"] = "resolved"
                out.append(a)
        else:
            if pct >= CUMULATIVE_OPEN_PCT:
                a = _cumulative_alert_from_window(w, lifecycle_state="open")
                a["lifecycleState"] = "open"
                out.append(a)
            elif prior and prior_state == "resolved":
                # Stay resolved; keep history row (do not re-open below 5%)
                out.append(prior)
    for aid, prior in prior_by_id.items():
        if aid in seen:
            continue
        prior_state = str(prior.get("lifecycleState") or "").lower()
        if prior_state == "resolved":
            out.append(prior)
            continue
        # Measurement disappeared this run — resolve rather than spam a new ID
        resolved = dict(prior)
        resolved["lifecycleState"] = "resolved"
        resolved["resolvedAt"] = now_iso
        resolved["expiresAt"] = now_iso
        resolved["activeUntil"] = now_iso
        out.append(resolved)
    return out


def rule2_cumulative(
    history: list[dict] | None = None,
    lookback_days: int = CUMULATIVE_LOOKBACK_DAYS,
    daily_rows: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Cumulative first→last revision in TRUE lookback window ≥ 5% (open threshold).

    Direct callers (unit tests) receive Open-threshold alerts with STABLE ids.
    Stateful hysteresis (keep-open 4–5%, resolve <4%) is applied in evaluate_alerts.
    """
    windows, diagnostics = compute_cumulative_windows(
        history=history, lookback_days=lookback_days, daily_rows=daily_rows, now=now
    )
    out = [
        _cumulative_alert_from_window(w, lifecycle_state="open")
        for w in windows
        if abs(float(w.get("cumulativePct") or 0)) >= CUMULATIVE_OPEN_PCT
    ]
    return out, diagnostics


def _norm_vs(val) -> str | None:
    if not isinstance(val, str):
        return None
    c = val.strip().lower()
    if c in {"above", "below"}:
        return c
    return None



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
            return url, tier
    # Heuristic: Seeking Alpha sources in digest.sources list
    for s in dig.get("sources") or []:
        if not isinstance(s, dict):
            continue
        tier = s.get("sourceTier") if s.get("sourceTier") is not None else s.get("tier")
        attr = str(s.get("attribution") or s.get("title") or "").lower()
        url = s.get("url") or s.get("sourceUrl")
        if tier == 4 or "seeking alpha" in attr or (url and "seekingalpha.com" in str(url).lower()):
            return url, tier if tier is not None else 4
    # results.notes often cite SA beat/miss — still do not use results.sourceUrl (IR)
    notes = str(((dig.get("results") or {}) if isinstance(dig.get("results"), dict) else {}).get("notes") or "")
    if "seeking alpha" in notes.lower() or "sa earnings" in notes.lower():
        # Find any SA URL in sources; else leave url None but tier 4
        for s in dig.get("sources") or []:
            if isinstance(s, dict) and s.get("url") and "seekingalpha.com" in str(s.get("url")).lower():
                return s.get("url"), 4
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
    """Driver status changed to improving/deteriorating with reason — unique per driver."""
    out = []
    for t in tickers:
        drv = load_json(DRIVERS_DIR / f"{t}.json")
        if not drv:
            continue
        for d in drv.get("drivers") or []:
            if not isinstance(d, dict):
                continue
            cur = str(d.get("currentStatus") or d.get("status") or "").lower()
            prev = str(d.get("previousStatus") or "").lower()
            reason = d.get("reason") or d.get("note")
            name = d.get("name") or "unnamed"
            event_date, missing = _driver_event_date(d, drv)
            # NEVER use today as business eventAt
            event_at = f"{event_date}T00:00:00Z" if event_date else None
            if cur in {"improving", "deteriorating"} and reason:
                extra = {}
                if missing:
                    extra["missingEventDate"] = True
                if prev and prev != cur:
                    out.append(
                        alert(
                            "driver_status_change",
                            "medium",
                            t,
                            f"{t} driver '{name}': {prev} → {cur} — {reason}",
                            period=d.get("fiscalPeriod") or "na",
                            event_date=event_date,
                            event_key=name,
                            event_at=event_at,
                            driver=name,
                            driverName=name,
                            previousStatus=prev,
                            currentStatus=cur,
                            sourceUrl=d.get("sourceUrl"),
                            **extra,
                        )
                    )
                elif not prev and cur in {"improving", "deteriorating"}:
                    out.append(
                        alert(
                            "driver_status_change",
                            "medium",
                            t,
                            f"{t} driver '{name}': {cur} — {reason}",
                            period=d.get("fiscalPeriod") or "na",
                            event_date=event_date,
                            event_key=name,
                            event_at=event_at,
                            driver=name,
                            driverName=name,
                            currentStatus=cur,
                            sourceUrl=d.get("sourceUrl"),
                            **extra,
                        )
                    )
    return out


def load_latest_snapshot_dict() -> dict | None:
    if not SNAP_DIR.exists():
        return None
    dated = sorted(
        p
        for p in SNAP_DIR.glob("20*.json")
        if not p.name.startswith("raw_") and re.match(r"^\d{4}-\d{2}-\d{2}", p.name)
    )
    if not dated:
        return None
    try:
        return json.loads(dated[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def sa_1m_stable_id(ticker: str, fiscal: str) -> str:
    return make_alert_id("source_reported_1m_revision", ticker, fiscal, "sa1m", "sa_1m")


def rule6_source_reported_1m(tickers: list[str], snap: dict | None = None) -> list[dict]:
    """Seeking Alpha source-reported |1M| >= 5%.

    Labeled 'Seeking Alpha 1M' — never 'Internal 30D'. Distinct from
    cumulative_revision_gt_5pct (which is internally computed over daily.jsonl).
    Stable ID (no daily spam while the SA 1M reading stays over threshold).
    """
    snap = snap if snap is not None else load_latest_snapshot_dict()
    if not snap:
        return []
    tickers_blob = snap.get("tickers") or {}
    out = []
    for t in tickers:
        td = tickers_blob.get(t) or {}
        eps = td.get("eps") or {}
        for slot, year in eps.items():
            if not isinstance(year, dict):
                continue
            rev = to_num(year.get("rev_1M_pct") if "rev_1M_pct" in year else year.get("rev1M"))
            if rev is None or abs(rev) < SA_1M_ALERT_PCT:
                continue
            fiscal = year.get("reported_fiscal_label") or year.get("reportedFiscalLabel") or slot
            direction = "upgrade" if rev > 0 else "downgrade"
            a = alert(
                "source_reported_1m_revision",
                "high",
                t,
                f"{t} {fiscal}: Seeking Alpha 1M {direction} {rev:+.2f}% (≥5%)",
                period=fiscal,
                event_date="sa1m",
                event_key="sa_1m",
                event_at=snap.get("snapshot_utc"),
                revisionPct=rev,
                source="Seeking Alpha 1M",
                sourceLabel="Seeking Alpha 1M",
                sourceWindow="1M",
                notInternal30d=True,
                oneshot=False,
                lifecycleState="open",
                slot=slot,
            )
            a["id"] = sa_1m_stable_id(t, fiscal)
            a["expiresAt"] = None
            a["activeUntil"] = None
            out.append(a)
    return out


def apply_sa_1m_state(generated: list[dict], prior_hist: list[dict] | None, now: datetime | None = None) -> list[dict]:
    """Keep SA 1M alerts stateful by stable ID; resolve when |1M| drops below 5%."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    gen_ids = {a.get("id") for a in generated}
    prior_by_id = {
        a.get("id"): a
        for a in (prior_hist or [])
        if isinstance(a, dict) and a.get("rule") == "source_reported_1m_revision" and a.get("id")
    }
    out = list(generated)
    for aid, prior in prior_by_id.items():
        if aid in gen_ids:
            # Update: preserve createdAt
            for a in out:
                if a.get("id") == aid and prior.get("createdAt"):
                    a["createdAt"] = prior["createdAt"]
            continue
        if str(prior.get("lifecycleState") or "").lower() == "resolved":
            out.append(prior)
            continue
        resolved = dict(prior)
        resolved["lifecycleState"] = "resolved"
        resolved["resolvedAt"] = now_iso
        resolved["expiresAt"] = now_iso
        resolved["activeUntil"] = now_iso
        out.append(resolved)
    return out


def is_downside_alert(a: dict) -> bool:
    status = str(a.get("currentStatus") or "").lower()
    if status == "deteriorating":
        return True
    msg = str(a.get("message") or a.get("title") or "").lower()
    if any(w in msg for w in ("downgrade", "deteriorating", "below", "pressure", "negative")):
        return True
    for key in ("revisionPct", "cumulativePct"):
        v = a.get(key)
        if isinstance(v, (int, float)) and v < 0:
            return True
    vs = str(a.get("resultsVsConsensus") or a.get("guidanceVsConsensus") or a.get("comparison") or "").lower()
    if vs == "below":
        return True
    return False


def attention_sort_key(a: dict) -> tuple:
    downside = 0 if is_downside_alert(a) else 1
    sev = SEVERITY_RANK.get(str(a.get("severity") or "").lower(), 9)
    ev = parse_date(a.get("eventAt") or a.get("eventDate") or a.get("createdAt"))
    recency = -(ev.timestamp()) if ev else 0.0
    return (downside, sev, recency)


def build_attention_queue(
    active: list[dict],
    *,
    max_total: int = ATTENTION_QUEUE_MAX,
    max_per_ticker: int = ATTENTION_QUEUE_MAX_PER_TICKER,
) -> list[dict]:
    """Homepage Attention Queue: downside first, max 2 per ticker, max 5."""
    ranked = sorted([a for a in active if isinstance(a, dict)], key=attention_sort_key)
    counts: dict[str, int] = {}
    queue: list[dict] = []
    for a in ranked:
        t = str(a.get("ticker") or "")
        if counts.get(t, 0) >= max_per_ticker:
            continue
        queue.append(a)
        counts[t] = counts.get(t, 0) + 1
        if len(queue) >= max_total:
            break
    return queue


# Material rules shown on homepage (exclude informational insufficient-history)
HOMEPAGE_RULES = {
    "single_revision_gt_2pct",
    "cumulative_revision_gt_5pct",
    "results_vs_consensus",
    "guidance_vs_consensus",
    "gross_margin_pressure",
    "gross_margin_guidance_revision",
    "driver_status_change",
    "source_reported_1m_revision",
    # legacy name kept if any residual
    "gm_guidance_change_gt_200bps",
}

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}



def evaluate_alerts(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tickers = load_tickers()
    history = load_history()
    daily_rows = load_daily_jsonl()
    prior = load_json(ALERTS_PATH) or {}
    prior_hist = prior.get("alertHistory") or prior.get("alerts") or []

    generated: list[dict] = []
    diagnostics: list[dict] = []
    generated.extend(rule1_single_revision(history))

    windows, cum_diag = compute_cumulative_windows(history, daily_rows=daily_rows, now=now)
    diagnostics.extend(cum_diag)
    generated.extend(apply_cumulative_hysteresis(windows, prior_hist, now=now))

    generated.extend(rule3_results_and_guidance(tickers))
    generated.extend(rule4_gm(tickers))
    generated.extend(rule5_driver_or_digest_flags(tickers))
    sa_1m = rule6_source_reported_1m(tickers)
    sa_1m = apply_sa_1m_state(sa_1m, prior_hist, now=now)
    generated.extend(sa_1m)

    # Deduplicate by FULL Alert ID only (keep last generated / first prior merge below).
    # driver_status_change is NOT collapsed by (ticker, driver) — every transition ID is kept.
    seen = set()
    uniq = []
    for a in generated:
        aid = a.get("id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        uniq.append(a)

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
        aid = a.get("id")
        if aid in hist_by_id:
            old = hist_by_id[aid]
            if old.get("createdAt"):
                a["createdAt"] = old["createdAt"]
            if a.get("oneshot") is not False and old.get("expiresAt") and not a.get("expiresAt"):
                a["expiresAt"] = old["expiresAt"]
                a["activeUntil"] = old.get("activeUntil") or old["expiresAt"]
        hist_by_id[aid] = a

    history_all = list(hist_by_id.values())

    # Stamp ageDays (operational — stripped from dataVersion hash)
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

    active.sort(key=attention_sort_key)
    attention_queue = build_attention_queue(active)

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
        "attentionQueue": attention_queue,
        "attentionQueueRule": "downside-first; max 2 per ticker; max 5",
        "alerts": active,
        "alertEngineLastEvaluated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alertEngineStatus": "ok",
        "alertEngineError": None,
        "oneShotActiveDays": ONE_SHOT_ACTIVE_DAYS,
        "cumulativeHysteresis": {
            "openPct": CUMULATIVE_OPEN_PCT,
            "resolvePct": CUMULATIVE_RESOLVE_PCT,
        },
        "rules": [
            "single_revision_gt_2pct: |revisionPct| > 2% (revision events)",
            "cumulative_revision_gt_5pct: stateful; ≥5% Open/Update, <4% Resolve; stable ID (no daily spam)",
            "results_vs_consensus / guidance_vs_consensus: split; guidance only if above/below; results uses consensusComparison provenance",
            "gross_margin_pressure / gross_margin_guidance_revision (prev+current guidance)",
            "driver_status_change: unique per full Alert ID (ticker+driver+eventDate); all transitions kept in history",
            "source_reported_1m_revision: SA |1M|≥5% labeled Seeking Alpha 1M (not Internal 30D)",
            f"lifecycle: one-shot active {ONE_SHOT_ACTIVE_DAYS}d; stateful cumulative/1M until resolve; diagnostics excluded from history",
            "attentionQueue: downside first, max 2/ticker, max 5",
        ],
    }


def write_alerts(payload: dict) -> Path:
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ALERTS_PATH, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
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
        err = {
            "activeAlerts": [],
            "alertHistory": [],
            "alerts": [],
            "alertEngineLastEvaluated": now_utc_iso(),
            "alertEngineStatus": "error",
            "alertEngineError": f"{type(exc).__name__}: {exc}",
            "oneShotActiveDays": ONE_SHOT_ACTIVE_DAYS,
        }
        try:
            write_alerts(err)
        except Exception:
            pass
        print(f"ALERT ENGINE ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

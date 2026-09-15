#!/usr/bin/env python3
"""Deterministic alert engine for ai-eps-monitor.

Rules (documented, no ML / no invented thresholds beyond these):
  1. Single consensus EPS revision |revisionPct| > 2% (from revision events;
     baselines / n/a skipped). Computes pct from previous→current when missing.
  2. Cumulative internal-history revision for a fiscal slot > 5% over a TRUE
     30-day window. If fewer than 2 points fall in that window → insufficient
     history (never fall back to all-history and label it 30D).
  3. results_vs_consensus when resultsVsConsensus is above/below.
     guidance_vs_consensus ONLY when guidanceDetail.vsConsensus is above/below;
     unknown → no guidance alert.
  4. gross_margin_guidance_revision requires Previous Guidance AND Current
     Guidance and |change| > 200 bps. gross_margin_pressure covers GM stress
     without a dual-guidance pair.
  5. Material driver status → improving/deteriorating with reason.

Alert id = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey

Writes data/alerts/index.json:
  { activeAlerts, alertHistory, alerts (alias of active),
    alertEngineLastEvaluated, alertEngineStatus, alertEngineError }
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
ROOT = _here.parent
if not (ROOT / "data" / "snapshots").exists():
    cand = Path(__file__).resolve().parent
    for _ in range(5):
        if (cand / "data" / "snapshots").exists() or (cand / "tests" / "fixtures" / "data" / "snapshots").exists():
            ROOT = cand
            break
        cand = cand.parent

REV_PATH = ROOT / "data" / "revisions" / "history.jsonl"
EARNINGS_DIR = ROOT / "data" / "earnings"
DRIVERS_DIR = ROOT / "data" / "drivers"
ALERTS_PATH = ROOT / "data" / "alerts" / "index.json"
UNIVERSE_PATH = ROOT / "data" / "universe.json"

TICKERS_DEFAULT = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]
HOMEPAGE_TTL_DAYS = 30
ONE_SHOT_RULES = {
    "single_revision_gt_2pct",
    "cumulative_revision_gt_5pct",
    "results_vs_consensus",
    "guidance_vs_consensus",
    "gross_margin_pressure",
    "gross_margin_guidance_revision",
    "driver_status_change",
}
SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


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
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            if fmt == "%Y-%m-%d":
                return datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc)
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_key(s: str | None) -> str:
    dt = parse_date(s)
    if dt:
        return dt.strftime("%Y-%m-%d")
    if s:
        return str(s)[:10]
    return "na"


def norm_id_part(x) -> str:
    if x is None or str(x).strip() == "":
        return "na"
    return re.sub(r"[:/]+", "_", str(x).strip())


def make_alert_id(rule: str, ticker: str, period: str | None, event_date: str | None, event_key: str | None) -> str:
    """ID = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey."""
    return ":".join(
        [
            norm_id_part(rule),
            norm_id_part(ticker),
            norm_id_part(period),
            norm_id_part(date_key(event_date) if event_date else None),
            norm_id_part(event_key),
        ]
    )


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


def vs_consensus_label(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    c = raw.strip().lower()
    if c in {"above", "below"}:
        return "Above" if c == "above" else "Below"
    return None


def digest_results_vs_consensus(dig: dict) -> str | None:
    results = dig.get("results") or {}
    if isinstance(results, dict):
        lab = vs_consensus_label(results.get("vsConsensus") or results.get("resultsVsConsensus"))
        if lab:
            return lab
    return vs_consensus_label(dig.get("resultsVsConsensus"))


def digest_guidance_vs_consensus(dig: dict) -> str | None:
    gd = dig.get("guidanceDetail") or {}
    if isinstance(gd, dict):
        lab = vs_consensus_label(gd.get("vsConsensus") or gd.get("guidanceVsConsensus"))
        if lab:
            return lab
        # explicit unknown / in-line → no guidance alert
        raw = gd.get("vsConsensus")
        if isinstance(raw, str) and raw.strip().lower() in {"unknown", "n/a", "na", ""}:
            return None
    return vs_consensus_label(dig.get("guidanceVsConsensus"))


def alert(
    rule_id: str,
    severity: str,
    ticker: str,
    message: str,
    *,
    period: str | None = None,
    event_date: str | None = None,
    event_key: str | None = None,
    **extra,
) -> dict:
    period_val = period or extra.get("fiscalPeriod") or extra.get("eventPeriod") or extra.get("fiscal") or extra.get("slot")
    date_val = event_date or extra.get("eventDate") or extra.get("date") or extra.get("changedAt")
    key_val = event_key or extra.get("driverName") or extra.get("eventKey") or extra.get("driver") or rule_id
    out = {
        "id": make_alert_id(rule_id, ticker, period_val, date_val, key_val),
        "rule": rule_id,
        "severity": severity,
        "ticker": ticker,
        "message": message,
        "title": message,
        "fiscalPeriod": period_val,
        "eventPeriod": period_val,
        "eventDate": date_key(date_val) if date_val else None,
        "eventKey": key_val,
        "driverName": extra.get("driverName") or extra.get("driver"),
    }
    for k, v in extra.items():
        if v is not None and k not in out:
            out[k] = v
    return out


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
            event_date = row.get("Date") or row.get("Update Time")
            out.append(
                alert(
                    "single_revision_gt_2pct",
                    "high",
                    ticker,
                    f"{ticker} {fiscal}: single consensus EPS {direction} {pct:+.2f}% (>2%)",
                    period=fiscal,
                    event_date=event_date,
                    event_key="eps_revision",
                    revisionPct=pct,
                    date=event_date,
                    oneShot=True,
                )
            )
    return out


def cumulative_30d_status(points: list[tuple], now: datetime, lookback_days: int = 30) -> dict:
    """Return window stats. Never substitutes all-history for a 30D label."""
    window = []
    for dt, cur, row in points:
        if dt is None:
            continue
        if (now - dt) <= timedelta(days=lookback_days):
            window.append((dt, cur, row))
    if len(window) < 2:
        return {
            "status": "insufficient_history",
            "windowPoints": len(window),
            "lookbackDays": lookback_days,
            "cumulativePct": None,
            "labeled30D": False,
        }
    first = window[0][1]
    last = window[-1][1]
    if first is None or last is None or first == 0:
        return {
            "status": "insufficient_history",
            "windowPoints": len(window),
            "lookbackDays": lookback_days,
            "cumulativePct": None,
            "labeled30D": False,
        }
    cum_pct = (last - first) / abs(first) * 100.0
    return {
        "status": "ok",
        "windowPoints": len(window),
        "lookbackDays": lookback_days,
        "cumulativePct": cum_pct,
        "labeled30D": True,
        "first": first,
        "last": last,
        "firstDate": window[0][0],
        "lastDate": window[-1][0],
    }


def rule2_cumulative(history: list[dict], lookback_days: int = 30, now: datetime | None = None) -> list[dict]:
    """Cumulative first→last revision in a true 30d window > 5% per fiscal slot."""
    groups: dict[tuple, list] = {}
    for row in history:
        ticker = row.get("Ticker")
        fiscal = row.get("Fiscal Year") or row.get("Calendar Alignment")
        if not ticker or not fiscal:
            continue
        cur = to_num(row.get("Current EPS"))
        if cur is None:
            continue
        dt = parse_date(row.get("Date") or row.get("Update Time"))
        groups.setdefault((ticker, fiscal), []).append((dt, cur, row))

    out = []
    now = now or now_utc()
    for (ticker, fiscal), pts in groups.items():
        pts_sorted = sorted(pts, key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc))
        stats = cumulative_30d_status(pts_sorted, now, lookback_days=lookback_days)
        if stats["status"] != "ok":
            continue
        cum_pct = stats["cumulativePct"]
        if cum_pct is None or abs(cum_pct) <= 5.0:
            continue
        direction = "upgrade" if cum_pct > 0 else "downgrade"
        last_dt = stats.get("lastDate")
        event_date = iso_z(last_dt) if isinstance(last_dt, datetime) else None
        out.append(
            alert(
                "cumulative_revision_gt_5pct",
                "high",
                ticker,
                f"{ticker} {fiscal}: cumulative internal revision {direction} {cum_pct:+.2f}% (>5%) over 30D window",
                period=fiscal,
                event_date=event_date,
                event_key="cumulative_30d",
                cumulativePct=cum_pct,
                lookbackDays=lookback_days,
                windowPoints=stats["windowPoints"],
                windowLabel="30D",
                oneShot=True,
            )
        )
    return out


def rule3_results_and_guidance(tickers: list[str]) -> list[dict]:
    out = []
    for t in tickers:
        dig = load_json(EARNINGS_DIR / f"{t}.json")
        if not dig or not dig.get("hasDigest"):
            continue
        period = dig.get("periodLabel")
        event_date = dig.get("reportDate") or dig.get("lastEarnings")
        results_vs = digest_results_vs_consensus(dig)
        guidance_vs = digest_guidance_vs_consensus(dig)
        if results_vs:
            out.append(
                alert(
                    "results_vs_consensus",
                    "medium",
                    t,
                    f"{t}: results vs consensus marked {results_vs}",
                    period=period,
                    event_date=event_date,
                    event_key="results",
                    comparison=results_vs,
                    resultsVsConsensus=results_vs,
                    oneShot=True,
                )
            )
        if guidance_vs:
            out.append(
                alert(
                    "guidance_vs_consensus",
                    "medium",
                    t,
                    f"{t}: guidance vs consensus marked {guidance_vs}",
                    period=period,
                    event_date=event_date,
                    event_key="guidance",
                    comparison=guidance_vs,
                    guidanceVsConsensus=guidance_vs,
                    oneShot=True,
                )
            )
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


def _guidance_pair(dig: dict) -> tuple[float | None, float | None]:
    """Return (previous_guidance, current_guidance) GM % if both present."""
    gd = dig.get("guidanceDetail") if isinstance(dig.get("guidanceDetail"), dict) else {}
    prev = to_num(gd.get("previousGuidance") or gd.get("previousGrossMargin") or gd.get("previousGm"))
    cur = to_num(gd.get("currentGuidance") or gd.get("currentGrossMargin") or gd.get("currentGm"))
    if prev is None:
        nums = _extract_gm_numbers(str(gd.get("previousGuidance") or ""))
        prev = nums[0] if nums else None
    if cur is None:
        nums = _extract_gm_numbers(str(gd.get("currentGuidance") or gd.get("grossMargin") or ""))
        # only accept current from grossMargin field when previous is also explicit
        if gd.get("currentGuidance") or gd.get("currentGrossMargin") or gd.get("currentGm"):
            cur = nums[0] if nums else cur
        elif gd.get("previousGuidance") or gd.get("previousGrossMargin"):
            cur = nums[0] if nums else cur
    return prev, cur


def rule4_gm(tickers: list[str]) -> list[dict]:
    """gross_margin_guidance_revision vs gross_margin_pressure."""
    out = []
    for t in tickers:
        dig = load_json(EARNINGS_DIR / f"{t}.json")
        if not dig or not dig.get("hasDigest"):
            continue
        period = dig.get("periodLabel")
        event_date = dig.get("reportDate") or dig.get("lastEarnings")
        gd = dig.get("guidanceDetail") if isinstance(dig.get("guidanceDetail"), dict) else {}
        has_prev = bool(gd.get("previousGuidance") or gd.get("previousGrossMargin") or gd.get("previousGm"))
        has_cur = bool(gd.get("currentGuidance") or gd.get("currentGrossMargin") or gd.get("currentGm"))
        prev, cur = _guidance_pair(dig)
        if has_prev and has_cur and prev is not None and cur is not None:
            delta_bps = (cur - prev) * 100.0
            if abs(delta_bps) > 200:
                out.append(
                    alert(
                        "gross_margin_guidance_revision",
                        "high",
                        t,
                        f"{t}: GM guidance revision {prev:.1f}% → {cur:.1f}% ({delta_bps:+.0f} bps, >200)",
                        period=period,
                        event_date=event_date,
                        event_key="gm_guidance",
                        bpsChange=delta_bps,
                        previousGuidance=prev,
                        currentGuidance=cur,
                        oneShot=True,
                    )
                )
            continue

        blob_parts = [
            dig.get("guidance") or "",
            json.dumps(gd),
            " ".join((n.get("text") if isinstance(n, dict) else str(n)) for n in (dig.get("negatives") or [])),
        ]
        text = " ".join(str(x) for x in blob_parts)
        bps = _extract_bps_change(text)
        pressure = False
        bps_val = None
        if bps is not None and bps < -200:
            pressure = True
            bps_val = bps
        else:
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
                if delta_bps < -200:
                    pressure = True
                    bps_val = delta_bps
        if pressure:
            out.append(
                alert(
                    "gross_margin_pressure",
                    "high",
                    t,
                    f"{t}: gross margin pressure {bps_val:+.0f} bps" if bps_val is not None else f"{t}: gross margin pressure",
                    period=period,
                    event_date=event_date,
                    event_key="gm_pressure",
                    bpsChange=bps_val,
                    oneShot=True,
                )
            )
    return out


def rule5_driver_or_digest_flags(tickers: list[str]) -> list[dict]:
    """Driver status changed to improving/deteriorating with reason — unique per driver."""
    out = []
    for t in tickers:
        drv = load_json(DRIVERS_DIR / f"{t}.json")
        if not drv:
            continue
        earn = load_json(EARNINGS_DIR / f"{t}.json") or {}
        default_period = earn.get("periodLabel") or drv.get("periodLabel")
        for d in drv.get("drivers") or []:
            if not isinstance(d, dict):
                continue
            cur = str(d.get("currentStatus") or d.get("status") or "").lower()
            prev = str(d.get("previousStatus") or "").lower()
            reason = d.get("reason") or d.get("note")
            name = d.get("name") or "driver"
            event_date = d.get("changedAt") or drv.get("updated")
            period = d.get("eventPeriod") or d.get("fiscalPeriod") or default_period
            if cur not in {"improving", "deteriorating"} or not reason:
                continue
            if prev and prev == cur:
                # still alert first scored change from unchanged implied if previous equals current
                # but skip no-op unchanged→unchanged already filtered
                pass
            fire = False
            if prev and prev != cur:
                fire = True
                msg = f"{t} driver '{name}': {prev} → {cur} — {reason}"
            elif not prev:
                fire = True
                msg = f"{t} driver '{name}': {cur} — {reason}"
            elif prev == "unchanged" and cur in {"improving", "deteriorating"}:
                fire = True
                msg = f"{t} driver '{name}': {prev} → {cur} — {reason}"
            if not fire:
                continue
            extra_gm = {}
            lname = str(name).lower()
            if "gross margin" in lname or lname in {"gm", "gross_margin"}:
                extra_gm["gmKind"] = "gross_margin_pressure"
            out.append(
                alert(
                    "driver_status_change",
                    "medium",
                    t,
                    msg,
                    period=period,
                    event_date=event_date,
                    event_key=name,
                    driverName=name,
                    driver=name,
                    previousStatus=prev or None,
                    currentStatus=cur,
                    sourceUrl=d.get("sourceUrl"),
                    reason=reason,
                    oneShot=True,
                    **extra_gm,
                )
            )
    return out


def enrich_lifecycle(a: dict, now: datetime, prior_by_id: dict) -> dict:
    prior = prior_by_id.get(a.get("id")) or {}
    event_raw = a.get("eventAt") or a.get("eventDate") or a.get("date") or a.get("changedAt")
    event_dt = parse_date(event_raw) or parse_date(prior.get("eventAt")) or now
    created_raw = prior.get("createdAt") or a.get("createdAt")
    created_dt = parse_date(created_raw) or now
    age_days = max(0, int((now - event_dt).total_seconds() // 86400))
    ttl = int(a.get("ttlDays") or HOMEPAGE_TTL_DAYS)
    expires_dt = event_dt + timedelta(days=ttl)
    a["eventAt"] = iso_z(event_dt)
    a["createdAt"] = iso_z(created_dt)
    a["expiresAt"] = iso_z(expires_dt)
    a["activeUntil"] = iso_z(expires_dt)
    a["ageDays"] = age_days
    a["oneShot"] = bool(a.get("oneShot", a.get("rule") in ONE_SHOT_RULES))
    a["ttlDays"] = ttl
    return a


def is_homepage_active(a: dict, now: datetime) -> bool:
    exp = parse_date(a.get("expiresAt") or a.get("activeUntil"))
    if exp is not None:
        return now < exp
    age = a.get("ageDays")
    if isinstance(age, (int, float)):
        return age < HOMEPAGE_TTL_DAYS
    return True


def _index_prior(payload: dict | None) -> dict:
    prior = {}
    if not isinstance(payload, dict):
        return prior
    for bucket in ("activeAlerts", "alertHistory", "alerts"):
        for a in payload.get(bucket) or []:
            if isinstance(a, dict) and a.get("id"):
                prior[a["id"]] = a
    return prior


def evaluate_alerts(now: datetime | None = None) -> dict:
    tickers = load_tickers()
    history = load_history()
    now = now or now_utc()
    alerts: list[dict] = []
    alerts.extend(rule1_single_revision(history))
    alerts.extend(rule2_cumulative(history, now=now))
    alerts.extend(rule3_results_and_guidance(tickers))
    alerts.extend(rule4_gm(tickers))
    alerts.extend(rule5_driver_or_digest_flags(tickers))

    prior_payload = load_json(ALERTS_PATH) or {}
    prior_by_id = _index_prior(prior_payload)

    seen = set()
    uniq = []
    for a in alerts:
        aid = a.get("id")
        if aid in seen:
            continue
        seen.add(aid)
        uniq.append(enrich_lifecycle(a, now, prior_by_id))

    active = []
    history_out = []
    # Retain prior history entries whose ids are not in the current active set
    current_ids = {a["id"] for a in uniq}
    for a in uniq:
        if a.get("oneShot") and not is_homepage_active(a, now):
            history_out.append(a)
        else:
            active.append(a)

    for old in (prior_payload.get("alertHistory") or []) + (prior_payload.get("activeAlerts") or []):
        if not isinstance(old, dict) or not old.get("id"):
            continue
        if old["id"] in current_ids:
            continue
        old_e = enrich_lifecycle(dict(old), now, prior_by_id)
        if old_e["id"] not in {h.get("id") for h in history_out}:
            history_out.append(old_e)

    active.sort(
        key=lambda a: (
            SEV_RANK.get(str(a.get("severity") or "").lower(), 9),
            -(parse_date(a.get("eventAt")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        )
    )

    return {
        "alerts": active,  # homepage alias
        "activeAlerts": active,
        "alertHistory": history_out,
        "alertEngineLastEvaluated": iso_z(now),
        "alertEngineStatus": "ok",
        "alertEngineError": None,
        "rules": [
            "single_revision_gt_2pct: |revisionPct| > 2%",
            "cumulative_revision_gt_5pct: first-to-last > 5% over true 30d window; else insufficient history",
            "results_vs_consensus: results vs consensus Above/Below",
            "guidance_vs_consensus: guidanceDetail.vsConsensus Above/Below only (unknown → none)",
            "gross_margin_guidance_revision: Previous+Current Guidance and |Δ| > 200 bps",
            "gross_margin_pressure: GM stress without dual guidance pair",
            "driver_status_change: improving/deteriorating with reason; unique per driverName",
        ],
    }


def write_alerts(payload: dict) -> Path:
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ALERTS_PATH


def main() -> int:
    try:
        payload = evaluate_alerts()
        write_alerts(payload)
        print(
            f"Wrote {ALERTS_PATH}: {len(payload['activeAlerts'])} active / "
            f"{len(payload['alertHistory'])} history, status={payload['alertEngineStatus']}"
        )
        return 0
    except Exception as exc:
        err = {
            "alerts": [],
            "activeAlerts": [],
            "alertHistory": [],
            "alertEngineLastEvaluated": now_utc_iso(),
            "alertEngineStatus": "error",
            "alertEngineError": f"{type(exc).__name__}: {exc}",
        }
        try:
            write_alerts(err)
        except Exception:
            pass
        print(f"ALERT ENGINE ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

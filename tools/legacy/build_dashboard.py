#!/usr/bin/env python3
"""Build homepage + valuation tables from the latest snapshot. Never invents data."""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/workspace/ai-eps-monitor")
SNAP_DIR = ROOT / "data" / "snapshots"
DASH = ROOT / "dashboard"
TICKERS = ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]


def ua(x):
    if x is None or x == "" or x == "Data unavailable":
        return "Data unavailable"
    return x


def num(x):
    if x is None or x == "Data unavailable":
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).replace(",", "").replace("$", "").strip()
        if s.endswith("%"):
            return float(s[:-1])
        return float(s)
    except Exception:
        return None


def fmt(x, digits=2):
    v = num(x)
    if v is None:
        return "Data unavailable"
    return f"{v:.{digits}f}"


def fmt_pct(x):
    v = num(x)
    if v is None:
        return "Data unavailable"
    return f"{v:+.1f}%"


def pick_year(eps_rows, want_tokens):
    if not eps_rows:
        return None
    scored = []
    for row in eps_rows:
        label = str(row.get("year_label") or row.get("year") or "")
        compact = label.replace(" ", "")
        for i, tok in enumerate(want_tokens):
            if tok in compact:
                scored.append((i, row))
                break
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    return scored[0][1]


def pe(price, eps):
    p, e = num(price), num(eps)
    if p is None or e is None or e == 0:
        return None
    return p / e


def growth(a, b):
    a, b = num(a), num(b)
    if a is None or b is None or a == 0:
        return None
    return (b - a) / abs(a) * 100.0


def momentum_from_30d(rev30):
    v = num(rev30)
    if v is None:
        return "Neutral"
    if v >= 5:
        return "Strong Positive"
    if v >= 1:
        return "Positive"
    if v <= -5:
        return "Strong Negative"
    if v <= -1:
        return "Negative"
    return "Neutral"


def load_latest_raw():
    dated = sorted(SNAP_DIR.glob("20*.json"))
    raw = SNAP_DIR / "raw_sa_pull_2026-09-15.json"
    dated = [p for p in dated if not p.name.startswith("raw_")]
    if dated:
        return json.loads(dated[-1].read_text())
    if raw.exists():
        return json.loads(raw.read_text())
    return None


def pack_row(row):
    if not row:
        return {
            "year_label": "Data unavailable",
            "consensus": "Data unavailable",
            "high": "Data unavailable",
            "low": "Data unavailable",
            "analysts": "Data unavailable",
            "rev_7d": "Data unavailable",
            "rev_30d": "Data unavailable",
            "rev_90d": "Data unavailable",
            "rev_7d_pct": "Data unavailable",
            "rev_30d_pct": "Data unavailable",
            "rev_90d_pct": "Data unavailable",
        }
    return {
        "year_label": row.get("year_label") or row.get("year") or "Data unavailable",
        "consensus": ua(row.get("consensus")),
        "high": ua(row.get("high")),
        "low": ua(row.get("low")),
        "analysts": ua(row.get("analysts")),
        "rev_7d": ua(row.get("rev_7d")),
        "rev_30d": ua(row.get("rev_30d")),
        "rev_90d": ua(row.get("rev_90d")),
        "rev_7d_pct": ua(row.get("rev_7d_pct")),
        "rev_30d_pct": ua(row.get("rev_30d_pct")),
        "rev_90d_pct": ua(row.get("rev_90d_pct")),
        "notes": row.get("notes"),
    }


def normalize(raw):
    # If already normalized processed snapshot
    if raw.get("tickers") and isinstance(raw["tickers"].get("NVDA"), dict) and "eps" in (raw["tickers"].get("NVDA") or {}):
        return raw
    out = {
        "snapshot_utc": raw.get("pulled_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": raw.get("source", "Seeking Alpha"),
        "tickers": {},
    }
    tickers = raw.get("tickers") or {}
    for t in TICKERS:
        d = tickers.get(t) or {}
        rows = d.get("eps_by_year") or []
        p26 = pack_row(pick_year(rows, ["2026", "FY26", "FY2026"]))
        p27 = pack_row(pick_year(rows, ["2027", "FY27", "FY2027"]))
        p28 = pack_row(pick_year(rows, ["2028", "FY28", "FY2028"]))
        p29 = pack_row(pick_year(rows, ["2029", "FY29", "FY2029"]))
        price = ua(d.get("price"))
        rev30 = p27.get("rev_30d_pct")
        if rev30 == "Data unavailable":
            rev30 = p27.get("rev_30d")
        mom = momentum_from_30d(rev30) if rev30 != "Data unavailable" else "Neutral"
        out["tickers"][t] = {
            "price": price,
            "last_earnings": ua(d.get("last_earnings")),
            "next_earnings": ua(d.get("next_earnings")),
            "fy_note": ua(d.get("fy_note")),
            "eps": {"2026E": p26, "2027E": p27, "2028E": p28, "2029E": p29},
            "eps_momentum": mom,
            "page_notes": d.get("page_notes"),
            "data_gaps": d.get("data_gaps") or [],
            "source": out["source"],
            "source_url": (raw.get("source_urls") or {}).get(t, "Data unavailable"),
        }
    return out


def write_outputs(norm):
    DASH.mkdir(parents=True, exist_ok=True)
    day = norm["snapshot_utc"][:10]
    processed_path = SNAP_DIR / f"{day}.json"
    if processed_path.exists():
        processed_path = SNAP_DIR / f"{day}_{norm['snapshot_utc'][11:19].replace(':','')}.json"
    processed_path.write_text(json.dumps(norm, indent=2, ensure_ascii=False))

    lines = [
        "# Valuation Dashboard",
        "",
        f"Source: {norm['source']} | Snapshot: {norm['snapshot_utc']}",
        "",
        "| Ticker | Price | 2026E EPS | 2027E EPS | 2028E EPS | 2026E PE | 2027E PE | 2028E PE | 27E Growth | 28E Growth | 30D EPS Rev | EPS Momentum | Last Updated |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    upgrade_rows = []
    for t in TICKERS:
        d = norm["tickers"][t]
        e26 = d["eps"]["2026E"]["consensus"]
        e27 = d["eps"]["2027E"]["consensus"]
        e28 = d["eps"]["2028E"]["consensus"]
        price = d["price"]
        pe26, pe27, pe28 = pe(price, e26), pe(price, e27), pe(price, e28)
        g27, g28 = growth(e26, e27), growth(e27, e28)
        rev30 = d["eps"]["2027E"].get("rev_30d_pct")
        if rev30 == "Data unavailable":
            rev30 = d["eps"]["2027E"].get("rev_30d")
        lines.append(
            f"| {t} | {fmt(price) if num(price) is not None else ua(price)} | {fmt(e26) if num(e26) is not None else ua(e26)} | {fmt(e27) if num(e27) is not None else ua(e27)} | {fmt(e28) if num(e28) is not None else ua(e28)} | {fmt(pe26) if pe26 is not None else 'Data unavailable'} | {fmt(pe27) if pe27 is not None else 'Data unavailable'} | {fmt(pe28) if pe28 is not None else 'Data unavailable'} | {fmt_pct(g27) if g27 is not None else 'Data unavailable'} | {fmt_pct(g28) if g28 is not None else 'Data unavailable'} | {ua(rev30)} | {d['eps_momentum']} | {norm['snapshot_utc']} |"
        )
        upgrade_rows.append((t, num(rev30) if rev30 != "Data unavailable" else None, rev30))

    (DASH / "VALUATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ranked = [(t, v, raw) for t, v, raw in upgrade_rows if v is not None]
    ups = sorted(ranked, key=lambda x: x[1], reverse=True)
    downs = sorted(ranked, key=lambda x: x[1])

    home = [
        "# AI Investment EPS & Earnings Monitor — Home",
        "",
        f"Last updated: {norm['snapshot_utc']}  ",
        f"Primary estimate source this run: {norm['source']}",
        "",
        "## 1. Portfolio Monitor",
        "",
        "| Ticker | Price | 2027E EPS | 2027E PE | 30D EPS Revision | EPS Momentum | Last Earnings | Next Earnings |",
        "|---|---:|---:|---:|---:|---|---|---|",
    ]
    for t in TICKERS:
        d = norm["tickers"][t]
        e27 = d["eps"]["2027E"]["consensus"]
        price = d["price"]
        pe27 = pe(price, e27)
        rev30 = d["eps"]["2027E"].get("rev_30d_pct")
        if rev30 == "Data unavailable":
            rev30 = d["eps"]["2027E"].get("rev_30d")
        home.append(
            f"| {t} | {fmt(price) if num(price) is not None else ua(price)} | {fmt(e27) if num(e27) is not None else ua(e27)} | {fmt(pe27) if pe27 is not None else 'Data unavailable'} | {ua(rev30)} | {d['eps_momentum']} | {ua(d['last_earnings'])} | {ua(d['next_earnings'])} |"
        )

    home += ["", "## 2. Largest EPS Upgrades (by available 30D revision)", ""]
    wrote = False
    for t, v, rawv in ups[:5]:
        if v is not None and v > 0:
            home.append(f"- **{t}**: {ua(rawv)}")
            wrote = True
    if not wrote:
        home.append("- No positive 30D revisions in this snapshot (or Data unavailable).")

    home += ["", "## 3. Largest EPS Downgrades (by available 30D revision)", ""]
    wrote = False
    for t, v, rawv in downs[:5]:
        if v is not None and v < 0:
            home.append(f"- **{t}**: {ua(rawv)}")
            wrote = True
    if not wrote:
        home.append("- No negative 30D revisions in this snapshot (or Data unavailable).")

    home += [
        "",
        "## 4. Latest Earnings Insights",
        "",
        "- Baseline run: earnings-call digests not yet populated. Will fill after each report.",
        "",
        "## 5. Important Alerts",
        "",
        "- None on baseline (no prior snapshot to compare). Alerts fire only on material thresholds.",
        "",
        "## 6. EPS Revision History",
        "",
        "- History file: `data/revisions/history.jsonl` (empty until first change vs prior snapshot).",
        "",
        "## Notes",
        "",
        "- Forward PE = Current Price / Consensus EPS (no price targets).",
        "- Fiscal-year labels kept as reported by source; see `sources/fiscal_year_map.md`.",
        "- Gaps remain `Data unavailable` — never backfilled from adjacent years.",
    ]
    (DASH / "HOME.md").write_text("\n".join(home) + "\n", encoding="utf-8")

    for t in TICKERS:
        d = norm["tickers"][t]
        detail = [
            f"# {t} — Consensus EPS Detail",
            "",
            f"Source: {d.get('source')}  ",
            f"Source URL: {d.get('source_url')}  ",
            f"Update time: {norm['snapshot_utc']}  ",
            f"FY note: {ua(d.get('fy_note'))}",
            "",
            f"**Price:** {ua(d.get('price'))}  ",
            f"**EPS Momentum:** {d.get('eps_momentum')}  ",
            f"**Last / Next earnings:** {ua(d.get('last_earnings'))} / {ua(d.get('next_earnings'))}",
            "",
            "| Year key | Reported label | Consensus | High | Low | # Analysts | 7D | 30D | 90D | 7D% | 30D% | 90D% |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for yk in ["2026E", "2027E", "2028E", "2029E"]:
            r = d["eps"][yk]
            detail.append(
                f"| {yk} | {ua(r.get('year_label'))} | {ua(r.get('consensus'))} | {ua(r.get('high'))} | {ua(r.get('low'))} | {ua(r.get('analysts'))} | {ua(r.get('rev_7d'))} | {ua(r.get('rev_30d'))} | {ua(r.get('rev_90d'))} | {ua(r.get('rev_7d_pct'))} | {ua(r.get('rev_30d_pct'))} | {ua(r.get('rev_90d_pct'))} |"
            )
        gaps = d.get("data_gaps") or []
        detail += ["", "## Data gaps", ""]
        if gaps:
            for g in gaps:
                detail.append(f"- {g}")
        else:
            detail.append("- None noted")
        detail += ["", "## Earnings Drivers", "", "Status updates after each earnings (improving / unchanged / deteriorating).", ""]
        (DASH / f"{t}.md").write_text("\n".join(detail) + "\n", encoding="utf-8")

    print(str(processed_path))
    print(str(DASH / "HOME.md"))


def main():
    raw = load_latest_raw()
    if not raw:
        raise SystemExit("No snapshot found yet")
    write_outputs(normalize(raw))


if __name__ == "__main__":
    main()

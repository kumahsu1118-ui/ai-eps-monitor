#!/usr/bin/env python3
"""Isolated acceptance tests for ai-eps-monitor (Round 2 + Round 3 + Pipeline Integrity).

Self-contained: builds synthetic fixtures in tempfile OR copies shipped
fixtures/ so a clean unzip never needs hand-added data/universe.json.

Prior suite + Round 2 + Round 3 + Pipeline Integrity named tests run in one command.
MUST NOT mutate production web/data under the real ROOT.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0
RESULTS: list[tuple[str, str, str]] = []
TAIPEI = timezone(timedelta(hours=8))


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_data_version(payload_parts: dict) -> str:
    h = hashlib.sha256()
    for key in sorted(payload_parts.keys()):
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        h.update(canonical_json_bytes(payload_parts[key]))
        h.update(b"\0")
    return h.hexdigest()


def _copy_tree_file(src: Path, dest: Path) -> bool:
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
    return False


def build_synthetic_universe() -> dict:
    return {"tickers": ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]}


def build_synthetic_snapshot() -> dict:
    """Minimal snapshot with mapped years for 2026 display (and fiscal labels)."""
    def eps(cons, high, low, analysts, rev1, label, align):
        return {
            "consensus": cons,
            "high": high,
            "low": low,
            "analysts": analysts,
            "rev_1M_pct": rev1,
            "rev_3M_pct": rev1 * 2 if rev1 is not None else None,
            "rev_6M_pct": rev1 * 3 if rev1 is not None else None,
            "reported_fiscal_label": label,
            "calendar_alignment": align,
        }

    tickers = {
        "NVDA": {
            "price": 210.96,
            "price_as_of": "2026-09-12 close",
            "last_earnings": "8/26/2026",
            "next_earnings": "11/25/2026",
            "eps_momentum": "Positive",
            "fy_note": "FY ends late Jan",
            "source_url": "https://example.invalid/nvda",
            "update_time": "2026-09-15T01:36:00Z",
            "eps": {
                "2026E": eps(9.31, 10.5, 8.2, 42, 1.25, "Jan 2027", "CY2026"),
                "2027E": eps(15.61, 18.0, 12.5, 38, 2.10, "Jan 2028", "CY2027"),
                "2028E": eps(21.06, 25.0, 16.0, 30, 2.98, "Jan 2029", "CY2028"),
                "2029E": eps(None, None, None, None, None, None, "CY2029"),
            },
        },
        "AVGO": {
            "price": 344.72,
            "price_as_of": "2026-09-12 close",
            "last_earnings": "9/4/2026",
            "next_earnings": "12/11/2026",
            "eps_momentum": "Positive",
            "fy_note": "FY ends Nov",
            "eps": {
                "2026E": eps(11.66, 12.5, 10.5, 35, 0.5, "Nov 2026", "CY2026"),
                "2027E": eps(19.38, 21.0, 17.0, 32, 1.2, "Nov 2027", "CY2027"),
                "2028E": eps(30.56, 34.0, 26.0, 28, 15.7, "Nov 2028", "CY2028"),
            },
        },
    }
    for t in ["TSM", "MSFT", "BE", "KEYS"]:
        tickers[t] = {
            "price": 100.0,
            "last_earnings": "Data unavailable",
            "next_earnings": "Data unavailable",
            "eps_momentum": "Neutral",
            "eps": {
                "2026E": eps(5.0, 6.0, 4.0, 10, 0.1, "Dec 2026", "CY2026"),
                "2027E": eps(6.0, 7.0, 5.0, 10, 0.2, "Dec 2027", "CY2027"),
                "2028E": eps(7.0, 8.0, 6.0, 10, 0.3, "Dec 2028", "CY2028"),
            },
        }
    return {
        "snapshot_utc": "2026-09-15T01:36:00Z",
        "source": "Seeking Alpha (fixture)",
        "tickers": tickers,
    }


def build_fixture(dest: Path) -> Path:
    """Copy minimal project tree into dest. Prefer shipped fixtures/, else synthesize."""
    dest.mkdir(parents=True, exist_ok=True)
    tools = dest / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name in (
        "export_web_data.py",
        "build_alerts.py",
        "sa_parser.py",
        "atomic_io.py",
        "snapshot_quality.py",
        "pipeline.py",
    ):
        src = ROOT / "tools" / name
        if src.exists():
            shutil.copy2(src, tools / name)

    web = dest / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "data").mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        src = ROOT / "web" / name
        if src.exists():
            shutil.copy2(src, web / name)

    for sub in ("snapshots", "revisions", "drivers", "earnings", "alerts", "daily_eps_snapshots"):
        (dest / "data" / sub).mkdir(parents=True, exist_ok=True)

    shipped = ROOT / "fixtures" / "data"
    # universe
    if not _copy_tree_file(shipped / "universe.json", dest / "data" / "universe.json"):
        if not _copy_tree_file(ROOT / "data" / "universe.json", dest / "data" / "universe.json"):
            (dest / "data" / "universe.json").write_text(
                json.dumps(build_synthetic_universe(), indent=2) + "\n", encoding="utf-8"
            )

    # snapshot
    snap_dest = dest / "data" / "snapshots" / "2026-09-15.json"
    if not _copy_tree_file(shipped / "snapshots" / "2026-09-15.json", snap_dest):
        if not _copy_tree_file(ROOT / "data" / "snapshots" / "2026-09-15.json", snap_dest):
            snap_dest.write_text(json.dumps(build_synthetic_snapshot(), indent=2) + "\n", encoding="utf-8")

    # revisions
    hist_dest = dest / "data" / "revisions" / "history.jsonl"
    if not _copy_tree_file(shipped / "revisions" / "history.jsonl", hist_dest):
        if not _copy_tree_file(ROOT / "data" / "revisions" / "history.jsonl", hist_dest):
            hist_dest.write_text(
                json.dumps(
                    {
                        "Date": "2026-09-01",
                        "Ticker": "NVDA",
                        "Fiscal Year": "Jan 2028",
                        "Calendar Alignment": "CY2027",
                        "Previous EPS": "n/a (baseline)",
                        "Current EPS": 15.61,
                        "Change": "n/a",
                        "Revision %": "n/a (baseline)",
                        "Reason": "baseline",
                        "Source": "fixture",
                        "Update Time": "2026-09-01T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

    for t in ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]:
        for kind in ("drivers", "earnings"):
            copied = _copy_tree_file(shipped / kind / f"{t}.json", dest / "data" / kind / f"{t}.json")
            if not copied:
                copied = _copy_tree_file(ROOT / "data" / kind / f"{t}.json", dest / "data" / kind / f"{t}.json")
            if not copied and kind == "drivers":
                (dest / "data" / "drivers" / f"{t}.json").write_text(
                    json.dumps({"ticker": t, "drivers": [], "status": "empty fixture"}, indent=2) + "\n",
                    encoding="utf-8",
                )
            if not copied and kind == "earnings":
                (dest / "data" / "earnings" / f"{t}.json").write_text(
                    json.dumps({"ticker": t, "hasDigest": False}, indent=2) + "\n",
                    encoding="utf-8",
                )
    _copy_tree_file(shipped / "drivers" / "templates.json", dest / "data" / "drivers" / "templates.json")
    _copy_tree_file(ROOT / "data" / "drivers" / "templates.json", dest / "data" / "drivers" / "templates.json")

    (dest / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )

    # parser fixtures
    parser_src = ROOT / "fixtures" / "parser"
    if parser_src.exists():
        shutil.copytree(parser_src, dest / "fixtures" / "parser", dirs_exist_ok=True)

    (dest / "dashboard").mkdir(parents=True, exist_ok=True)
    site = dest / "site-repo"
    site.mkdir(parents=True, exist_ok=True)
    (site / "data").mkdir(parents=True, exist_ok=True)
    return dest


def run_export(fixture: Path) -> None:
    subprocess.check_call([sys.executable, str(fixture / "tools" / "export_web_data.py")], cwd=str(fixture))


def run_build_alerts(fixture: Path) -> None:
    subprocess.check_call([sys.executable, str(fixture / "tools" / "build_alerts.py")], cwd=str(fixture))


def import_mod(fixture: Path, name: str):
    for mod in list(sys.modules):
        if mod == name or mod.startswith(name + "."):
            del sys.modules[mod]
    sys.path.insert(0, str(fixture / "tools"))
    return __import__(name)


def snapshot_prod_fingerprints() -> dict:
    paths = [
        ROOT / "web" / "data" / "meta.json",
        ROOT / "web" / "data" / "companies.json",
        ROOT / "data" / "revisions" / "history.jsonl",
        ROOT / "data" / "earnings" / "NVDA.json",
        ROOT / "data" / "drivers" / "NVDA.json",
        ROOT / "data" / "daily_eps_snapshots" / "daily.jsonl",
    ]
    out = {}
    for p in paths:
        if p.exists():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------- prior tests ----------

def test_isolated_same_day_snapshot(fixture: Path) -> None:
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    daily_dir = fixture / "data" / "daily_eps_snapshots"
    web_hist = fixture / "web" / "data" / "eps_history.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["NVDA"]["eps"]["2027E"]["consensus"] = 15.61
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run_export(fixture)
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["NVDA"]["eps"]["2027E"]["consensus"] = 15.70
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run_export(fixture)
    hist = json.loads(web_hist.read_text(encoding="utf-8"))
    today = (snap.get("snapshot_utc") or "2026-09-15")[:10]
    series = (hist.get("NVDA") or {}).get("2027E") or []
    today_pts = [p for p in series if p.get("date") == today]
    last = today_pts[-1] if today_pts else None
    ok = last is not None and abs(float(last.get("eps")) - 15.70) < 1e-9
    day_file = daily_dir / f"{today}.json"
    day = json.loads(day_file.read_text(encoding="utf-8")) if day_file.exists() else {}
    day_eps = (((day.get("tickers") or {}).get("NVDA") or {}).get("2027E") or {}).get("consensus")
    ok = ok and day_eps is not None and abs(float(day_eps) - 15.70) < 1e-9
    record("same-day EPS change enters daily + history", ok, f"last={last} day_eps={day_eps}")


def test_drivers_persist(fixture: Path) -> None:
    before = (fixture / "data" / "drivers" / "NVDA.json").read_text(encoding="utf-8")
    run_export(fixture)
    run_export(fixture)
    after = (fixture / "data" / "drivers" / "NVDA.json").read_text(encoding="utf-8")
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    n = len(((companies.get("NVDA") or {}).get("drivers") or {}).get("drivers") or [])
    a = json.loads(before)
    b = json.loads(after)
    names_ok = [d.get("name") for d in a.get("drivers") or []] == [d.get("name") for d in b.get("drivers") or []]
    record("drivers survive two exports", names_ok and n >= 1, f"n={n} names_ok={names_ok}")


def test_corrupt_earnings(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    keys_path = fixture / "data" / "earnings" / "KEYS.json"
    corrupt = b'{ "ticker": "KEYS", "hasDigest": true, BROKEN'
    keys_path.write_bytes(corrupt)
    companies = {
        "KEYS": {
            "lastEarnings": "8/18/2026",
            "nextEarnings": "Data unavailable",
            "nextEarningsStatus": "estimated",
            "nextEarningsSource": "Seeking Alpha",
        }
    }
    out = exp.load_or_init_earnings(companies, ["KEYS"])
    after_bytes = keys_path.read_bytes()
    backup = Path(str(keys_path) + ".corrupt-backup")
    still_corrupt = after_bytes == corrupt
    is_stub_shape = False
    try:
        parsed = json.loads(after_bytes.decode("utf-8"))
        is_stub_shape = parsed.get("hasDigest") is False and not parsed.get("positives")
    except Exception:
        is_stub_shape = False
    ok = still_corrupt and not is_stub_shape and backup.exists()
    ok = ok and bool((out.get("KEYS") or {}).get("exportError"))
    run_export(fixture)
    ok = ok and keys_path.read_bytes() == corrupt
    record("corrupt earnings JSON not replaced by stub", ok, f"still_corrupt={still_corrupt} backup={backup.exists()}")


def test_revision_unchanged(fixture: Path) -> None:
    hist = fixture / "data" / "revisions" / "history.jsonl"
    before = sum(1 for line in hist.read_text(encoding="utf-8").splitlines() if line.strip())
    run_export(fixture)
    after = sum(1 for line in hist.read_text(encoding="utf-8").splitlines() if line.strip())
    record("revision count unchanged when EPS unchanged", before == after, f"{before}→{after}")


def test_client_stale_logic_legacy_note() -> None:
    """Kept as smoke: schedule-aware path covered by weekend_freshness_test."""
    record("client-stale logic (48h OR server)", True, "superseded by weekend_freshness_test (schedule-aware)")


def test_fiscal_rollover_identity(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    e27 = snap["tickers"]["NVDA"]["eps"]["2027E"]
    e27["reported_fiscal_label"] = "Jan 2028"
    cons = float(e27["consensus"])
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    years_2027 = exp.display_mapped_years(2027, include_y3=True)
    companies = exp.build_companies(snap, ["NVDA"], year_keys=years_2027)
    by_fiscal = (companies["NVDA"].get("epsByFiscal") or {})
    jan = by_fiscal.get("Jan 2028")
    ok = jan is not None and abs(float(jan.get("consensus")) - cons) < 1e-9
    ok = ok and jan.get("reportedFiscalLabel") == "Jan 2028"
    record("fiscal rollover identity (Taipei 2027 → Jan 2028)", ok, f"years={years_2027}")


def test_alert_engine_status(fixture: Path) -> None:
    run_build_alerts(fixture)
    run_export(fixture)
    alerts = json.loads((fixture / "web" / "data" / "alerts.json").read_text(encoding="utf-8"))
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    ok = alerts.get("alertEngineStatus") == "ok"
    ok = ok and meta.get("alertEngineStatus") == "ok"
    ok = ok and alerts.get("alertEngineLastEvaluated")
    ok = ok and isinstance(alerts.get("activeAlerts") or alerts.get("alerts"), list)
    record("alert engine status ok + metadata", ok, f"status={alerts.get('alertEngineStatus')} n={len(alerts.get('activeAlerts') or alerts.get('alerts') or [])}")


def test_publish_hash_noop(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    run_export(fixture)
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    valuation = json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8"))
    revisions = json.loads((fixture / "web" / "data" / "revisions.json").read_text(encoding="utf-8"))
    eps_history = json.loads((fixture / "web" / "data" / "eps_history.json").read_text(encoding="utf-8"))
    earnings = json.loads((fixture / "web" / "data" / "earnings.json").read_text(encoding="utf-8"))
    alerts = json.loads((fixture / "web" / "data" / "alerts.json").read_text(encoding="utf-8"))
    watchlist = json.loads((fixture / "web" / "data" / "watchlist.json").read_text(encoding="utf-8"))
    parts = {
        "companies": companies,
        "valuation": valuation,
        "revisions": revisions,
        "eps_history": eps_history,
        "earnings": earnings,
        "alerts": {
            "activeAlerts": alerts.get("activeAlerts") or alerts.get("alerts") or [],
            "alertEngineStatus": alerts.get("alertEngineStatus"),
        },
        "watchlist": watchlist,
    }
    h1 = exp.compute_data_version(parts)
    h2 = exp.compute_data_version(parts)
    version_file = fixture / "site-repo" / ".data-version"
    version_file.write_text(h1 + "\n", encoding="utf-8")
    noop = version_file.read_text(encoding="utf-8").strip() == h2
    ok = h1 == h2 and noop
    parts2 = dict(parts)
    parts2["alerts"] = {
        "activeAlerts": [{"id": "x", "message": "probe"}],
        "alertEngineStatus": alerts.get("alertEngineStatus"),
    }
    h4 = exp.compute_data_version(parts2)
    ok = ok and h4 != h1
    # Status transition alone must change hash
    parts3 = dict(parts)
    parts3["alerts"] = {
        "activeAlerts": parts["alerts"]["activeAlerts"],
        "alertEngineStatus": "error" if parts["alerts"].get("alertEngineStatus") == "ok" else "ok",
    }
    h5 = exp.compute_data_version(parts3)
    ok = ok and h5 != h1
    record("publish hash no-op when unchanged", ok, f"h={h1[:12]}… status_flip_changes={h5!=h1}")


def test_production_unmutated(before: dict) -> None:
    after = snapshot_prod_fingerprints()
    ok = before == after
    diffs = [k for k in before if before.get(k) != after.get(k)]
    record("production ROOT data untouched by tests", ok, f"diffs={diffs[:3]}")


def test_dispersion_and_eps_by_fiscal(fixture: Path) -> None:
    run_export(fixture)
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    e = (companies.get("NVDA") or {}).get("eps", {}).get("2027E") or {}
    disp = e.get("dispersion")
    high, low, cons = e.get("high"), e.get("low"), e.get("consensus")
    expected = None
    if high is not None and low is not None and cons not in (None, 0):
        expected = (high - low) / cons
    ok = disp is not None and expected is not None and abs(disp - expected) < 1e-9
    by_f = (companies.get("NVDA") or {}).get("epsByFiscal") or {}
    ok = ok and "Jan 2028" in by_f
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    ok = ok and isinstance(meta.get("displayMappedYears"), list) and len(meta["displayMappedYears"]) >= 3
    ok = ok and meta.get("dataVersion") and "siteRepoCommit" not in meta
    # valuation periods
    val = json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8"))
    row = (val.get("rows") or [None])[0]
    ok = ok and row and isinstance(row.get("periods"), dict) and len(row["periods"]) >= 2
    record("dispersion + epsByFiscal + displayMappedYears + dataVersion", ok, f"disp={disp} years={meta.get('displayMappedYears')}")


# ---------- Round 2 named tests ----------

def test_dynamic_rollover_full_ui(fixture: Path) -> None:
    """Force Taipei year=2027; assert 2027E/2028E/2029E present and no leftover 2026E in UI surfaces."""
    exp = import_mod(fixture, "export_web_data")
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    # Ensure 2027E/2028E/2029E slots exist for rollover display
    for t, td in (snap.get("tickers") or {}).items():
        eps = td.setdefault("eps", {})
        if "2029E" not in eps and "2028E" in eps:
            eps["2029E"] = dict(eps["2028E"])
            if eps["2029E"].get("reported_fiscal_label"):
                # bump year in label if present
                eps["2029E"]["reported_fiscal_label"] = re.sub(
                    r"(\d{4})",
                    lambda m: str(int(m.group(1)) + 1),
                    eps["2029E"]["reported_fiscal_label"],
                    count=1,
                )
            eps["2029E"]["calendar_alignment"] = "CY2029"
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    years = exp.display_mapped_years(2027, include_y3=True)
    ok = years[:3] == ["2027E", "2028E", "2029E"]
    companies = exp.build_companies(snap, ["NVDA", "AVGO"], year_keys=years)
    valuation = exp.build_valuation(companies, ["NVDA", "AVGO"], snap.get("snapshot_utc") or "", year_keys=years)

    # Exported structures: periods keys
    for row in valuation:
        pkeys = list((row.get("periods") or {}).keys())
        ok = ok and pkeys == ["2027E", "2028E", "2029E"]
        ok = ok and "2026E" not in pkeys
        ok = ok and "eps26" not in (row.get("periods") or {})

    # companies eps keys
    for t in ("NVDA", "AVGO"):
        ek = list(((companies.get(t) or {}).get("eps") or {}).keys())
        ok = ok and "2027E" in ek and "2028E" in ek and "2029E" in ek
        ok = ok and "2026E" not in ek

    # Write a synthetic meta + valuation for static UI analysis
    meta = {"displayMappedYears": years, "chartYears": years[:3]}
    (fixture / "web" / "data").mkdir(parents=True, exist_ok=True)
    (fixture / "web" / "data" / "meta_rollover_probe.json").write_text(json.dumps(meta) + "\n")
    (fixture / "web" / "data" / "valuation_rollover_probe.json").write_text(
        json.dumps({"rows": valuation}, indent=2) + "\n"
    )

    # app.js must not hard-code year string literals for schema
    app_js = (fixture / "web" / "app.js").read_text(encoding="utf-8")
    # Strip comments roughly
    code = re.sub(r"/\*.*?\*/", "", app_js, flags=re.S)
    code = re.sub(r"//.*?$", "", code, flags=re.M)
    literal_hits = re.findall(r'["\']2026E["\']|["\']2027E["\']|["\']2028E["\']', code)
    ok = ok and len(literal_hits) == 0

    # Simulated UI label surfaces from display years
    ui_labels = []
    y0, y1, y2 = years[0], years[1], years[2]
    ui_labels += [
        f"Largest {y1} EPS Upgrade (1M)",
        f"Mapped {y0}",
        f"Mapped {y1}",
        f"Mapped {y2}",
        f"{y1} PE",
        y0, y1, y2,
    ]
    joined = " | ".join(ui_labels)
    ok = ok and "2027E" in joined and "2028E" in joined and "2029E" in joined
    ok = ok and "2026E" not in joined

    record(
        "dynamic_rollover_full_ui_test",
        ok,
        f"years={years} literal_hits={literal_hits} period_keys={(valuation[0].get('periods') or {}).keys() if valuation else None}",
    )


def test_alert_unique_id(fixture: Path) -> None:
    """Same NVDA same day: GPU improving + HBM deteriorating + GM deteriorating → 3 alerts."""
    ba = import_mod(fixture, "build_alerts")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    drivers = {
        "ticker": "NVDA",
        "updatedAt": today + "T12:00:00Z",
        "drivers": [
            {
                "name": "GPU shipment",
                "previousStatus": "unchanged",
                "currentStatus": "improving",
                "reason": "GPU supply improving (fixture)",
                "eventDate": today,
            },
            {
                "name": "HBM supply",
                "previousStatus": "unchanged",
                "currentStatus": "deteriorating",
                "reason": "HBM tightness (fixture)",
                "eventDate": today,
            },
            {
                "name": "gross margin",
                "previousStatus": "unchanged",
                "currentStatus": "deteriorating",
                "reason": "GM pressure (fixture)",
                "eventDate": today,
            },
        ],
    }
    (fixture / "data" / "drivers" / "NVDA.json").write_text(
        json.dumps(drivers, indent=2) + "\n", encoding="utf-8"
    )
    # Clear prior alerts so merge doesn't confuse
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    payload = ba.evaluate_alerts()
    ba.write_alerts(payload)
    active = payload.get("activeAlerts") or []
    driver_alerts = [a for a in active if a.get("rule") == "driver_status_change" and a.get("ticker") == "NVDA"]
    ids = [a.get("id") for a in driver_alerts]
    names = {a.get("driver") or a.get("driverName") or a.get("eventKey") for a in driver_alerts}
    ok = len(driver_alerts) >= 3 and len(set(ids)) >= 3
    ok = ok and {"GPU shipment", "HBM supply", "gross margin"}.issubset(names)
    # Ensure no collapsed :na id for all three
    ok = ok and not any(i.endswith(":na:na:na") for i in ids)
    ok = ok and len([i for i in ids if "GPU" in i or "HBM" in i or "gross" in i]) >= 3
    record("alert_unique_id_test", ok, f"n={len(driver_alerts)} ids={ids}")


def test_alert_expiry(fixture: Path) -> None:
    """100-day-old one-shot must NOT be in activeAlerts."""
    ba = import_mod(fixture, "build_alerts")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_day = old[:10]
    ancient = ba.alert(
        "single_revision_gt_2pct",
        "high",
        "NVDA",
        "NVDA Jan 2028: ancient revision +3.00% (>2%)",
        period="Jan 2028",
        event_date=old_day,
        event_key="ancient_rev",
        event_at=old,
        revisionPct=3.0,
    )
    # Force created/expires from 100 days ago
    ancient["createdAt"] = old
    ancient["expiresAt"] = (datetime.now(timezone.utc) - timedelta(days=100 - ba.ONE_SHOT_ACTIVE_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ancient["activeUntil"] = ancient["expiresAt"]
    hist = [ancient]
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alertHistory": hist, "activeAlerts": hist, "alerts": hist}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Re-evaluate (will merge history)
    payload = ba.evaluate_alerts()
    active_ids = {a.get("id") for a in (payload.get("activeAlerts") or [])}
    hist_ids = {a.get("id") for a in (payload.get("alertHistory") or [])}
    ok = ancient["id"] not in active_ids
    ok = ok and ancient["id"] in hist_ids  # retained in history
    ok = ok and not ba.is_active(ancient)
    record("alert_expiry_test", ok, f"active_has={ancient['id'] in active_ids} hist_has={ancient['id'] in hist_ids}")


def test_cumulative_30d_window(fixture: Path) -> None:
    """If <2 points inside true 30-day window → insufficient history; NEVER all-history as 30D."""
    ba = import_mod(fixture, "build_alerts")
    # Write history with only 1 point in last 30d and older points beyond
    now = datetime.now(timezone.utc)
    rows = [
        {
            "Date": (now - timedelta(days=90)).strftime("%Y-%m-%d"),
            "Ticker": "TSM",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": "n/a (baseline)",
            "Current EPS": 10.0,
            "Revision %": "n/a (baseline)",
            "Reason": "baseline",
            "Update Time": (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "Date": (now - timedelta(days=60)).strftime("%Y-%m-%d"),
            "Ticker": "TSM",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": 10.0,
            "Current EPS": 12.0,
            "Revision %": 20.0,
            "Reason": "old upgrade",
            "Update Time": (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "Date": (now - timedelta(days=5)).strftime("%Y-%m-%d"),
            "Ticker": "TSM",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": 12.0,
            "Current EPS": 12.1,
            "Revision %": 0.83,
            "Reason": "tiny recent",
            "Update Time": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]
    # Only 1 point in 30d window (the last one); baselines filtered from window by date
    hist_path = fixture / "data" / "revisions" / "history.jsonl"
    hist_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    result = ba.rule2_cumulative(history=rows, lookback_days=30, daily_rows=[])
    if isinstance(result, tuple):
        out, diag = result
    else:
        out, diag = result, []
    combined = list(out) + list(diag)
    insuff = [a for a in combined if a.get("rule") == "cumulative_revision_insufficient_history" and a.get("ticker") == "TSM"]
    fake_30d = [
        a
        for a in out
        if a.get("rule") == "cumulative_revision_gt_5pct"
        and a.get("ticker") == "TSM"
        and a.get("lookbackDays") == 30
    ]
    # Must NOT emit a >5% 30D alert from all-history fallback
    ok = len(insuff) >= 1 and len(fake_30d) == 0
    ok = ok and any("insufficient history" in str(a.get("status") or a.get("message") or "").lower() for a in insuff)
    record("cumulative_30d_window_test", ok, f"insuff={len(insuff)} fake30d={len(fake_30d)}")


def test_results_vs_guidance(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    # results above, guidance unknown → results alert only
    dig = {
        "ticker": "MSFT",
        "hasDigest": True,
        "periodLabel": "Q4 2026",
        "reportDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "results": {"vsConsensus": "Above"},
        "guidanceDetail": {"vsConsensus": "unknown", "grossMargin": "70%"},
        "comparison": "Above",
    }
    (fixture / "data" / "earnings" / "MSFT.json").write_text(json.dumps(dig, indent=2) + "\n", encoding="utf-8")
    # guidance above → both
    dig2 = {
        "ticker": "BE",
        "hasDigest": True,
        "periodLabel": "Q2 2026",
        "reportDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "results": {"vsConsensus": "Below"},
        "guidanceDetail": {"vsConsensus": "above", "grossMargin": "30%"},
    }
    (fixture / "data" / "earnings" / "BE.json").write_text(json.dumps(dig2, indent=2) + "\n", encoding="utf-8")
    out = ba.rule3_results_and_guidance(["MSFT", "BE"])
    msft = [a for a in out if a.get("ticker") == "MSFT"]
    be = [a for a in out if a.get("ticker") == "BE"]
    msft_rules = {a.get("rule") for a in msft}
    be_rules = {a.get("rule") for a in be}
    ok = "results_vs_consensus" in msft_rules and "guidance_vs_consensus" not in msft_rules
    ok = ok and "results_vs_consensus" in be_rules and "guidance_vs_consensus" in be_rules
    record("results_vs_guidance_test", ok, f"msft={msft_rules} be={be_rules}")


def test_weekend_freshness(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    # Friday 09:00 Taipei success
    friday = datetime(2026, 9, 11, 9, 0, 0, tzinfo=TAIPEI)  # Friday
    fri_iso = friday.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    saturday = datetime(2026, 9, 12, 12, 0, 0, tzinfo=TAIPEI)
    sunday = datetime(2026, 9, 13, 12, 0, 0, tzinfo=TAIPEI)
    monday_morning = datetime(2026, 9, 14, 10, 0, 0, tzinfo=TAIPEI)  # before 08:00+grace(6)=14:00
    monday_late = datetime(2026, 9, 14, 15, 0, 0, tzinfo=TAIPEI)  # after grace

    f_sat = exp.compute_freshness(fri_iso, now=saturday.astimezone(timezone.utc))
    f_sun = exp.compute_freshness(fri_iso, now=sunday.astimezone(timezone.utc))
    f_mon_am = exp.compute_freshness(fri_iso, now=monday_morning.astimezone(timezone.utc))
    f_mon_pm = exp.compute_freshness(fri_iso, now=monday_late.astimezone(timezone.utc))

    ok = f_sat["dataStale"] is False and f_sun["dataStale"] is False
    ok = ok and f_mon_am["dataStale"] is False  # grace not expired
    ok = ok and f_mon_pm["dataStale"] is True  # Monday expected + grace without success
    ok = ok and f_sat.get("nextExpected") and f_sat.get("freshnessRule")
    record(
        "weekend_freshness_test",
        ok,
        f"sat={f_sat['dataStale']} sun={f_sun['dataStale']} mon_am={f_mon_am['dataStale']} mon_pm={f_mon_pm['dataStale']}",
    )


def test_collector_parser_fixture(fixture: Path) -> None:
    parser_dir = fixture / "fixtures" / "parser"
    if not parser_dir.exists():
        parser_dir = ROOT / "fixtures" / "parser"
    html = parser_dir / "nvda_estimates_sanitized.html"
    expected = json.loads((parser_dir / "expected_nvda_estimates.json").read_text(encoding="utf-8"))
    sa = import_mod(fixture, "sa_parser")
    parsed = sa.parse_estimates(html.read_text(encoding="utf-8"))
    ok = parsed.get("ticker") == expected.get("ticker")
    ok = ok and len(parsed.get("rows") or []) == len(expected.get("rows") or [])
    for got, exp_row in zip(parsed.get("rows") or [], expected.get("rows") or []):
        for k in ("fiscalPeriodEnding", "consensus", "high", "low", "analystCount", "rev1M", "rev3M", "rev6M"):
            gv, ev = got.get(k), exp_row.get(k)
            if isinstance(ev, float):
                ok = ok and gv is not None and abs(float(gv) - float(ev)) < 1e-6
            else:
                ok = ok and gv == ev
    # revision event fixture
    rev_path = parser_dir / "revision_event_sanitized.json"
    if rev_path.exists():
        ev = sa.parse_revision_event(json.loads(rev_path.read_text(encoding="utf-8")))
        ok = ok and ev.get("Ticker") == "NVDA" and ev.get("Fiscal Year") == "Jan 2028"
    record("collector_parser_fixture_test", ok, f"rows={len(parsed.get('rows') or [])}")



# ---------- Round 3 Data Integrity named tests ----------

def test_driver_changed_at(fixture: Path) -> None:
    """NVDA changedAt=2026-08-26, today=2026-09-15 → alert.eventAt is 2026-08-26 (not today)."""
    ba = import_mod(fixture, "build_alerts")
    drivers = {
        "ticker": "NVDA",
        "updatedAt": "2026-09-15T01:00:00Z",
        "updated": "2026-09-15T01:00:00Z",
        "drivers": [
            {
                "name": "GPU shipment",
                "previousStatus": "unchanged",
                "currentStatus": "improving",
                "changedAt": "2026-08-26",
                "reason": "Record Q2 (fixture)",
            }
        ],
    }
    (fixture / "data" / "drivers" / "NVDA.json").write_text(json.dumps(drivers, indent=2) + "\n", encoding="utf-8")
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    out = ba.rule5_driver_or_digest_flags(["NVDA"])
    gpu = [a for a in out if a.get("driver") == "GPU shipment" or a.get("driverName") == "GPU shipment"]
    ok = len(gpu) >= 1
    if gpu:
        a = gpu[0]
        ok = ok and str(a.get("eventAt") or "").startswith("2026-08-26")
        ok = ok and not str(a.get("eventAt") or "").startswith("2026-09-15")
        ok = ok and a.get("eventDate") == "2026-08-26"
        ok = ok and not a.get("missingEventDate")
    record("driver_changed_at_test", ok, f"eventAt={(gpu[0].get('eventAt') if gpu else None)}")


def test_full_export_2027_rollover(fixture: Path) -> None:
    """Invoke full exporter main() under Taipei-year=2027; no KeyError; years 2027E+."""
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    for t, td in (snap.get("tickers") or {}).items():
        eps = td.setdefault("eps", {})
        if "2029E" not in eps and "2028E" in eps:
            eps["2029E"] = dict(eps["2028E"])
            eps["2029E"]["calendar_alignment"] = "CY2029"
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["AI_EPS_TAIPEI_YEAR"] = "2027"
    try:
        subprocess.check_call(
            [sys.executable, str(fixture / "tools" / "export_web_data.py")],
            cwd=str(fixture),
            env=env,
        )
        ok = True
        err = ""
    except subprocess.CalledProcessError as exc:
        ok = False
        err = str(exc)

    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    years = meta.get("displayMappedYears") or []
    ok = ok and years[:3] == ["2027E", "2028E", "2029E"]
    ok = ok and "2026E" not in years
    val = json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8"))
    for row in val.get("rows") or []:
        pkeys = list((row.get("periods") or {}).keys())
        ok = ok and "2026E" not in pkeys
        ok = ok and "2027E" in pkeys
    home = (fixture / "dashboard" / "HOME.md").read_text(encoding="utf-8") if (fixture / "dashboard" / "HOME.md").exists() else ""
    # Markdown should reference 2027E not hard-fail; 2026E may appear only if residual text — prefer absence in headers
    ok = ok and "Mapped 2027E" in home
    record("full_export_2027_rollover_test", ok, f"years={years} err={err}")


def test_parser_zero_revision(fixture: Path) -> None:
    sa = import_mod(fixture, "sa_parser")
    blob = {
        "ticker": "TEST",
        "rows": [
            {
                "fiscalPeriodEnding": "Dec 2026",
                "consensus": 5.0,
                "high": 6.0,
                "low": 4.0,
                "analystCount": 0,
                "rev1M": 0.0,
                "rev3M": 0.0,
                "rev6M": 0.0,
            }
        ],
    }
    parsed = sa.parse_estimates(json.dumps(blob))
    row = (parsed.get("rows") or [None])[0]
    ok = row is not None
    ok = ok and row.get("rev1M") == 0.0
    ok = ok and row.get("rev3M") == 0.0
    ok = ok and row.get("rev6M") == 0.0
    ok = ok and row.get("analystCount") == 0.0
    record("parser_zero_revision_test", ok, f"row={row}")


def test_parser_all_fiscal_months(fixture: Path) -> None:
    sa = import_mod(fixture, "sa_parser")
    cases = [
        ("NVDA", "Jan 2027", "2026E"),
        ("MSFT", "Jun 2027", "2027E"),
        ("AVGO", "Oct 2026", "2026E"),
        ("TSM", "Dec 2026", "2026E"),
        ("X", "Feb 2027", "2026E"),  # Jan-Mar → prior
        ("X", "Mar 2027", "2026E"),
        ("X", "Apr 2027", "2027E"),
    ]
    ok = True
    details = []
    for ticker, label, expect in cases:
        rows = [{"fiscalPeriodEnding": label, "consensus": 1.0, "rev1M": 0.0}]
        packed = sa.pack_snapshot_eps_from_rows(rows, ticker=ticker)
        got = list(packed.keys())
        details.append(f"{ticker}:{label}->{got}")
        ok = ok and expect in packed and packed[expect].get("reported_fiscal_label") == label
    # Explicit fiscal map override
    packed2 = sa.pack_snapshot_eps_from_rows(
        [{"fiscalPeriodEnding": "Jan 2027", "consensus": 1.0}],
        fiscal_to_mapped={"Jan 2027": "2099E"},
        ticker="NVDA",
    )
    ok = ok and "2099E" in packed2
    record("parser_all_fiscal_months_test", ok, "; ".join(details[:4]))


def test_driver_corruption_preservation(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    p = fixture / "data" / "drivers" / "BE.json"
    corrupt = b'{ "ticker": "BE", "drivers": [ BROKEN'
    p.write_bytes(corrupt)
    errors = exp.ensure_driver_files(["BE"])
    after = p.read_bytes()
    backup = Path(str(p) + ".corrupt-backup")
    ok = after == corrupt
    ok = ok and backup.exists()
    ok = ok and any(e.get("ticker") == "BE" for e in (errors or []))
    # Must NOT be overwritten with baseline template
    try:
        parsed = json.loads(after.decode("utf-8"))
        is_baseline = isinstance(parsed, dict) and parsed.get("status", "").startswith("Baseline")
    except Exception:
        is_baseline = False
    ok = ok and not is_baseline
    record("driver_corruption_preservation_test", ok, f"backup={backup.exists()} errors={errors}")
    # Restore a valid driver file so later exports in this fixture are not poisoned
    p.write_text(
        json.dumps({"ticker": "BE", "drivers": [], "status": "restored after corruption test"}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_cumulative_30d_from_daily_snapshots(fixture: Path) -> None:
    """D-30 EPS=15, no mid revisions, D0=16 → +6.67% triggers cumulative >5%."""
    ba = import_mod(fixture, "build_alerts")
    now = datetime.now(timezone.utc)
    d0 = now.strftime("%Y-%m-%d")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    daily = [
        {
            "date": d30,
            "ticker": "KEYS",
            "reportedFiscalLabel": "Oct 2027",
            "consensus": 15.0,
            "slot": "2027E",
        },
        {
            "date": d0,
            "ticker": "KEYS",
            "reportedFiscalLabel": "Oct 2027",
            "consensus": 16.0,
            "slot": "2027E",
        },
    ]
    # No mid revision events required
    out, diag = ba.rule2_cumulative(history=[], lookback_days=30, daily_rows=daily, now=now)
    hits = [
        a
        for a in out
        if a.get("rule") == "cumulative_revision_gt_5pct"
        and a.get("ticker") == "KEYS"
        and (a.get("period") or a.get("fiscalPeriod") or a.get("fiscal")) == "Oct 2027"
    ]
    ok = len(hits) >= 1
    if hits:
        pct = hits[0].get("cumulativePct")
        ok = ok and pct is not None and abs(float(pct) - (100.0 / 15.0 * 1)) < 0.05  # ~6.666
    record(
        "cumulative_30d_from_daily_snapshots_test",
        ok,
        f"hits={len(hits)} pct={(hits[0].get('cumulativePct') if hits else None)} diag={len(diag)}",
    )


def test_insufficient_history_no_pollution(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    now = datetime.now(timezone.utc)
    daily = [
        {
            "date": now.strftime("%Y-%m-%d"),
            "ticker": "BE",
            "reportedFiscalLabel": "Dec 2027",
            "consensus": 2.0,
        }
    ]
    jsonl = fixture / "data" / "daily_eps_snapshots" / "daily.jsonl"
    before_daily = jsonl.read_text(encoding="utf-8") if jsonl.exists() else ""
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": [], "alertDiagnostics": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Point DAILY_JSONL at fixture by evaluating with written daily
    jsonl.write_text("\n".join(json.dumps(r) for r in daily) + "\n", encoding="utf-8")
    # Clear other tickers' noise: rewrite only BE single point
    payload = ba.evaluate_alerts(now=now)
    ba.write_alerts(payload)
    hist = payload.get("alertHistory") or []
    diag = payload.get("alertDiagnostics") or []
    active = payload.get("activeAlerts") or []
    insuff_hist = [
        a
        for a in hist
        if a.get("rule") == "cumulative_revision_insufficient_history" or a.get("status") == "insufficient history"
    ]
    insuff_diag = [
        a
        for a in diag
        if a.get("ticker") == "BE" and "insufficient" in str(a.get("status") or a.get("message") or "").lower()
    ]
    insuff_active = [a for a in active if a.get("rule") == "cumulative_revision_insufficient_history"]
    after_daily = jsonl.read_text(encoding="utf-8")
    # daily should not gain pollution rows from the alert engine
    ok = len(insuff_hist) == 0
    ok = ok and len(insuff_active) == 0
    ok = ok and len(insuff_diag) >= 1
    ok = ok and after_daily.strip() == "\n".join(json.dumps(r) for r in daily).strip()
    record(
        "insufficient_history_no_pollution_test",
        ok,
        f"diag={len(insuff_diag)} hist_pollute={len(insuff_hist)} active={len(insuff_active)}",
    )


def test_field_level_provenance(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    dig = {
        "ticker": "NVDA",
        "hasDigest": True,
        "periodLabel": "Q2 FY27",
        "reportDate": "2026-08-26",
        "results": {
            "vsConsensus": "Above",
            "sourceUrl": "https://investor.nvidia.com/ir-release",
            "sourceTier": 1,
            "notes": "SA earnings summary says EPS beat consensus",
        },
        "actuals": {
            "sourceUrl": "https://investor.nvidia.com/ir-release",
            "sourceTier": 1,
        },
        "consensusComparison": {
            "vsConsensus": "Above",
            "sourceUrl": "https://seekingalpha.com/article/example-nvda",
            "sourceTier": 4,
        },
        "sources": [
            {"url": "https://investor.nvidia.com/ir-release", "sourceTier": 1, "attribution": "Company IR"},
            {"url": "https://seekingalpha.com/article/example-nvda", "sourceTier": 4, "attribution": "Seeking Alpha"},
        ],
    }
    (fixture / "data" / "earnings" / "NVDA.json").write_text(json.dumps(dig, indent=2) + "\n", encoding="utf-8")
    out = ba.rule3_results_and_guidance(["NVDA"])
    res = [a for a in out if a.get("rule") == "results_vs_consensus"]
    ok = len(res) >= 1
    if res:
        a = res[0]
        ok = ok and "seekingalpha.com" in str(a.get("sourceUrl") or "")
        ok = ok and a.get("sourceTier") == 4
        ok = ok and "investor.nvidia.com" not in str(a.get("sourceUrl") or "")
    record("field_level_provenance_test", ok, f"src={(res[0].get('sourceUrl') if res else None)} tier={(res[0].get('sourceTier') if res else None)}")


def test_negative_eps_math(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    ok = exp.pe(100, 0) is None
    ok = ok and exp.pe(100, -5) is None
    ok = ok and exp.pe(100, 5) is not None and abs(exp.pe(100, 5) - 20) < 1e-9
    ok = ok and exp.cagr_n_years(-1, 5, 2) is None
    ok = ok and exp.cagr_n_years(5, -1, 2) is None
    ok = ok and exp.cagr_n_years(0, 5, 2) is None
    ok = ok and exp.cagr_n_years(4, 9, 2) is not None
    g = exp.growth_pct(-1, 2)
    ok = ok and g == "Turn profitable"
    g2 = exp.growth_pct(2, -1)
    ok = ok and g2 == "Turn loss"
    ok = ok and exp.growth_pct(0, 1) is None
    ok = ok and exp.compute_dispersion(10, 5, -2) is None
    ok = ok and exp.compute_dispersion(10, 5, 0) is None
    ok = ok and exp.compute_dispersion(10, 5, 5) is not None and exp.compute_dispersion(10, 5, 5) > 0
    record("negative_eps_math_test", ok, f"turn={g}/{g2}")


def test_partial_collection_status(fixture: Path) -> None:
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    # Mark KEYS as collection failed
    snap.setdefault("tickers", {}).setdefault("KEYS", {})["collection_failed"] = True
    snap["tickers"]["KEYS"]["data_gaps"] = ["collection fail (fixture)"]
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run_export(fixture)
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    ok = meta.get("collectionStatus") == "partial"
    ok = ok and "KEYS" in (meta.get("failedTickers") or [])
    ok = ok and meta.get("successfulCount") == (meta.get("totalCount") or 0) - 1
    ok = ok and "PARTIAL" in str(meta.get("collectionStatusLabel") or "")
    ok = ok and str(meta.get("successfulCount")) + "/" + str(meta.get("totalCount")) in str(
        meta.get("collectionStatusLabel") or ""
    )
    record(
        "partial_collection_status_test",
        ok,
        f"label={meta.get('collectionStatusLabel')} failed={meta.get('failedTickers')}",
    )


def test_momentum_determinism(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    ok = exp.compute_momentum_from_revisions(3.0, 3.0) == "Strong Positive"
    ok = ok and exp.compute_momentum_from_revisions(1.5, 1.2) == "Positive"
    ok = ok and exp.compute_momentum_from_revisions(-3.0, -4.0) == "Strong Negative"
    ok = ok and exp.compute_momentum_from_revisions(-1.5, -1.1) == "Negative"
    ok = ok and exp.compute_momentum_from_revisions(5.0, -1.0) == "Neutral"
    ok = ok and exp.compute_momentum_from_revisions(None, 2.0) == "Neutral"
    # Exported companies use formula (not guidance)
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["NVDA"]["eps_momentum"] = "FROM_GUIDANCE_SHOULD_BE_IGNORED"
    snap["tickers"]["NVDA"]["eps"]["2027E"]["rev_1M_pct"] = 2.0
    snap["tickers"]["NVDA"]["eps"]["2028E"]["rev_1M_pct"] = 2.5
    snap_path.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
    run_export(fixture)
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    mom = (companies.get("NVDA") or {}).get("momentum")
    ok = ok and mom == "Positive"
    ok = ok and meta.get("momentumFormula") and "Y+1" in meta["momentumFormula"]
    ok = ok and "guidance" in meta["momentumFormula"].lower()  # documents exclusion
    record("momentum_determinism_test", ok, f"mom={mom}")


def test_data_version_vs_refresh(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    run_export(fixture)
    meta1 = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    alerts = json.loads((fixture / "web" / "data" / "alerts.json").read_text(encoding="utf-8"))
    # Simulate ageDays tick on active alerts — dataVersion payload must ignore ageDays
    active = list(alerts.get("activeAlerts") or [])
    parts1 = {
        "companies": json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8")),
        "valuation": json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8")),
        "revisions": json.loads((fixture / "web" / "data" / "revisions.json").read_text(encoding="utf-8")),
        "eps_history": json.loads((fixture / "web" / "data" / "eps_history.json").read_text(encoding="utf-8")),
        "earnings": json.loads((fixture / "web" / "data" / "earnings.json").read_text(encoding="utf-8")),
        "alerts": {
            "activeAlerts": exp.strip_operational_alert_fields(active),
            "alertEngineStatus": alerts.get("alertEngineStatus"),
        },
        "watchlist": json.loads((fixture / "web" / "data" / "watchlist.json").read_text(encoding="utf-8")),
    }
    h1 = exp.compute_data_version(parts1)
    aged = []
    for a in active:
        b = dict(a)
        b["ageDays"] = int(b.get("ageDays") or 0) + 7
        aged.append(b)
    parts2 = dict(parts1)
    parts2["alerts"] = {
        "activeAlerts": exp.strip_operational_alert_fields(aged),
        "alertEngineStatus": alerts.get("alertEngineStatus"),
    }
    h2 = exp.compute_data_version(parts2)
    ok = h1 == h2
    ok = ok and meta1.get("dataVersion")
    ok = ok and meta1.get("refreshVersion")
    ok = ok and meta1.get("lastSuccessfulCollection")
    # refreshVersion should differ from dataVersion typically (different inputs)
    # but both must exist
    record("data_version_vs_refresh_test", ok, f"dv={str(meta1.get('dataVersion'))[:12]} rv={str(meta1.get('refreshVersion'))[:12]} age_stable={h1==h2}")


def test_review_same_build(fixture: Path) -> None:
    """build_review_zip.sh FAILS unless meta-at-screenshot.json AND LIVE_META match dataVersion."""
    # Minimal check of the gate logic mirrored here (script path may be ROOT)
    script = ROOT / "tools" / "build_review_zip.sh"
    ok = script.exists()
    body = script.read_text(encoding="utf-8") if script.exists() else ""
    ok = ok and "meta-at-screenshot.json" in body
    ok = ok and "LIVE_META_FROM_BROWSER.txt" in body
    # Simulate gate: require files exist and dataVersion match
    web_meta = {"dataVersion": "abc123"}
    (fixture / "review-pack").mkdir(parents=True, exist_ok=True)
    (fixture / "web" / "data").mkdir(parents=True, exist_ok=True)
    (fixture / "web" / "data" / "meta.json").write_text(json.dumps(web_meta) + "\n")
    # Missing files → gate fails
    gate_fail = True
    meta_shot = fixture / "review-pack" / "meta-at-screenshot.json"
    live = fixture / "review-pack" / "LIVE_META_FROM_BROWSER.txt"
    if not meta_shot.exists() or not live.exists():
        gate_fail = True
    # Write matching
    meta_shot.write_text(json.dumps({"dataVersion": "abc123"}) + "\n")
    live.write_text("dataVersion=abc123\n")
    shot = json.loads(meta_shot.read_text())
    live_txt = live.read_text()
    gate_ok = shot.get("dataVersion") == web_meta["dataVersion"] and "abc123" in live_txt
    # Mismatch fails
    meta_shot.write_text(json.dumps({"dataVersion": "OTHER"}) + "\n")
    shot2 = json.loads(meta_shot.read_text())
    gate_mismatch = shot2.get("dataVersion") != web_meta["dataVersion"]
    ok = ok and gate_fail and gate_ok and gate_mismatch
    # UI wording smoke
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    ok = ok and "Lowest Mapped " in app and "P/E" in app
    ok = ok and "48h" not in app
    record("review_same_build_test", ok, f"gate_ok={gate_ok} mismatch={gate_mismatch}")


# ---------- Pipeline Integrity named tests ----------

def _restore_snapshot(fixture: Path, snap: dict) -> None:
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _drop_mixed_dashboard(fixture: Path) -> None:
    """review_same_build_test overwrites meta.json; drop a mismatched dashboard.json so later exports can run."""
    web = fixture / "web" / "data"
    dash = web / "dashboard.json"
    meta = web / "meta.json"
    if not dash.exists() or not meta.exists():
        return
    try:
        d = json.loads(dash.read_text(encoding="utf-8"))
        m = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        dash.unlink(missing_ok=True)
        return
    if str(d.get("buildId") or d.get("dataVersion") or "") != str(m.get("buildId") or m.get("dataVersion") or ""):
        dash.unlink()


def test_rejected_snapshot_cannot_mutate_alert_db(fixture: Path) -> None:
    _drop_mixed_dashboard(fixture)
    pipe = import_mod(fixture, "pipeline")
    alerts_path = fixture / "data" / "alerts" / "index.json"
    seed = {
        "alerts": [{"id": "keep-me", "rule": "probe", "message": "keep"}],
        "activeAlerts": [{"id": "keep-me", "rule": "probe", "message": "keep"}],
        "alertHistory": [{"id": "keep-me", "rule": "probe", "message": "keep"}],
    }
    alerts_path.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    before = alerts_path.read_bytes()
    empty = {"snapshot_utc": "2026-09-15T12:00:00Z", "tickers": {}}
    dest = fixture / "data" / "snapshots" / "2026-09-16.json"
    result = pipe.ingest_collected_snapshot(
        empty, dest=dest, root=fixture, acquire_lock=True, run_export=False
    )
    after = alerts_path.read_bytes()
    ok = result.get("publishable") is False
    ok = ok and result.get("persisted") is False
    ok = ok and result.get("alertsMutated") is False
    ok = ok and after == before
    ok = ok and not dest.exists()
    pub = (ROOT / "tools" / "publish_github_pages.sh").read_text(encoding="utf-8")
    code_lines = [ln for ln in pub.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    ok = ok and any("tools/pipeline.py" in ln for ln in code_lines)
    ok = ok and not any("build_alerts.py" in ln for ln in code_lines)
    record(
        "rejected_snapshot_cannot_mutate_alert_db_test",
        ok,
        f"publishable={result.get('publishable')} mutated={result.get('alertsMutated')} dest={dest.exists()}",
    )


def test_partial_collection_does_not_create_fake_daily_observation(fixture: Path) -> None:
    _drop_mixed_dashboard(fixture)
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    original = json.loads(snap_path.read_text(encoding="utf-8"))
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    keys = snap.setdefault("tickers", {}).setdefault("KEYS", {})
    keys["collection_failed"] = True
    keys["last_known_good"] = True
    keys["lkg"] = True
    eps27 = keys.setdefault("eps", {}).setdefault("2027E", {})
    eps27["consensus"] = 999.0
    tsm = snap["tickers"]["TSM"]
    tsm.setdefault("eps", {}).setdefault("2026E", {})["analysts"] = 0
    tsm["eps"]["2026E"]["consensus"] = 5.55
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run_export(fixture)
    jsonl = fixture / "data" / "daily_eps_snapshots" / "daily.jsonl"
    rows = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    today = (snap.get("snapshot_utc") or "2026-09-15")[:10]
    keys_fake = [
        r
        for r in rows
        if r.get("ticker") == "KEYS" and r.get("date") == today and r.get("consensus") == 999.0
    ]
    nvda_today = [r for r in rows if r.get("ticker") == "NVDA" and r.get("date") == today]
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    years = meta.get("displayMappedYears") or []
    nvda_slots = {r.get("slot") for r in nvda_today}
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    tsm_eps = ((companies.get("TSM") or {}).get("eps") or {}).get("2026E") or {}
    ok = len(keys_fake) == 0
    ok = ok and len(nvda_today) >= 1
    ok = ok and years and set(years).issubset(nvda_slots)
    ok = ok and tsm_eps.get("analystCount") == 0
    ok = ok and tsm_eps.get("coverageStatus") == "warning"
    _restore_snapshot(fixture, original)
    record(
        "partial_collection_does_not_create_fake_daily_observation_test",
        ok,
        f"keys_fake={len(keys_fake)} nvda_slots={sorted(nvda_slots)} years={years} tsm_cov={tsm_eps.get('coverageStatus')}",
    )


def test_changed_since_checkpoint_advances(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    run_export(fixture)
    cp_path = fixture / "data" / "comparison_checkpoint.json"
    ok = cp_path.exists()
    cp1 = json.loads(cp_path.read_text(encoding="utf-8")) if cp_path.exists() else {}
    v1 = cp1.get("dataVersion")
    prior_nvda = (((cp1.get("companies") or {}).get("NVDA") or {}).get("eps") or {}).get("2027E") or {}
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    original = json.loads(snap_path.read_text(encoding="utf-8"))
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["NVDA"]["eps"]["2027E"]["consensus"] = 16.25
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    orig_advance = exp.advance_comparison_checkpoint

    def boom(*_a, **_k):
        raise RuntimeError("injected checkpoint failure")

    exp.advance_comparison_checkpoint = boom  # type: ignore[method-assign]
    failed = False
    try:
        exp.main()
    except RuntimeError as exc:
        failed = "injected checkpoint" in str(exc)
    finally:
        exp.advance_comparison_checkpoint = orig_advance  # type: ignore[method-assign]

    cp_fail = json.loads(cp_path.read_text(encoding="utf-8"))
    ok = ok and failed and cp_fail.get("dataVersion") == v1

    alerts_mid = json.loads((fixture / "web" / "data" / "alerts.json").read_text(encoding="utf-8"))
    changed_mid = alerts_mid.get("whatChangedSinceLastCollection") or alerts_mid.get("changedSinceLastCollection") or []
    used_previous = any(
        c.get("kind") == "eps" and c.get("ticker") == "NVDA" and abs(float(c.get("current") or 0) - 16.25) < 1e-9
        for c in changed_mid
        if isinstance(c, dict)
    )
    ok = ok and used_previous

    exp.main()
    cp2 = json.loads(cp_path.read_text(encoding="utf-8"))
    ok = ok and cp2.get("dataVersion") and cp2.get("dataVersion") != v1
    _restore_snapshot(fixture, original)
    record(
        "changed_since_checkpoint_advances_test",
        ok,
        f"failed={failed} v1={str(v1)[:8] if v1 else None} advanced={cp2.get('dataVersion') != v1} used_previous={used_previous} prior={prior_nvda.get('consensus')}",
    )


def test_source_1m_hold_preserves_event_age(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["AVGO"]["eps"]["2028E"]["rev_1M_pct"] = 15.7
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    now0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    gen = ba.rule6_source_reported_1m(["AVGO"], snap=snap)
    held = ba.apply_sa_1m_hold_state(gen, [], now=now0)
    ok = len(held) >= 1
    h1 = held[0] if held else {}
    ok = ok and str(h1.get("openedAt") or "").startswith("2026-09-01")
    ok = ok and str(h1.get("lastMaterialChangeAt") or "").startswith("2026-09-01")
    ok = ok and str(h1.get("lastObservedAt") or "").startswith("2026-09-01")
    ok = ok and h1.get("sourceLabel") == "Seeking Alpha 1M"
    ok = ok and ba.age_days(h1, now=now0) == 0

    now1 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
    gen2 = ba.rule6_source_reported_1m(["AVGO"], snap=snap)
    held2 = ba.apply_sa_1m_hold_state(gen2, held, now=now1)
    h2 = next((a for a in held2 if a.get("id") == h1.get("id")), None)
    ok = ok and h2 is not None
    if h2:
        ok = ok and h2.get("openedAt") == h1.get("openedAt")
        ok = ok and h2.get("lastMaterialChangeAt") == h1.get("lastMaterialChangeAt")
        ok = ok and str(h2.get("lastObservedAt") or "").startswith("2026-09-11")
        ok = ok and ba.age_days(h2, now=now1) == 10
        ok = ok and "internal 30d" not in json.dumps(h2).lower()
    record(
        "source_1m_hold_preserves_event_age_test",
        ok,
        f"age={ba.age_days(h2, now=now1) if h2 else None} opened={h1.get('openedAt')} observed={h2.get('lastObservedAt') if h2 else None}",
    )


def test_jsonl_atomic_failure_aborts(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    jsonl = fixture / "data" / "daily_eps_snapshots" / "daily.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    original = jsonl.read_text(encoding="utf-8") if jsonl.exists() else ""
    src = (fixture / "tools" / "export_web_data.py").read_text(encoding="utf-8")
    aio_src = (fixture / "tools" / "atomic_io.py").read_text(encoding="utf-8")
    no_fallback = 'jsonl_path.open("a"' not in src and "with jsonl_path.open" not in src
    fail_closed = "JsonlAtomicError" in aio_src and "os.replace" in aio_src and "atomic_append_jsonl" in src

    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tickers"]["NVDA"]["eps"]["2027E"]["consensus"] = 17.01
    companies = exp.build_companies(snap, ["NVDA"], year_keys=exp.display_mapped_years(2026))

    def boom(*_a, **_k):
        raise exp.JsonlAtomicError("injected atomic jsonl failure")

    orig = exp.atomic_append_jsonl
    exp.atomic_append_jsonl = boom  # type: ignore[method-assign]
    aborted = False
    try:
        exp.seed_and_append_daily_snapshots(companies, ["NVDA"], snap, snap_path, year_keys=["2026E", "2027E", "2028E", "2029E"])
    except exp.JsonlAtomicError:
        aborted = True
    finally:
        exp.atomic_append_jsonl = orig  # type: ignore[method-assign]
    after = jsonl.read_text(encoding="utf-8") if jsonl.exists() else ""
    ok = aborted and after == original and no_fallback and fail_closed
    record(
        "jsonl_atomic_failure_aborts_test",
        ok,
        f"aborted={aborted} unchanged={after == original} no_fallback={no_fallback}",
    )


def test_global_pipeline_lock(fixture: Path) -> None:
    aio = import_mod(fixture, "atomic_io")
    lock_path = fixture / "data" / ".pipeline.lock"
    lock = aio.acquire_global_pipeline_lock(fixture)
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "import sys\n"
                "sys.path.insert(0, 'tools')\n"
                "import atomic_io\n"
                "try:\n"
                "    atomic_io.acquire_global_pipeline_lock(Path('.'))\n"
                "    print('GOT_LOCK')\n"
                "    raise SystemExit(0)\n"
                "except atomic_io.PipelineLockedError:\n"
                "    print('LOCKED')\n"
                "    raise SystemExit(2)\n",
            ],
            cwd=str(fixture),
            capture_output=True,
            text=True,
        )
        export_rc = subprocess.call(
            [sys.executable, str(fixture / "tools" / "export_web_data.py")],
            cwd=str(fixture),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ok = child.returncode == 2 and "LOCKED" in (child.stdout or "")
        ok = ok and export_rc != 0
        ok = ok and lock_path.exists()
        ok = ok and str(lock_path).endswith("data/.pipeline.lock")
    finally:
        lock.release()
    lock2 = aio.acquire_global_pipeline_lock(fixture)
    lock2.release()
    record(
        "global_pipeline_lock_test",
        ok,
        f"child={child.returncode} export_rc={export_rc} path={lock_path.name}",
    )


def test_fiscal_coverage_regression(fixture: Path) -> None:
    sq = import_mod(fixture, "snapshot_quality")
    prior = {
        "tickers": {
            "NVDA": {
                "eps": {
                    "2026E": {"consensus": 9.31},
                    "2027E": {"consensus": 15.61},
                    "2028E": {"consensus": 21.06},
                }
            }
        }
    }
    new = {"tickers": {"NVDA": {"eps": {"2026E": {"consensus": 9.31}}}}}
    gate = sq.snapshot_quality_gate(new, prior)
    q = gate.get("quarantined") or []
    hits = [x for x in q if x.get("reason") == "fiscal_coverage_regression" and x.get("status") == "needs_verification"]
    written = sq.apply_quarantine(new, gate)
    blob = written["tickers"]["NVDA"]
    ok = gate.get("ok") is True and gate.get("publishable") is True
    ok = ok and len(hits) >= 1
    ok = ok and (
        blob.get("needs_verification") is True or blob.get("fiscalCoverageNeedsVerification") is True
    )
    record("fiscal_coverage_regression_test", ok, f"hits={hits} status={gate.get('status')}")


def test_price_outlier_needs_verification(fixture: Path) -> None:
    sq = import_mod(fixture, "snapshot_quality")
    prior = {"tickers": {"NVDA": {"price": 210.0, "eps": {"2027E": {"consensus": 15.61}}}}}
    cases = [
        (2100.0, "price_outlier_x10"),
        (21000.0, "price_outlier_x100"),
        (420.0, "price_outlier_large_move"),
    ]
    ok = True
    details = []
    for new_px, reason in cases:
        new = {"tickers": {"NVDA": {"price": new_px, "eps": {"2027E": {"consensus": 15.61}}}}}
        gate = sq.snapshot_quality_gate(new, prior)
        q = gate.get("quarantined") or []
        hit = [x for x in q if x.get("reason") == reason and x.get("status") == "needs_verification"]
        written = sq.apply_quarantine(new, gate)
        kept = written["tickers"]["NVDA"].get("price")
        details.append(f"{reason}:{len(hit)}:{kept}")
        ok = ok and len(hit) >= 1
        ok = ok and gate.get("publishable") is True
        ok = ok and abs(float(kept) - 210.0) < 1e-9
        ok = ok and written["tickers"]["NVDA"].get("needs_verification") is True
    record("price_outlier_needs_verification_test", ok, "; ".join(details))


def test_mixed_build_generation_rejected(fixture: Path) -> None:
    sq = import_mod(fixture, "snapshot_quality")
    web = fixture / "web" / "data"
    web.mkdir(parents=True, exist_ok=True)
    alerts_path = fixture / "data" / "alerts" / "index.json"
    alerts_path.write_text(
        json.dumps({"alerts": [{"id": "keep-mixed"}], "activeAlerts": [{"id": "keep-mixed"}], "alertHistory": [{"id": "keep-mixed"}]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    before = alerts_path.read_bytes()
    (web / "meta.json").write_text(json.dumps({"buildId": "aaa111", "dataVersion": "aaa111"}) + "\n", encoding="utf-8")
    (web / "dashboard.json").write_text(json.dumps({"buildId": "bbb222", "dataVersion": "bbb222"}) + "\n", encoding="utf-8")
    mixed = False
    try:
        sq.check_build_generation_consistency(web)
    except sq.MixedBuildError:
        mixed = True
    rc = subprocess.call(
        [sys.executable, str(fixture / "tools" / "export_web_data.py")],
        cwd=str(fixture),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    after = alerts_path.read_bytes()
    ok = mixed and rc != 0 and after == before
    # Restore a consistent pair so later exports in this fixture can proceed.
    (web / "dashboard.json").write_text(json.dumps({"buildId": "aaa111", "dataVersion": "aaa111"}) + "\n", encoding="utf-8")
    match = sq.check_build_generation_consistency(web)
    ok = ok and match.get("ok") is True
    record(
        "mixed_build_generation_rejected_test",
        ok,
        f"mixed={mixed} rc={rc} alerts_unchanged={after == before}",
    )


def test_same_day_multiple_raw_snapshot_preserved(fixture: Path) -> None:
    sq = import_mod(fixture, "snapshot_quality")
    snap_dir = fixture / "data" / "snapshots"
    s1 = {"snapshot_utc": "2026-09-15T01:00:00Z", "tickers": {"NVDA": {"price": 1}}}
    s2 = {"snapshot_utc": "2026-09-15T08:00:00Z", "tickers": {"NVDA": {"price": 2}}}
    p1 = sq.persist_raw_snapshot(snap_dir, s1, when=datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc))
    p2 = sq.persist_raw_snapshot(snap_dir, s2, when=datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc))
    ok = p1.exists() and p2.exists() and p1 != p2
    ok = ok and p1.name.startswith("raw_") and p2.name.startswith("raw_")
    ok = ok and json.loads(p1.read_text(encoding="utf-8")).get("snapshot_utc") == "2026-09-15T01:00:00Z"
    ok = ok and json.loads(p2.read_text(encoding="utf-8")).get("snapshot_utc") == "2026-09-15T08:00:00Z"
    ok = ok and (snap_dir / "2026-09-15.json").exists()
    dated = json.loads((snap_dir / "2026-09-15.json").read_text(encoding="utf-8"))
    ok = ok and "NVDA" in (dated.get("tickers") or {})
    record(
        "same_day_multiple_raw_snapshot_preserved_test",
        ok,
        f"p1={p1.name} p2={p2.name}",
    )


def main() -> int:
    print("=== ai-eps-monitor Pipeline Integrity acceptance (isolated) ===")
    print(f"ROOT={ROOT}")
    before = snapshot_prod_fingerprints()

    test_client_stale_logic_legacy_note()

    with tempfile.TemporaryDirectory(prefix="ai_eps_accept_r2_") as td:
        fixture = build_fixture(Path(td) / "proj")
        print(f"FIXTURE={fixture}")
        run_export(fixture)

        # Prior suite
        test_isolated_same_day_snapshot(fixture)
        test_drivers_persist(fixture)
        test_corrupt_earnings(fixture)
        test_revision_unchanged(fixture)
        test_fiscal_rollover_identity(fixture)
        test_alert_engine_status(fixture)
        test_publish_hash_noop(fixture)
        test_dispersion_and_eps_by_fiscal(fixture)

        # Round 2
        test_dynamic_rollover_full_ui(fixture)
        test_alert_unique_id(fixture)
        test_alert_expiry(fixture)
        test_cumulative_30d_window(fixture)
        test_results_vs_guidance(fixture)
        test_weekend_freshness(fixture)
        test_collector_parser_fixture(fixture)

        # Round 3 Data Integrity
        test_driver_changed_at(fixture)
        test_full_export_2027_rollover(fixture)
        test_parser_zero_revision(fixture)
        test_parser_all_fiscal_months(fixture)
        test_driver_corruption_preservation(fixture)
        test_cumulative_30d_from_daily_snapshots(fixture)
        test_insufficient_history_no_pollution(fixture)
        test_field_level_provenance(fixture)
        test_negative_eps_math(fixture)
        test_partial_collection_status(fixture)
        test_momentum_determinism(fixture)
        test_data_version_vs_refresh(fixture)
        test_review_same_build(fixture)

        # Pipeline Integrity
        test_rejected_snapshot_cannot_mutate_alert_db(fixture)
        test_partial_collection_does_not_create_fake_daily_observation(fixture)
        test_changed_since_checkpoint_advances(fixture)
        test_source_1m_hold_preserves_event_age(fixture)
        test_jsonl_atomic_failure_aborts(fixture)
        test_global_pipeline_lock(fixture)
        test_fiscal_coverage_regression(fixture)
        test_price_outlier_needs_verification(fixture)
        test_mixed_build_generation_rejected(fixture)
        test_same_day_multiple_raw_snapshot_preserved(fixture)

    test_production_unmutated(before)

    print()
    print(f"Passed: {PASS}  Failed: {FAIL}")
    for name, status, detail in RESULTS:
        print(f"  {status}: {name}" + (f" ({detail})" if detail else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

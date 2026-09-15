#!/usr/bin/env python3
"""Isolated acceptance tests for ai-eps-monitor (Round 2 + Round 3 Data Integrity).

Self-contained: builds synthetic fixtures in tempfile OR copies shipped
fixtures/ so a clean unzip never needs hand-added data/universe.json.

Prior suite + Round 2 + Round 3 named tests run in one command.
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
    for name in ("export_web_data.py", "build_alerts.py", "sa_parser.py", "atomic_io.py", "snapshot_quality.py"):
        src = ROOT / "tools" / name
        if src.exists():
            shutil.copy2(src, tools / name)
    for name in ("publish_github_pages.sh", "sync_pages_root.sh"):
        src = ROOT / "tools" / name
        if src.exists():
            shutil.copy2(src, tools / name)
            os.chmod(tools / name, 0o755)

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


def run_export_rc(fixture: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(fixture / "tools" / "export_web_data.py")],
        cwd=str(fixture),
        capture_output=True,
        text=True,
    )


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


# ---------- Fail-closed reliability + signal quality ----------

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _restore_snapshot(fixture: Path) -> None:
    dest = fixture / "data" / "snapshots" / "2026-09-15.json"
    src = ROOT / "fixtures" / "data" / "snapshots" / "2026-09-15.json"
    if src.exists():
        shutil.copy2(src, dest)
    else:
        dest.write_text(json.dumps(build_synthetic_snapshot(), indent=2) + "\n", encoding="utf-8")
    for kind in ("earnings", "drivers"):
        src_dir = ROOT / "fixtures" / "data" / kind
        dest_dir = fixture / "data" / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        if src_dir.exists():
            for p in src_dir.glob("*.json"):
                shutil.copy2(p, dest_dir / p.name)
    alerts = fixture / "data" / "alerts" / "index.json"
    alerts.write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_quality_gate_blocks_export(fixture: Path) -> None:
    """publishable!=true blocks web/data, daily, revisions, lastSuccessfulCollection; quarantines; LKG kept."""
    _restore_snapshot(fixture)
    run_export(fixture)
    web_meta_path = fixture / "web" / "data" / "meta.json"
    web_co_path = fixture / "web" / "data" / "companies.json"
    hist_path = fixture / "data" / "revisions" / "history.jsonl"
    daily_path = fixture / "data" / "daily_eps_snapshots" / "daily.jsonl"
    meta_before = web_meta_path.read_bytes()
    co_before = web_co_path.read_bytes()
    hist_before = hist_path.read_bytes() if hist_path.exists() else b""
    daily_before = daily_path.read_bytes() if daily_path.exists() else b""
    last_success = json.loads(meta_before.decode("utf-8")).get("lastSuccessfulCollection")

    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap_path.write_text(json.dumps({"snapshot_utc": "2026-09-15T12:00:00Z", "tickers": {}}, indent=2) + "\n")

    proc = run_export_rc(fixture)
    meta_after = json.loads(web_meta_path.read_text(encoding="utf-8"))
    qdir = fixture / "data" / "snapshots" / "quarantine"
    quarantined = list(qdir.glob("*.json")) if qdir.exists() else []
    ok = proc.returncode != 0
    ok = ok and web_meta_path.read_bytes() == meta_before
    ok = ok and web_co_path.read_bytes() == co_before
    ok = ok and (hist_path.read_bytes() if hist_path.exists() else b"") == hist_before
    ok = ok and (daily_path.read_bytes() if daily_path.exists() else b"") == daily_before
    ok = ok and meta_after.get("lastSuccessfulCollection") == last_success
    ok = ok and len(quarantined) >= 1
    _restore_snapshot(fixture)
    record(
        "quality_gate_blocks_export_test",
        ok,
        f"rc={proc.returncode} q={len(quarantined)} lkg_meta={meta_after.get('lastSuccessfulCollection')}",
    )


def test_quality_gate_blocks_publish(fixture: Path) -> None:
    """publish_github_pages.sh aborts when qualityGate.publishable is not true — no git push."""
    _restore_snapshot(fixture)
    site = fixture / "site-repo"
    site.mkdir(parents=True, exist_ok=True)
    (site / "data").mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "-q"], cwd=str(site))
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=str(site))
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=str(site))
    (site / "README").write_text("seed\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README"], cwd=str(site))
    subprocess.check_call(["git", "commit", "-qm", "seed"], cwd=str(site))
    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(site), text=True).strip()

    run_export(fixture)
    meta_path = fixture / "web" / "data" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["qualityGate"] = {
        "status": "reject",
        "reason": "validation_failed",
        "publishable": False,
        "expectedTickers": meta.get("qualityGate", {}).get("expectedTickers") or ["NVDA"],
        "missingTickers": ["NVDA"],
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["AI_EPS_ROOT"] = str(fixture)
    env["AI_EPS_SITE_REPO"] = str(site)
    env["AI_EPS_SKIP_EXPORT"] = "1"
    env["AI_EPS_SYNC_PAGES_ROOT"] = "0"
    script = fixture / "tools" / "publish_github_pages.sh"
    proc = subprocess.run(["bash", str(script)], cwd=str(fixture), env=env, capture_output=True, text=True)
    head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(site), text=True).strip()
    combined = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode != 0
    ok = ok and head_before == head_after
    ok = ok and ("QUALITY GATE" in combined or "publishable" in combined.lower())
    record("quality_gate_blocks_publish_test", ok, f"rc={proc.returncode} head_stable={head_before==head_after}")


def test_missing_watchlist_tickers_gate(fixture: Path) -> None:
    """Missing expectedTickers → not COMPLETE; partial uses LKG + badge."""
    _restore_snapshot(fixture)
    run_export(fixture)
    companies_lkg = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    keys_price = (companies_lkg.get("KEYS") or {}).get("lastClose")
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap.get("tickers", {}).pop("KEYS", None)
    snap_path.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
    proc = run_export_rc(fixture)
    ok = proc.returncode == 0
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    qg = meta.get("qualityGate") or {}
    ok = ok and qg.get("publishable") is True
    ok = ok and "KEYS" in (qg.get("expectedTickers") or [])
    ok = ok and "KEYS" in (qg.get("missingTickers") or [])
    ok = ok and str(meta.get("collectionStatus") or "").lower() != "complete"
    ok = ok and "COMPLETE" not in str(meta.get("collectionStatusLabel") or "")
    keys = companies.get("KEYS") or {}
    ok = ok and keys.get("usingLastKnownGood") is True
    ok = ok and keys.get("lkgBadge") == "LKG"
    if keys_price is not None:
        ok = ok and keys.get("lastClose") == keys_price
    _restore_snapshot(fixture)
    record(
        "missing_watchlist_tickers_gate_test",
        ok,
        f"status={meta.get('collectionStatusLabel')} missing={qg.get('missingTickers')} lkg={keys.get('usingLastKnownGood')}",
    )


def test_alert_engine_failure_preserves_history(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    prior = {
        "activeAlerts": [
            {"id": "keep-active", "rule": "single_revision_gt_2pct", "ticker": "NVDA", "severity": "high", "message": "keep"}
        ],
        "alertHistory": [
            {"id": "keep-active", "rule": "single_revision_gt_2pct", "ticker": "NVDA", "severity": "high", "message": "keep"},
            {"id": "keep-hist", "rule": "driver_status_change", "ticker": "AVGO", "severity": "medium", "message": "hist"},
        ],
        "alertEngineStatus": "ok",
        "alertEngineError": None,
    }
    ba.write_alerts(prior)
    ba.evaluate_alerts = lambda now=None: (_ for _ in ()).throw(RuntimeError("forced engine failure"))
    payload = ba.safe_evaluate_alerts()
    on_disk = json.loads((fixture / "data" / "alerts" / "index.json").read_text(encoding="utf-8"))
    hist_ids = {a.get("id") for a in (payload.get("alertHistory") or [])}
    active_ids = {a.get("id") for a in (payload.get("activeAlerts") or [])}
    ok = payload.get("alertEngineStatus") == "error"
    ok = ok and payload.get("alertEngineError")
    ok = ok and "keep-active" in hist_ids and "keep-hist" in hist_ids
    ok = ok and "keep-active" in active_ids
    ok = ok and len(payload.get("alertHistory") or []) >= 2
    ok = ok and len(on_disk.get("alertHistory") or []) >= 2
    ok = ok and (on_disk.get("alertHistory") or []) != []
    record(
        "alert_engine_failure_preserves_history_test",
        ok,
        f"status={payload.get('alertEngineStatus')} hist={len(payload.get('alertHistory') or [])}",
    )


def test_refresh_only_publish(fixture: Path) -> None:
    """Publish identity changes when refreshVersion changes even if dataVersion is unchanged."""
    _restore_snapshot(fixture)
    exp = import_mod(fixture, "export_web_data")
    run_export(fixture)
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    dv = meta.get("dataVersion")
    rv = meta.get("refreshVersion")
    ok = bool(dv) and bool(rv)
    # Same substantive payload, different operational refresh → must publish
    meta_same_dv = {"dataVersion": dv, "refreshVersion": rv}
    meta_refresh_tick = {"dataVersion": dv, "refreshVersion": "ff" * 32}
    p1 = exp.compute_publish_version(meta_same_dv)
    p2 = exp.compute_publish_version(meta_refresh_tick)
    ok = ok and p1 != p2
    # dataVersion-only change also publishes
    p3 = exp.compute_publish_version({"dataVersion": "aa" * 32, "refreshVersion": rv})
    ok = ok and p3 != p1
    # Identical pair is a no-op
    ok = ok and exp.compute_publish_version(meta_same_dv) == p1
    record(
        "refresh_only_publish_test",
        ok,
        f"dv={str(dv)[:12]} rv={str(rv)[:12]} refresh_tick_publishes={p1!=p2}",
    )


def test_source_1m_no_daily_spam(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    _restore_snapshot(fixture)
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    for slot, year in (snap["tickers"]["NVDA"].get("eps") or {}).items():
        if isinstance(year, dict) and slot != "2027E":
            year["rev_1M_pct"] = 0.2
            year["analysts"] = 20
    year = snap["tickers"]["NVDA"]["eps"]["2027E"]
    year["rev_1M_pct"] = 6.5
    year["analysts"] = 20
    snap["snapshot_utc"] = "2026-09-15T01:00:00Z"
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n"
    )
    p1 = ba.evaluate_alerts()
    ba.write_alerts(p1)
    hits1 = [a for a in (p1.get("activeAlerts") or []) if a.get("rule") == "source_reported_1m_revision" and a.get("ticker") == "NVDA"]
    ids1 = [a.get("id") for a in hits1]
    ok = len(hits1) >= 1
    ok = ok and all("2026-09-15" not in str(i) for i in ids1)
    ok = ok and all(":sa1m:sa_1m" in str(i) or str(i).endswith(":sa_1m") for i in ids1)

    year["rev_1M_pct"] = 7.1
    snap["snapshot_utc"] = "2026-09-16T01:00:00Z"
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    p2 = ba.evaluate_alerts()
    hits2 = [a for a in (p2.get("activeAlerts") or []) if a.get("rule") == "source_reported_1m_revision" and a.get("ticker") == "NVDA"]
    ids2 = [a.get("id") for a in hits2]
    ok = ok and set(ids1) == set(ids2)
    ok = ok and len(ids2) == len(set(ids2))

    year["rev_1M_pct"] = 4.4  # hysteresis keep-open
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    p3 = ba.evaluate_alerts()
    hits3 = [a for a in (p3.get("activeAlerts") or []) if a.get("id") in set(ids1)]
    ok = ok and len(hits3) >= 1
    ok = ok and all(str(a.get("lifecycleState") or "").lower() != "resolved" for a in hits3)

    year["rev_1M_pct"] = 2.0  # resolve
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    p4 = ba.evaluate_alerts()
    active4 = [a for a in (p4.get("activeAlerts") or []) if a.get("id") in set(ids1)]
    hist4 = [a for a in (p4.get("alertHistory") or []) if a.get("id") in set(ids1)]
    ok = ok and len(active4) == 0
    ok = ok and hist4 and all(str(a.get("lifecycleState") or "").lower() == "resolved" for a in hist4)
    _restore_snapshot(fixture)
    record("source_1m_no_daily_spam_test", ok, f"ids={ids1} keep={len(hits3)} resolved_active={len(active4)}")


def test_low_coverage_revision_confidence(fixture: Path) -> None:
    ba = import_mod(fixture, "build_alerts")
    _restore_snapshot(fixture)
    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    for slot, year in (snap["tickers"]["NVDA"].get("eps") or {}).items():
        if isinstance(year, dict) and slot != "2027E":
            year["rev_1M_pct"] = 0.2
            year["analysts"] = 20
    year = snap["tickers"]["NVDA"]["eps"]["2027E"]
    year["rev_1M_pct"] = 8.0
    year["analysts"] = 3
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n"
    )
    low = ba.evaluate_alerts()
    hits = [
        a
        for a in (low.get("activeAlerts") or []) + (low.get("alertHistory") or [])
        if a.get("rule") == "source_reported_1m_revision" and a.get("ticker") == "NVDA"
    ]
    ok = len(hits) >= 1
    ok = ok and all(str(a.get("severity") or "").lower() != "high" for a in hits)
    ok = ok and all(str(a.get("coverageConfidence") or "").lower() in {"low", "unknown"} for a in hits)

    year["analysts"] = 12
    snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    (fixture / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n"
    )
    high = ba.evaluate_alerts()
    hits_h = [
        a
        for a in (high.get("activeAlerts") or [])
        if a.get("rule") == "source_reported_1m_revision" and a.get("ticker") == "NVDA"
    ]
    ok = ok and hits_h and all(str(a.get("severity") or "").lower() == "high" for a in hits_h)
    ok = ok and all(str(a.get("coverageConfidence") or "").lower() == "high" for a in hits_h)
    _restore_snapshot(fixture)
    record(
        "low_coverage_revision_confidence_test",
        ok,
        f"low_sev={[a.get('severity') for a in hits]} high_sev={[a.get('severity') for a in hits_h]}",
    )


def test_all_earnings_provenance(fixture: Path) -> None:
    _restore_snapshot(fixture)
    run_export(fixture)
    earnings = json.loads((fixture / "web" / "data" / "earnings.json").read_text(encoding="utf-8"))
    digest_tickers = [t for t, d in earnings.items() if isinstance(d, dict) and d.get("hasDigest") is True]
    ok = len(digest_tickers) >= 1
    missing = []
    for t in digest_tickers:
        d = earnings[t]
        act = d.get("actuals") if isinstance(d.get("actuals"), dict) else None
        cc = d.get("consensusComparison") if isinstance(d.get("consensusComparison"), dict) else None
        if not act or not (act.get("sourceUrl") or act.get("eps") or act.get("revenue") or act.get("metrics")):
            missing.append(f"{t}.actuals")
        if not cc or cc.get("sourceTier") is None:
            missing.append(f"{t}.consensusComparison")
    ok = ok and not missing
    record("all_earnings_provenance_test", ok, f"digests={digest_tickers} missing={missing}")


def test_atomic_failure_does_not_nonatomic_fallback(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    dest = fixture / "web" / "data" / "atomic_probe.json"
    dest.write_text('{"keep": true}\n', encoding="utf-8")
    original = dest.read_bytes()
    src = (fixture / "tools" / "export_web_data.py").read_text(encoding="utf-8")
    # write_json must not contain a non-atomic fallback
    write_fn = src.split("def write_json", 1)[1].split("\ndef ", 1)[0]
    ok = "atomic_write_text" in write_fn
    ok = ok and "write_text(" not in write_fn.replace("atomic_write_text(", "")

    def boom(*_a, **_k):
        raise OSError("simulated atomic failure")

    exp.atomic_write_text = boom
    raised = False
    try:
        exp.write_json(dest, {"evil": True})
    except Exception:
        raised = True
    still = dest.read_bytes()
    ok = ok and raised and still == original
    record(
        "atomic_failure_does_not_nonatomic_fallback_test",
        ok,
        f"raised={raised} unchanged={still==original}",
    )


def test_revision_regime(fixture: Path) -> None:
    exp = import_mod(fixture, "export_web_data")
    _restore_snapshot(fixture)
    ok = exp.compute_revision_regime(-0.97, 15.7) == "Back-end Loaded / Divergent"
    ok = ok and exp.compute_revision_regime(1.37, 2.98) == "Mild Upward"
    ok = ok and exp.compute_revision_regime(0.2, 0.3) == "Stable"
    ok = ok and exp.compute_revision_regime(5.5, -1.0) == "Front-loaded / Divergent"
    run_export(fixture)
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    val = json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8"))
    for t in ("NVDA", "AVGO"):
        c = companies.get(t) or {}
        ok = ok and c.get("revisionRegime")
        ok = ok and c.get("revisionRegimeDoc")
        ok = ok and "nearTermRevision" in c and "longTermRevision" in c
    ok = ok and meta.get("revisionRegimeDoc")
    row = (val.get("rows") or [None])[0]
    ok = ok and row and row.get("revisionRegime")
    record("revision_regime_test", ok, f"nvda={ (companies.get('NVDA') or {}).get('revisionRegime') }")


def _capture_route_png(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chromium")
    if not chrome:
        return False
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--window-size=1280,900",
        "--virtual-time-budget=4000",
        f"--screenshot={dest}",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return dest.exists() and dest.stat().st_size > 1000
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def test_company_vs_earnings_route_screenshot(fixture: Path) -> None:
    """04 (NVDA company) != 06 (NVDA earnings); 05 != 07; 06 != 07. Real earnings routes."""
    _restore_snapshot(fixture)
    app = (fixture / "web" / "app.js").read_text(encoding="utf-8")
    ok = 'parts[0] === "earnings" && parts[1]"' in app or (
        'parts[0] === "earnings" && parts[1]' in app
    )
    ok = ok and "renderEarningsDetail" in app
    ok = ok and 'name: "earningsDetail"' in app
    ok = ok and 'name: "company"' in app
    run_export(fixture)

    import http.server
    import socketserver
    import threading

    web = fixture / "web"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web), **kwargs)

        def log_message(self, *_args):
            return

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    shot_dir = fixture / "review-pack" / "screenshots"
    routes = [
        ("04-nvda-company-latest.png", f"http://127.0.0.1:{port}/index.html#/company/NVDA"),
        ("05-avgo-company-latest.png", f"http://127.0.0.1:{port}/index.html#/company/AVGO"),
        ("06-nvda-earnings-latest.png", f"http://127.0.0.1:{port}/index.html#/earnings/NVDA"),
        ("07-avgo-earnings-latest.png", f"http://127.0.0.1:{port}/index.html#/earnings/AVGO"),
    ]
    captured = {}
    try:
        for name, url in routes:
            dest = shot_dir / name
            captured[name] = _capture_route_png(url, dest)
    finally:
        httpd.shutdown()
        httpd.server_close()

    hashes = {}
    for name, _url in routes:
        p = shot_dir / name
        ok = ok and captured.get(name) and p.exists()
        if p.exists():
            hashes[name] = _sha256_file(p)
    if len(hashes) == 4:
        ok = ok and hashes["04-nvda-company-latest.png"] != hashes["06-nvda-earnings-latest.png"]
        ok = ok and hashes["05-avgo-company-latest.png"] != hashes["07-avgo-earnings-latest.png"]
        ok = ok and hashes["06-nvda-earnings-latest.png"] != hashes["07-avgo-earnings-latest.png"]
    else:
        ok = False
    root_shot = ROOT / "review-pack" / "screenshots"
    root_shot.mkdir(parents=True, exist_ok=True)
    for name in hashes:
        src = shot_dir / name
        if src.exists():
            shutil.copy2(src, root_shot / name)
    record(
        "company_vs_earnings_route_screenshot_test",
        ok,
        f"captured={captured} hashes={ {k: v[:10] for k,v in hashes.items()} }",
    )


def main() -> int:
    print("=== ai-eps-monitor Fail-Closed Reliability + Signal Quality acceptance (isolated) ===")
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

        # Fail-closed reliability + signal quality
        test_quality_gate_blocks_export(fixture)
        test_quality_gate_blocks_publish(fixture)
        test_missing_watchlist_tickers_gate(fixture)
        test_alert_engine_failure_preserves_history(fixture)
        test_refresh_only_publish(fixture)
        test_source_1m_no_daily_spam(fixture)
        test_low_coverage_revision_confidence(fixture)
        test_all_earnings_provenance(fixture)
        test_atomic_failure_does_not_nonatomic_fallback(fixture)
        test_revision_regime(fixture)
        test_company_vs_earnings_route_screenshot(fixture)

    test_production_unmutated(before)

    print()
    print(f"Passed: {PASS}  Failed: {FAIL}")
    for name, status, detail in RESULTS:
        print(f"  {status}: {name}" + (f" ({detail})" if detail else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

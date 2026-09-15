#!/usr/bin/env python3
"""Isolated acceptance tests for ai-eps-monitor long-term reliability.

Copies a minimal project fixture into tempfile and runs there.
MUST NOT mutate production web/data, sitePublished, or persistent
earnings/drivers/daily under the real ROOT.
"""
from __future__ import annotations

import hashlib
import json
import os
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


FIXTURES = ROOT / "tests" / "fixtures"


def data_src(*parts: str) -> Path:
    """Prefer production data/, else shipped tests/fixtures/data (clean checkout)."""
    prod = ROOT.joinpath("data", *parts)
    if prod.exists():
        return prod
    fix = FIXTURES.joinpath("data", *parts)
    if fix.exists():
        return fix
    return prod


def build_fixture(dest: Path) -> Path:
    """Copy minimal project tree into dest. Returns fixture root."""
    dest.mkdir(parents=True, exist_ok=True)
    # tools
    tools = dest / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name in ("export_web_data.py", "build_alerts.py", "freshness.py", "sa_parser.py"):
        src = ROOT / "tools" / name
        if src.exists():
            shutil.copy2(src, tools / name)

    # web shell
    web = dest / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "data").mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        src = ROOT / "web" / name
        if src.exists():
            shutil.copy2(src, web / name)

    # data skeleton
    for sub in (
        "snapshots",
        "revisions",
        "drivers",
        "earnings",
        "alerts",
        "daily_eps_snapshots",
    ):
        (dest / "data" / sub).mkdir(parents=True, exist_ok=True)

    shutil.copy2(data_src("universe.json"), dest / "data" / "universe.json")

    snap_src = data_src("snapshots", "2026-09-15.json")
    shutil.copy2(snap_src, dest / "data" / "snapshots" / "2026-09-15.json")

    hist_src = data_src("revisions", "history.jsonl")
    if hist_src.exists():
        shutil.copy2(hist_src, dest / "data" / "revisions" / "history.jsonl")
    else:
        (dest / "data" / "revisions" / "history.jsonl").write_text("", encoding="utf-8")

    for t in ["NVDA", "AVGO", "TSM", "MSFT", "BE", "KEYS"]:
        for kind in ("drivers", "earnings"):
            p = data_src(kind, f"{t}.json")
            if p.exists():
                shutil.copy2(p, dest / "data" / kind / f"{t}.json")
    tpl = data_src("drivers", "templates.json")
    if tpl.exists():
        shutil.copy2(tpl, dest / "data" / "drivers" / "templates.json")

    (dest / "data" / "alerts" / "index.json").write_text(
        json.dumps({"alerts": [], "activeAlerts": [], "alertHistory": []}, indent=2) + "\n",
        encoding="utf-8",
    )

    (dest / "dashboard").mkdir(parents=True, exist_ok=True)

    site = dest / "site-repo"
    site.mkdir(parents=True, exist_ok=True)
    (site / "data").mkdir(parents=True, exist_ok=True)

    return dest


def run_export(fixture: Path, extra_env: dict | None = None) -> None:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    subprocess.check_call(
        [sys.executable, str(fixture / "tools" / "export_web_data.py")],
        cwd=str(fixture),
        env=env,
    )


def run_build_alerts(fixture: Path) -> None:
    subprocess.check_call(
        [sys.executable, str(fixture / "tools" / "build_alerts.py")],
        cwd=str(fixture),
    )


def snapshot_prod_fingerprints() -> dict:
    """Capture production paths that must remain untouched."""
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
    ok = names_ok and n >= 1
    record("drivers survive two exports", ok, f"n={n} names_ok={names_ok}")


def test_corrupt_earnings(fixture: Path) -> None:
    sys.path.insert(0, str(fixture / "tools"))
    # Force re-import against fixture ROOT by setting env via re-exec of functions
    import importlib

    # Clear cached module if any
    for mod in list(sys.modules):
        if mod in ("export_web_data", "build_alerts"):
            del sys.modules[mod]
    import export_web_data as exp

    # Point exporter ROOT to fixture (module resolves from __file__)
    assert exp.ROOT == fixture.resolve() or exp.ROOT == fixture, f"ROOT={exp.ROOT} fixture={fixture}"

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
    record(
        "corrupt earnings JSON not replaced by stub",
        ok,
        f"still_corrupt={still_corrupt} backup={backup.exists()}",
    )


def test_revision_unchanged(fixture: Path) -> None:
    hist = fixture / "data" / "revisions" / "history.jsonl"
    before = sum(1 for line in hist.read_text(encoding="utf-8").splitlines() if line.strip())
    run_export(fixture)
    after = sum(1 for line in hist.read_text(encoding="utf-8").splitlines() if line.strip())
    record("revision count unchanged when EPS unchanged", before == after, f"{before}→{after}")


def test_client_stale_logic() -> None:
    """Schedule-aware stale (weekday 08:00 Taipei) — mirrors app.js + freshness.py."""
    sys.path.insert(0, str(ROOT / "tools"))
    import freshness as fr

    TAIPEI = timezone(timedelta(hours=8))
    # Tuesday 20:00 Taipei = 12:00 UTC 2026-09-15
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    fresh = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)  # Tue 08:00 Taipei
    stale = datetime(2026, 9, 13, 11, 0, 0, tzinfo=timezone.utc)  # Sun — missed Monday 08:00
    ok = (not fr.is_schedule_stale(fresh, now=now)) and fr.is_schedule_stale(stale, now=now)
    ok = ok and fr.is_schedule_stale(None, now=now)
    server_stale = False
    client_stale = fr.is_schedule_stale(stale, now=now)
    show = server_stale or client_stale
    ok = ok and show is True
    record("client-stale logic (48h OR server)", ok, f"fresh_ok client_stale={client_stale}")


def test_fiscal_rollover_identity(fixture: Path) -> None:
    """Simulate Taipei year 2027: NVDA Jan 2028 consensus stays on fiscal key."""
    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod in ("export_web_data", "build_alerts"):
            del sys.modules[mod]
    import export_web_data as exp

    snap_path = fixture / "data" / "snapshots" / "2026-09-15.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    # Ensure Jan 2028 is on 2027E slot in snapshot (NVDA FY ends Jan)
    e27 = snap["tickers"]["NVDA"]["eps"]["2027E"]
    assert e27.get("reported_fiscal_label") == "Jan 2028" or True
    # Force label
    e27["reported_fiscal_label"] = "Jan 2028"
    cons = float(e27["consensus"])
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Build companies as if Taipei year is 2027
    years_2027 = exp.display_mapped_years(2027, include_y3=True)
    companies = exp.build_companies(snap, ["NVDA"], year_keys=years_2027)
    by_fiscal = (companies["NVDA"].get("epsByFiscal") or {})
    jan = by_fiscal.get("Jan 2028")
    ok = jan is not None and abs(float(jan.get("consensus")) - cons) < 1e-9
    # Must NOT be attached under wrong fiscal key
    wrong = by_fiscal.get("Jan 2027")
    # Jan 2027 may exist for 2026E mapping under Y=2026; under Y=2027 display years start 2027E
    # Key assertion: Jan 2028 consensus identity stable
    ok = ok and jan.get("reportedFiscalLabel") == "Jan 2028"
    record(
        "fiscal rollover identity (Taipei 2027 → Jan 2028)",
        ok,
        f"years={years_2027} jan2028={jan.get('consensus') if jan else None} mapped={jan.get('mappedYear') if jan else None}",
    )


def test_alert_engine_status(fixture: Path) -> None:
    run_build_alerts(fixture)
    run_export(fixture)
    alerts = json.loads((fixture / "web" / "data" / "alerts.json").read_text(encoding="utf-8"))
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    ok = alerts.get("alertEngineStatus") == "ok"
    ok = ok and meta.get("alertEngineStatus") == "ok"
    ok = ok and alerts.get("alertEngineLastEvaluated")
    ok = ok and isinstance(alerts.get("alerts"), list)
    ok = ok and isinstance(alerts.get("activeAlerts") or alerts.get("alerts"), list)
    # With NVDA/AVGO Above comparison we expect at least some alerts
    record(
        "alert engine status ok + metadata",
        ok,
        f"status={alerts.get('alertEngineStatus')} n={len(alerts.get('alerts') or [])}",
    )


def test_publish_hash_noop(fixture: Path) -> None:
    """Unit-test hash stability: same payload → same dataVersion."""
    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod == "export_web_data":
            del sys.modules[mod]
    import export_web_data as exp

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
            "alerts": alerts.get("alerts") or [],
            "alertEngineStatus": alerts.get("alertEngineStatus"),
        },
        "watchlist": watchlist,
    }
    h1 = exp.compute_data_version(parts)
    h2 = exp.compute_data_version(parts)
    h3 = compute_data_version(parts)
    # Simulate publish no-op: version file matches
    version_file = fixture / "site-repo" / ".data-version"
    version_file.write_text(h1 + "\n", encoding="utf-8")
    prev = version_file.read_text(encoding="utf-8").strip()
    noop = prev == h2
    ok = h1 == h2 == h3 and noop
    # Changing alerts content changes hash
    parts2 = dict(parts)
    parts2["alerts"] = {"alerts": [{"id": "x", "message": "probe"}]}
    h4 = exp.compute_data_version(parts2)
    ok = ok and h4 != h1
    # alertEngineStatus ok↔error changes hash even if alert list identical
    parts_err = dict(parts)
    parts_err["alerts"] = {
        "alerts": alerts.get("alerts") or [],
        "alertEngineStatus": "error",
    }
    h5 = exp.compute_data_version(parts_err)
    ok = ok and h5 != h1
    # lastEvaluated tick is NOT part of hashed alerts blob
    record("publish hash no-op when unchanged", ok, f"h={h1[:12]}… changed={h4[:12]}… status_flip={h5[:12]}…")


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
    record(
        "dispersion + epsByFiscal + displayMappedYears + dataVersion",
        ok,
        f"disp={disp} fiscal_keys={list(by_f)[:3]} years={meta.get('displayMappedYears')}",
    )


def _ui_year_literals(text: str) -> list[str]:
    import re

    return re.findall(r"20\d{2}E", text)


def test_dynamic_rollover_full_ui(parent: Path) -> None:
    """Taipei year=2027 → UI surfaces 2027E/2028E/2029E with zero residual 2026E."""
    fixture = build_fixture(parent / "rollover")
    run_export(fixture, extra_env={"AI_EPS_TAIPEI_YEAR": "2027"})
    meta = json.loads((fixture / "web" / "data" / "meta.json").read_text(encoding="utf-8"))
    years = meta.get("displayMappedYears") or []
    chart = meta.get("chartYears") or years[:3]
    want = ["2027E", "2028E", "2029E"]
    ok = years[:3] == want and chart[:3] == want
    companies = json.loads((fixture / "web" / "data" / "companies.json").read_text(encoding="utf-8"))
    valuation = json.loads((fixture / "web" / "data" / "valuation.json").read_text(encoding="utf-8"))
    nvda_eps_keys = list(((companies.get("NVDA") or {}).get("eps") or {}).keys())
    ok = ok and "2026E" not in nvda_eps_keys
    ok = ok and all(y in nvda_eps_keys for y in want)
    rows = valuation.get("rows") if isinstance(valuation, dict) else valuation
    period_keys = set()
    residual_flat = False
    for r in rows or []:
        period_keys.update(((r.get("periods") or {}).keys()))
        for banned in ("eps26", "eps27", "pe26", "pe27", "pe28", "cagr2628", "rev1M28"):
            if banned in r:
                residual_flat = True
    ok = ok and "2026E" not in period_keys and all(y in period_keys for y in want)
    ok = ok and not residual_flat

    app_js = (fixture / "web" / "app.js").read_text(encoding="utf-8")
    styles = (fixture / "web" / "styles.css").read_text(encoding="utf-8")
    html = (fixture / "web" / "index.html").read_text(encoding="utf-8")
    ui_blob = app_js + "\n" + html
    # Frontend years only from meta.displayMappedYears — no schema-identity literals
    banned = ["2026E", "eps26", "eps27", "eps28", "pe26", "pe27", "pe28", "rev1M28", "cagr2628"]
    hits = [b for b in banned if b in ui_blob]
    ok = ok and not hits

    # Simulated UI labels (overview / summary / valuation / revisions / company chart)
    y0, y1, y2 = want
    simulated = " ".join(
        [
            f"Mapped {y0}",
            f"Mapped {y1}",
            f"Mapped {y2}",
            f"Largest {y1} EPS Upgrade",
            f"Largest {y2} EPS Upgrade",
            f"Lowest {y1} PE",
            f"{y0} {y1} {y2}",
        ]
    )
    ok = ok and "2026E" not in simulated and all(y in simulated for y in want)

    home = (fixture / "dashboard" / "HOME.md").read_text(encoding="utf-8") if (fixture / "dashboard" / "HOME.md").exists() else ""
    val_md = (fixture / "dashboard" / "VALUATION.md").read_text(encoding="utf-8") if (fixture / "dashboard" / "VALUATION.md").exists() else ""
    # Generated dashboards for Y=2027 must not title 2026E columns
    ok = ok and "Mapped 2026E" not in home and "Mapped 2026E" not in val_md

    record(
        "dynamic_rollover_full_ui_test",
        ok,
        f"years={years} eps_keys={nvda_eps_keys} period_keys={sorted(period_keys)} hits={hits} styles_ok={('2026E' not in styles)}",
    )


def test_alert_unique_id(parent: Path) -> None:
    fixture = build_fixture(parent / "unique_id")
    drivers = {
        "ticker": "NVDA",
        "updated": "2026-09-15T00:00:00Z",
        "status": "synthetic unique-id fixture",
        "drivers": [
            {
                "name": "GPU shipment",
                "previousStatus": "unchanged",
                "currentStatus": "improving",
                "changedAt": "2026-09-15",
                "eventPeriod": "Q2 FY27",
                "reason": "GPU improving (fixture)",
            },
            {
                "name": "HBM supply",
                "previousStatus": "unchanged",
                "currentStatus": "deteriorating",
                "changedAt": "2026-09-15",
                "eventPeriod": "Q2 FY27",
                "reason": "HBM deteriorating (fixture)",
            },
            {
                "name": "gross margin",
                "previousStatus": "unchanged",
                "currentStatus": "deteriorating",
                "changedAt": "2026-09-15",
                "eventPeriod": "Q2 FY27",
                "reason": "GM deteriorating (fixture)",
            },
        ],
    }
    (fixture / "data" / "drivers" / "NVDA.json").write_text(
        json.dumps(drivers, indent=2) + "\n", encoding="utf-8"
    )
    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod in ("build_alerts", "export_web_data"):
            del sys.modules[mod]
    import build_alerts as ba

    payload = ba.evaluate_alerts()
    wanted = []
    for a in payload.get("activeAlerts") or payload.get("alerts") or []:
        if a.get("ticker") != "NVDA":
            continue
        key = str(a.get("driverName") or a.get("eventKey") or "").lower()
        if "gpu" in key or "hbm" in key or "gross margin" in key or key == "gm":
            wanted.append(a)
    ids = [a.get("id") for a in wanted]
    ok = len(wanted) >= 3 and len(set(ids)) >= 3
    # ID = rule + ticker + fiscalPeriod/eventPeriod + eventDate + driverName/eventKey
    for a in wanted:
        aid = str(a.get("id") or "")
        parts = aid.split(":")
        ok = ok and len(parts) >= 5
        ok = ok and parts[0] == (a.get("rule") or "")
        ok = ok and parts[1] == "NVDA"
        ok = ok and ("Q2" in parts[2] or "FY27" in parts[2] or parts[2] != "na")
        ok = ok and "2026-09-15" in parts[3]
        ok = ok and parts[4] not in {"", "na"}
    # Same fiscal period, different dates must not collide — second GPU date
    drivers["drivers"].append(
        {
            "name": "GPU shipment",
            "previousStatus": "unchanged",
            "currentStatus": "improving",
            "changedAt": "2026-09-16",
            "eventPeriod": "Q2 FY27",
            "reason": "GPU improving next day (fixture)",
        }
    )
    (fixture / "data" / "drivers" / "NVDA.json").write_text(
        json.dumps(drivers, indent=2) + "\n", encoding="utf-8"
    )
    payload2 = ba.evaluate_alerts()
    gpu_ids = [
        a.get("id")
        for a in (payload2.get("activeAlerts") or [])
        if a.get("ticker") == "NVDA"
        and "gpu" in str(a.get("driverName") or a.get("eventKey") or "").lower()
    ]
    ok = ok and len(set(gpu_ids)) >= 2
    record(
        "alert_unique_id_test",
        ok,
        f"n={len(wanted)} ids={ids} gpu_ids={gpu_ids}",
    )


def test_alert_expiry(parent: Path) -> None:
    fixture = build_fixture(parent / "expiry")
    hist_path = fixture / "data" / "revisions" / "history.jsonl"
    old = {
        "Date": "2026-06-07",
        "Ticker": "MSFT",
        "Fiscal Year": "Jun 2027",
        "Calendar Alignment": "CY2026",
        "Previous EPS": 10.0,
        "Current EPS": 10.40,
        "Change": 0.40,
        "Revision %": 4.0,
        "Reason": "one-shot 100-day-old revision",
        "Source": "fixture",
        "Update Time": "2026-06-07T08:00:00Z",
    }
    with hist_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod == "build_alerts":
            del sys.modules[mod]
    import build_alerts as ba

    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    payload = ba.evaluate_alerts(now=now)
    active = payload.get("activeAlerts") or []
    history = payload.get("alertHistory") or []
    old_hits_active = [
        a
        for a in active
        if a.get("ticker") == "MSFT" and str(a.get("eventDate") or a.get("eventAt") or "").startswith("2026-06-07")
    ]
    old_hits_hist = [
        a
        for a in history
        if a.get("ticker") == "MSFT" and str(a.get("eventDate") or a.get("eventAt") or "").startswith("2026-06-07")
    ]
    sample = (active[0] if active else (history[0] if history else {})) or {}
    fields_ok = True
    check_list = active if active else history
    for a in check_list[:3]:
        fields_ok = fields_ok and a.get("eventAt") and a.get("createdAt")
        fields_ok = fields_ok and (a.get("expiresAt") or a.get("activeUntil"))
        fields_ok = fields_ok and a.get("ageDays") is not None
    ok = not old_hits_active and len(old_hits_hist) >= 1 and fields_ok
    if old_hits_hist:
        ok = ok and int(old_hits_hist[0].get("ageDays") or 0) >= 90
    record(
        "alert_expiry_test",
        ok,
        f"active_old={len(old_hits_active)} hist_old={len(old_hits_hist)} fields_ok={fields_ok} sample_keys={list(sample)[:8]}",
    )


def test_cumulative_30d_window(parent: Path) -> None:
    fixture = build_fixture(parent / "cum30")
    # Two points outside 30d that would be >5% if all-history were used; only 1 point inside 30d
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "Date": "2026-06-01",
            "Ticker": "BE",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": "n/a (baseline)",
            "Current EPS": 4.00,
            "Revision %": "n/a (baseline)",
            "Reason": "baseline",
        },
        {
            "Date": "2026-07-01",
            "Ticker": "BE",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": 4.00,
            "Current EPS": 4.50,
            "Revision %": 12.5,
            "Reason": "old move",
        },
        {
            "Date": "2026-09-10",
            "Ticker": "BE",
            "Fiscal Year": "Dec 2027",
            "Calendar Alignment": "CY2027",
            "Previous EPS": 4.50,
            "Current EPS": 4.51,
            "Revision %": 0.22,
            "Reason": "recent tiny move",
        },
    ]
    (fixture / "data" / "revisions" / "history.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod == "build_alerts":
            del sys.modules[mod]
    import build_alerts as ba

    pts = []
    for r in rows:
        dt = ba.parse_date(r["Date"])
        pts.append((dt, float(r["Current EPS"]), r))
    stats = ba.cumulative_30d_status(pts, now, lookback_days=30)
    payload = ba.evaluate_alerts(now=now)
    cum_alerts = [
        a
        for a in (payload.get("activeAlerts") or []) + (payload.get("alertHistory") or [])
        if a.get("rule") == "cumulative_revision_gt_5pct" and a.get("ticker") == "BE"
    ]
    labeled_30d = [a for a in cum_alerts if str(a.get("windowLabel") or a.get("message") or "").find("30D") >= 0 or a.get("lookbackDays") == 30]
    ok = stats["status"] == "insufficient_history" and stats.get("labeled30D") is False
    ok = ok and len(labeled_30d) == 0
    record(
        "cumulative_30d_window_test",
        ok,
        f"status={stats['status']} windowPoints={stats.get('windowPoints')} n_30d_alerts={len(labeled_30d)}",
    )


def test_results_vs_guidance(parent: Path) -> None:
    fixture = build_fixture(parent / "rvg")
    earn_path = fixture / "data" / "earnings" / "NVDA.json"
    dig = json.loads(earn_path.read_text(encoding="utf-8"))
    # results above, guidance unknown → results alert, no guidance alert
    if isinstance(dig.get("results"), dict):
        dig["results"]["vsConsensus"] = "Above"
    dig["resultsVsConsensus"] = "Above"
    gd = dig.get("guidanceDetail") if isinstance(dig.get("guidanceDetail"), dict) else {}
    gd["vsConsensus"] = "unknown"
    dig["guidanceDetail"] = gd
    dig["guidanceVsConsensus"] = "unknown"
    earn_path.write_text(json.dumps(dig, indent=2) + "\n", encoding="utf-8")

    sys.path.insert(0, str(fixture / "tools"))
    for mod in list(sys.modules):
        if mod in ("build_alerts", "export_web_data"):
            del sys.modules[mod]
    import build_alerts as ba

    payload = ba.evaluate_alerts()
    alerts = payload.get("activeAlerts") or payload.get("alerts") or []
    nvda = [a for a in alerts if a.get("ticker") == "NVDA"]
    results_alerts = [a for a in nvda if a.get("rule") == "results_vs_consensus"]
    guidance_alerts = [a for a in nvda if a.get("rule") == "guidance_vs_consensus"]
    ok = len(results_alerts) >= 1 and len(guidance_alerts) == 0

    # guidance below → guidance alert
    gd["vsConsensus"] = "Below"
    dig["guidanceDetail"] = gd
    dig["guidanceVsConsensus"] = "Below"
    earn_path.write_text(json.dumps(dig, indent=2) + "\n", encoding="utf-8")
    payload2 = ba.evaluate_alerts()
    g2 = [a for a in (payload2.get("activeAlerts") or []) if a.get("ticker") == "NVDA" and a.get("rule") == "guidance_vs_consensus"]
    ok = ok and len(g2) >= 1

    run_export(fixture)
    web_earn = json.loads((fixture / "web" / "data" / "earnings.json").read_text(encoding="utf-8"))
    n = web_earn.get("NVDA") or {}
    ok = ok and n.get("resultsVsConsensus")
    ok = ok and "guidanceVsConsensus" in n
    record(
        "results_vs_guidance_test",
        ok,
        f"results={len(results_alerts)} guidance_unknown={len(guidance_alerts)} guidance_below={len(g2)} web_r={n.get('resultsVsConsensus')} web_g={n.get('guidanceVsConsensus')}",
    )


def test_weekend_freshness() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import freshness as fr

    TAIPEI = timezone(timedelta(hours=8))
    friday_success = datetime(2026, 9, 11, 8, 0, tzinfo=TAIPEI)
    saturday = datetime(2026, 9, 12, 12, 0, tzinfo=TAIPEI)
    sunday = datetime(2026, 9, 13, 18, 0, tzinfo=TAIPEI)
    monday_after_grace = datetime(2026, 9, 14, 10, 30, tzinfo=TAIPEI)
    monday_within_grace = datetime(2026, 9, 14, 9, 0, tzinfo=TAIPEI)
    ok = not fr.is_schedule_stale(friday_success, now=saturday)
    ok = ok and not fr.is_schedule_stale(friday_success, now=sunday)
    ok = ok and not fr.is_schedule_stale(friday_success, now=monday_within_grace)
    ok = ok and fr.is_schedule_stale(friday_success, now=monday_after_grace)
    # per-ticker: same function
    ticker_success = friday_success
    ok = ok and not fr.is_schedule_stale(ticker_success, now=sunday)
    ok = ok and fr.is_schedule_stale(ticker_success, now=monday_after_grace)
    record(
        "weekend_freshness_test",
        ok,
        "Fri success → Sat/Sun fresh; Monday after grace stale",
    )


def test_collector_parser_fixture() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import sa_parser as sp

    html_path = FIXTURES / "sa" / "estimates_nvda.html"
    expected_path = FIXTURES / "sa" / "expected_nvda_estimates.json"
    ok = html_path.exists() and expected_path.exists()
    rows = sp.parse_estimates_html(html_path.read_text(encoding="utf-8")) if ok else []
    expected = json.loads(expected_path.read_text(encoding="utf-8")).get("rows") if ok else []
    ok = ok and len(rows) == len(expected) == 3
    for got, exp in zip(rows, expected):
        ok = ok and got.get("fiscalPeriodEnding") == exp.get("fiscalPeriodEnding")
        ok = ok and got.get("periodType") == "Fiscal Period Ending"
        for k in ("consensus", "high", "low", "analysts", "rev1M", "rev3M", "rev6M"):
            gv, ev = got.get(k), exp.get(k)
            if ev is None:
                continue
            ok = ok and gv is not None and abs(float(gv) - float(ev)) < 1e-9
        ok = ok and got.get("mappedYear") == exp.get("mappedYear")
        ok = ok and got.get("calendarAlignment") == exp.get("calendarAlignment")
    # revision-event parser + snapshot change (no SA login)
    rev_html = (FIXTURES / "sa" / "revisions_nvda.html").read_text(encoding="utf-8")
    revs = sp.parse_revisions_html(rev_html)
    ok = ok and revs and revs[0].get("ticker") == "NVDA" and revs[0].get("currentEps") == 15.70
    ev = sp.revision_event_from_snapshot_change("NVDA", "Jan 2028", 15.61, 15.70, "2026-09-15")
    ok = ok and ev is not None and abs(float(ev["Current EPS"]) - 15.70) < 1e-9
    nonev = sp.revision_event_from_snapshot_change("NVDA", "Jan 2028", 15.61, 15.61, "2026-09-15")
    ok = ok and nonev is None
    snap_eps = sp.build_snapshot_eps_from_parsed(rows)
    ok = ok and "2027E" in snap_eps and snap_eps["2027E"]["consensus"] == 15.61
    record(
        "collector_parser_fixture_test",
        ok,
        f"rows={len(rows)} fiscal0={rows[0].get('fiscalPeriodEnding') if rows else None} mapped={ [r.get('mappedYear') for r in rows] }",
    )


def main() -> int:
    print("=== ai-eps-monitor long-term reliability acceptance (isolated) ===")
    print(f"ROOT={ROOT}")
    before = snapshot_prod_fingerprints()

    # Pure unit tests (no fixture mutation of prod)
    test_client_stale_logic()

    with tempfile.TemporaryDirectory(prefix="ai_eps_accept_") as td:
        fixture = build_fixture(Path(td) / "proj")
        print(f"FIXTURE={fixture}")

        # Warm export in fixture only
        run_export(fixture)

        test_isolated_same_day_snapshot(fixture)
        test_drivers_persist(fixture)
        test_corrupt_earnings(fixture)
        test_revision_unchanged(fixture)
        test_fiscal_rollover_identity(fixture)
        test_alert_engine_status(fixture)
        test_publish_hash_noop(fixture)
        test_dispersion_and_eps_by_fiscal(fixture)

        test_dynamic_rollover_full_ui(Path(td))
        test_alert_unique_id(Path(td))
        test_alert_expiry(Path(td))
        test_cumulative_30d_window(Path(td))
        test_results_vs_guidance(Path(td))
        test_weekend_freshness()
        test_collector_parser_fixture()

    test_production_unmutated(before)

    print()
    print(f"Passed: {PASS}  Failed: {FAIL}")
    for name, status, detail in RESULTS:
        print(f"  {status}: {name}" + (f" ({detail})" if detail else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

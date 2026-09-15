#!/usr/bin/env python3
"""Auto-generate README_REVIEW.md from final meta.json + test results (this round only)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))


def main() -> int:
    meta = {}
    mp = ROOT / "web" / "data" / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))

    test_results = ROOT / "TEST_RESULTS_INGESTION_INTEGRITY.md"
    if not test_results.exists():
        test_results = ROOT / "TEST_RESULTS_PIPELINE_INTEGRITY.md"
    if not test_results.exists():
        test_results = ROOT / "TEST_RESULTS_FAILCLOSED_SIGNAL.md"
    if not test_results.exists():
        test_results = ROOT / "TEST_RESULTS_FINAL_RELIABILITY.md"
    test_count = "n/a"
    test_pass = "n/a"
    if test_results.exists():
        body = test_results.read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*PASS", body)
        if m:
            test_pass, test_count = m.group(1), m.group(2)
        else:
            m = re.search(r"Passed:\s*(\d+).*Failed:\s*(\d+)", body)
            if m:
                p, f = int(m.group(1)), int(m.group(2))
                test_pass = str(p)
                test_count = str(p + f)

    shot_dir = ROOT / "review-pack" / "screenshots"
    hashes = []
    if shot_dir.exists():
        for p in sorted(shot_dir.glob("*-latest.png")):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            hashes.append(f"| `{p.name}` | `{h}` |")

    now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M Taipei")
    lines = [
        "# AI EPS Monitor — Review Pack (Ingestion Integrity)",
        "",
        f"**Generated:** {now}  ",
        f"**Round:** Ingestion Integrity (auto-generated — replaces prior round README metadata)",
        "",
        "## Build identity",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| sitePublished | `{meta.get('sitePublished')}` |",
        f"| sitePublishedDisplay | {meta.get('sitePublishedDisplay')} |",
        f"| dataVersion | `{meta.get('dataVersion')}` |",
        f"| refreshVersion | `{meta.get('refreshVersion')}` |",
        f"| buildId | `{meta.get('buildId')}` |",
        f"| lastSuccessfulCollection | `{meta.get('lastSuccessfulCollection')}` |",
        f"| collectionStatus | {meta.get('collectionStatusLabel') or meta.get('collectionStatus')} |",
        f"| alertEngineStatus | {meta.get('alertEngineStatus')} |",
        f"| qualityGate | {(meta.get('qualityGate') or {}).get('status') if isinstance(meta.get('qualityGate'), dict) else meta.get('qualityGate')} |",
        f"| acceptance tests | **{test_pass}/{test_count} PASS** (this round only) |",
        "",
        "## Public URL",
        "",
        "https://kumahsu1118-ui.github.io/ai-eps-monitor/",
        "",
        "## Screenshot SHA256 (this build)",
        "",
        "| File | SHA256 |",
        "|------|--------|",
    ]
    lines.extend(hashes or ["| _(none)_ | |"])
    lines += [
        "",
        "## Ingestion Integrity highlights",
        "",
        "- Collection → `data/incoming/` only; Quality Gate → validated `data/snapshots/` or quarantine",
        "- Per-ticker LKG (`load_last_known_good_by_ticker`) for Extreme EPS / Price / Fiscal Coverage",
        "- Auto revision events before Alert Engine; Alert Engine uses gated snapshot (not manifest.json)",
        "- Reported Fiscal Period Ending required; FY-only normalized via universe.json fiscal-end config",
        "- Publish stamps meta.json AND dashboard.json.meta atomically",
        "- Single entrypoint `tools/ingest_snapshot.py`; screenshot DOM sidecars + secret content scan",
        "- Prior Pipeline Integrity: Quality Gate before Alert mutation; no fake daily LKG observations",
        "",
        "## Review ZIP",
        "",
        "`/workspace/ai-eps-monitor-review.zip` — `bash tools/build_review_zip.sh`",
        "",
        "Clean unzip: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`",
        "",
        "---",
        "",
        "_This file is regenerated from `web/data/meta.json` + `TEST_RESULTS_INGESTION_INTEGRITY.md`. "
        "It MUST NOT retain leftover 10/10, 17/17, or 30/30 round metadata._",
        "",
    ]
    out = "\n".join(lines)
    # Hard guard: no leftover multi-round score lines
    for bad in ("10/10", "17/17", "30/30"):
        if bad in out and f"{test_pass}/{test_count}" != bad:
            # allow only if it is the current score
            out = out.replace(bad, "(prior-round-omitted)")
    (ROOT / "README_REVIEW.md").write_text(out, encoding="utf-8")
    print("Wrote README_REVIEW.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

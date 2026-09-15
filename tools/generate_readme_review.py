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

    test_results = ROOT / "TEST_RESULTS_COMMIT_SEMANTICS.md"
    for cand in (
        test_results,
        ROOT / "TEST_RESULTS_IDENTITY_DEPLOY_CRASH.md",
        ROOT / "TEST_RESULTS_INGESTION_INTEGRITY.md",
    ):
        if cand.exists():
            test_results = cand
            break

    test_count = "n/a"
    test_pass = "n/a"
    unit_line = "n/a"
    integ_line = "n/a"
    total_line = "n/a"
    if test_results.exists():
        body = test_results.read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*PASS", body)
        if m:
            test_pass, test_count = m.group(1), m.group(2)
        # Prefer acceptance total if present
        m_all = re.search(
            r"run_acceptance_tests\.py`?\s*→\s*\*\*(\d+)\s*/\s*(\d+)\s*PASS\*\*.*?(\d+\.\d+)s",
            body,
            re.S,
        )
        if m_all:
            test_pass, test_count = m_all.group(1), m_all.group(2)
            total_line = f"{m_all.group(1)}/{m_all.group(2)} in {m_all.group(3)}s"
        m_u = re.search(
            r"run_unit_tests\.py`?\s*→\s*\*\*(\d+)\s*/\s*(\d+)\s*PASS\*\*.*?(\d+\.\d+)s",
            body,
            re.S,
        )
        if m_u:
            unit_line = f"{m_u.group(1)}/{m_u.group(2)} in {m_u.group(3)}s"
        m_i = re.search(
            r"run_integration_tests\.py`?\s*→\s*\*\*(\d+)\s*/\s*(\d+)\s*PASS\*\*.*?(\d+\.\d+)s",
            body,
            re.S,
        )
        if m_i:
            integ_line = f"{m_i.group(1)}/{m_i.group(2)} in {m_i.group(3)}s"
        # Table fallback
        if unit_line == "n/a":
            mt = re.search(r"unit.*?\|?\s*(\d+)\s*\|\s*([\d.]+)s", body, re.I)
            if mt:
                unit_line = f"{mt.group(1)} in {mt.group(2)}s"
        if integ_line == "n/a":
            mt = re.search(r"integration.*?\|?\s*(\d+)\s*\|\s*([\d.]+)s", body, re.I)
            if mt:
                integ_line = f"{mt.group(1)} in {mt.group(2)}s"

    shot_dir = ROOT / "review-pack" / "screenshots"
    hashes = []
    if shot_dir.exists():
        for p in sorted(shot_dir.glob("*-latest.png")):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            hashes.append(f"| `{p.name}` | `{h}` |")

    now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M Taipei")
    lines = [
        "# AI EPS Monitor — Review Pack (Commit Semantics + Single Writer + Release Identity)",
        "",
        f"**Generated:** {now}  ",
        "**Round:** Commit Semantics + Single Writer + Release Identity (auto-generated — replaces prior round README metadata)",
        "",
        "## Build identity",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| sitePublished | `{meta.get('sitePublished')}` |",
        f"| sitePublishedDisplay | {meta.get('sitePublishedDisplay')} |",
        f"| dataVersion | `{meta.get('dataVersion')}` |",
        f"| refreshVersion | `{meta.get('refreshVersion')}` |",
        f"| buildId | `{meta.get('buildId')}` |",
        f"| schemaVersion | `{meta.get('schemaVersion')}` |",
        f"| appVersion | `{meta.get('appVersion')}` |",
        f"| releaseVersion | `{meta.get('releaseVersion')}` |",
        f"| lastSuccessfulCollection | `{meta.get('lastSuccessfulCollection')}` |",
        f"| collectionStatus | {meta.get('collectionStatusLabel') or meta.get('collectionStatus')} |",
        f"| alertEngineStatus | {meta.get('alertEngineStatus')} |",
        f"| qualityGate | {(meta.get('qualityGate') or {}).get('status') if isinstance(meta.get('qualityGate'), dict) else meta.get('qualityGate')} |",
        f"| acceptance tests | **{test_pass}/{test_count} PASS** (this round only) |",
        f"| unit suite | {unit_line} |",
        f"| integration suite | {integ_line} |",
        f"| total suite | {total_line} |",
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
        "## Commit Semantics + Single Writer + Release Identity highlights",
        "",
        "- Post-CURRENT materialize failure → committed + materializationStatus=failed (never aborted; no CURRENT rollback)",
        "- Single writer: ingest_snapshot.py only; export read-only by default; publish_github_pages.sh publish-only",
        "- Readers ensure_live_matches_current() before live cache; pending publish from CURRENT generation web/",
        "- appVersion canonicalizes index ?v=; releaseVersion = hash(appVersion|schema|data|refresh); finalize after stamp",
        "- Tier1 only officialDomainsByTicker; generic investor.*/ir.* → unverified_ir_candidate; SA host-strict",
        "- Parser: identical fiscal dedupe; conflicting → duplicate_conflicting_fiscal_row; mapped_slot_collision",
        "- Suite split: run_unit_tests.py (<30s) + run_integration_tests.py; run_acceptance_tests.py runs both",
        "",
        "## Review ZIP",
        "",
        "`/workspace/ai-eps-monitor-review.zip` — `bash tools/build_review_zip.sh`",
        "",
        "Clean unzip: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`",
        "",
        "---",
        "",
        "_This file is regenerated from `web/data/meta.json` + `TEST_RESULTS_COMMIT_SEMANTICS.md`. "
        "It MUST NOT retain leftover prior-round score metadata._",
        "",
    ]
    out = "\n".join(lines)
    for bad in ("10/10", "17/17", "30/30", "118/118"):
        if bad in out and f"{test_pass}/{test_count}" != bad:
            out = out.replace(bad, "(prior-round-omitted)")
    (ROOT / "README_REVIEW.md").write_text(out, encoding="utf-8")
    print("Wrote README_REVIEW.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

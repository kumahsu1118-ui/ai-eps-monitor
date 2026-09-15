# AI EPS Monitor — Review Pack (Commit Semantics + Single Writer + Release Identity)

**Generated:** 2026-09-15 23:06 Taipei  
**Round:** Commit Semantics + Single Writer + Release Identity (auto-generated — replaces prior round README metadata)

## Build identity

| Field | Value |
|-------|-------|
| sitePublished | `2026-09-15T14:24:56Z` |
| sitePublishedDisplay | Sep 15, 2026 22:24 Taipei Time |
| dataVersion | `7f93184951a93aef15985e40abd73ff0ac5e796069798909ac17b85730a8ba8b` |
| refreshVersion | `13b6e576e814b2f5cdbbfd778ce61edb4e1f7a7df9dd9a13a3922bab864f9bb0` |
| buildId | `7f93184951a93aef15985e40abd73ff0ac5e796069798909ac17b85730a8ba8b` |
| schemaVersion | `1` |
| appVersion | `824c9c361deeef2378513f18fc9eb9db7ec8fb4c4fcf179e52778164461f6d23` |
| releaseVersion | `f34c29665d2f766c114f827ba5085f55b0afe8739a989d90a09a8ac941c2d24b` |
| lastSuccessfulCollection | `2026-09-15T01:36:00Z` |
| collectionStatus | COMPLETE |
| alertEngineStatus | ok |
| qualityGate | ok |
| acceptance tests | **133/133 PASS** (this round only) |
| unit suite | 104/104 in 3.17s |
| integration suite | 31/31 in 3.91s |
| total suite | 133/133 in 7.08s |

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Screenshot SHA256 (this build)

| File | SHA256 |
|------|--------|
| `01-overview-latest.png` | `330d8a952ed055924a7ac449330854dc68b7242469156324cd1e8c44cb761d32` |
| `02-valuation-latest.png` | `e1de8fbd602ca408afe457e43d4372dad56803fac900f56e7feb9bacffcb6515` |
| `03-eps-revisions-latest.png` | `d478ccdf05c72e6939caaca14af2d0fda6756579e0bc22a3cf0b6a86ee3ac585` |
| `04-nvda-company-latest.png` | `004cb573988201f34439d3bd0f77b7fed5f4064a64cbc3268d254eee2e09e2b3` |
| `05-avgo-company-latest.png` | `d6c9e3118b215996c20703d0119875ed8a246e1e405c6c1cb7b58a63b7e1257c` |
| `06-nvda-earnings-latest.png` | `ae780105393efdcdaecd5d049e6be55f34dba32ef0b44be1c4bb8522642df16b` |
| `07-avgo-earnings-latest.png` | `b1117fa77f1eab05c8ac4db054b3f2f1173b6d482de6e6bf3f921adf28519690` |
| `08-mobile-overview-latest.png` | `3c2cbb1a18461335b6fc8f26e25c7678a9e4d6a0cdf25ca575cb0a6a6b6de022` |

## Commit Semantics + Single Writer + Release Identity highlights

- Post-CURRENT materialize failure → committed + materializationStatus=failed (never aborted; no CURRENT rollback)
- Single writer: ingest_snapshot.py only; export read-only by default; publish_github_pages.sh publish-only
- Readers ensure_live_matches_current() before live cache; pending publish from CURRENT generation web/
- appVersion canonicalizes index ?v=; releaseVersion = hash(appVersion|schema|data|refresh); finalize after stamp
- Tier1 only officialDomainsByTicker; generic investor.*/ir.* → unverified_ir_candidate; SA host-strict
- Parser: identical fiscal dedupe; conflicting → duplicate_conflicting_fiscal_row; mapped_slot_collision
- Suite split: run_unit_tests.py (<30s) + run_integration_tests.py; run_acceptance_tests.py runs both

## Review ZIP

`/workspace/ai-eps-monitor-review.zip` — `bash tools/build_review_zip.sh`

Clean unzip: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`

---

_This file is regenerated from `web/data/meta.json` + `TEST_RESULTS_COMMIT_SEMANTICS.md`. It MUST NOT retain leftover prior-round score metadata._

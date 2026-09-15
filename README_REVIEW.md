# AI EPS Monitor — Review Pack (Transactional & Idempotent Pipeline)

**Generated:** 2026-09-15 17:04 Taipei  
**Round:** Transactional & Idempotent Pipeline (auto-generated — replaces prior round README metadata)

## Build identity

| Field | Value |
|-------|-------|
| sitePublished | `2026-09-15T08:36:48Z` |
| sitePublishedDisplay | Sep 15, 2026 16:36 Taipei Time |
| dataVersion | `7f93184951a93aef15985e40abd73ff0ac5e796069798909ac17b85730a8ba8b` |
| refreshVersion | `13b6e576e814b2f5cdbbfd778ce61edb4e1f7a7df9dd9a13a3922bab864f9bb0` |
| buildId | `7f93184951a93aef15985e40abd73ff0ac5e796069798909ac17b85730a8ba8b` |
| lastSuccessfulCollection | `2026-09-15T01:36:00Z` |
| collectionStatus | COMPLETE |
| alertEngineStatus | ok |
| qualityGate | ok |
| acceptance tests | **89/89 PASS** (this round only) |

## Public URL

https://kumahsu1118-ui.github.io/ai-eps-monitor/

## Screenshot SHA256 (this build)

| File | SHA256 |
|------|--------|
| `01-overview-latest.png` | `2163d1d0fa497816105fcfe91c11acbf97e844e49e7a11ad8f3035979539be1a` |
| `02-valuation-latest.png` | `0658746299bbb7765607d7fcd48ce57be93138eb91769ce16824b9eff3745676` |
| `03-eps-revisions-latest.png` | `9a0a73585127e717c468461447f13f872a090df9cb07717779ce6961f920f924` |
| `04-nvda-company-latest.png` | `1b313a6dfd78902ef510f79ed4dcaed46106bd2ce157b674719fb5c631e8c4c7` |
| `05-avgo-company-latest.png` | `76ea6f4ef5fc7905f5b3f877002d186ca2f6df16841d39336f5cda1036922b4a` |
| `06-nvda-earnings-latest.png` | `af9429a11f93beee45f3271e1b0e02d13019815eefd10584bce6a65fb7b091ae` |
| `07-avgo-earnings-latest.png` | `942f8a0dc0bf659e699995418093ed1f27517cbf3a612870b3d9840c5afeefc5` |
| `08-mobile-overview-latest.png` | `6d916982bd5799d89b3fdec43d78e2cb0ac081a339f44bbdebe2550ba9cf94cb` |

## Transactional & Idempotent Pipeline highlights

- Transactional ingest: stage → export → atomic COMMIT; export failure ABORT (no LKG / no revisions / incoming unprocessed)
- Revision eventId idempotency (exact replay → 0 new); immutable validated snapshots (timestamp+contentHash)
- ingest --publish: single export + publish_prebuilt_site (no second export / no duplicate revisions)
- refreshVersion excludes sitePublished; .data-version written only after successful git push
- Unknown analystCount severity capped at Medium; UI Coverage (not Confidence) + Dispersion
- Earnings provenance: results.sourceUrl survives missing guidanceDetail; Reuters≠Tier1 classifier
- Revision generation fail-closed; null consensus skipped in daily observations

## Review ZIP

`/workspace/ai-eps-monitor-review.zip` — `bash tools/build_review_zip.sh`

Clean unzip: `unzip … && cd ai-eps-monitor-review && python3 tools/run_acceptance_tests.py`

---

_This file is regenerated from `web/data/meta.json` + `TEST_RESULTS_TRANSACTIONAL_IDEMPOTENT.md`. It MUST NOT retain leftover prior-round score metadata._

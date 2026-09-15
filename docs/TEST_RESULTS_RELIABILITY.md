# Final Reliability Fix — Acceptance Results

## Automated suite
```bash
python3 tools/run_acceptance_tests.py
```
**Result: Passed 8 / Failed 0**

| Test | Result |
|------|--------|
| Same-day EPS change → daily append + history last-wins | PASS |
| Drivers survive two exports | PASS |
| Corrupt earnings JSON not overwritten by stub | PASS |
| No EPS change → no new revision event | PASS |
| Consensus vs Site Published separate | PASS |
| PE uses Last Close only | PASS |
| Dispersion on NVDA 2027E | PASS |
| nextEarningsStatus estimated when SA-only | PASS |

## DATA STALE display bug (fixed during acceptance)
| Check | Result |
|-------|--------|
| meta.dataStale false but badge still visible | **Root cause:** `.meta-row { display:flex }` overrode HTML `[hidden]` |
| Fix | `.meta-stale-row[hidden] { display:none !important }` + JS sets `display:none` when not stale |
| Live CSS contains fix | PASS |

## Final publish / screenshot alignment (`web/data/meta.json`)

| Field | Value |
|-------|-------|
| Consensus Data As Of | Sep 15, 2026 09:36 Taipei Time |
| Last Successful Collection | Sep 15, 2026 09:36 Taipei Time |
| Site Published | Sep 15, 2026 12:22 Taipei Time |
| DATA STALE | False |
| meta.siteRepoCommit | `29f3d27e4de19bb29253b34cba1375f453f69a0f` |
| **Final site-repo HEAD** | `b5b8dc1cb83d8f74d5a31f72360628f782bf0d43` |

Screenshots captured after this publish must show the three display fields above and **must not** show DATA STALE.

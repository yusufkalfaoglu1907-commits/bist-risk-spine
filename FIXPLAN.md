# Fix Plan — BİST KG remediation (from AUDIT-2026-06-11)

**Decision applied (D1, confirmed 2026-06-11):** the 2026-06-07 listed-only rule is reversed in bounded form — external parents and unlisted debt issuers become **stub Company nodes** (`listing_status='EXTERNAL'`, no pricing, no LEI/sector requirements). All 729-denominator metrics filter `listing_status <> 'EXTERNAL'` so existing coverage numbers stay comparable.

**Ordering logic.** Three constraints drive the order, all learned the hard way in earlier sessions:

1. *Offline-first.* Anything fixable from committed reference files and caches comes before anything needing the network (45s tool budget, no background processes, cache persists only on adapter `__exit__`, KAP rate-limits after harvest bursts).
2. *Output honesty before data growth.* Code-only changes that stop the system from emitting misleading numbers ship first — they protect every query run while the slower data work proceeds.
3. *The universe decision gates the data work.* F2 (Ziraat/Denizbank/TEB instruments) and F3 (Zorlu-type groups) cannot be fixed without stub nodes — those issuers/parents aren't in the 729. So Phase 1 must land before Phases 2–3 pay off.

Every item follows the established pattern: adapter → committed reference JSON → idempotent loader → `backfill_gleif.py --stage X`, additive `ddl._MIGRATIONS`, provenance on every write, report JSON in `data/cache/`. Build/test in `/tmp/<fresh-name>.kuzu`, persist via `cat /tmp/x.kuzu > data/tmkg.kuzu` (mount can't open Kuzu directly; never reuse a /tmp filename after a killed run — stale lock).

---

## Phase 0 — Honesty & integrity (code-only, offline, no schema risk)

*Fixes: F1-partial, F4, F5, F6, F7, F10-partial. Effort: ~1–2 sessions. No network, no new data, fully testable on the /tmp copy.*

**0.1 Provenance-tier split in blast-radius output (F7).**
`group_blast_radius` gains per-member `min_path_confidence` (min over the path's CONTROLS edges — path edges are already available in the Cypher; return `min(r.confidence)` alongside `min(length(path))`) and a group total split: `outstanding_gleif_confirmed` vs `outstanding_inference_attached` (any path containing an edge < 0.85). Measured stake: 18% of the Koç headline (₺3.0bn via YKR) currently rides unmarked on 0.70 edges.

**0.2 Coverage-class gate + refusal logic (F1, F5).**
Every result gets a `coverage` block: `{coverage_class: assembled|partial|blind, nominal_coverage, seed_edge_count, excluded_at_ingest: N}`. Rules: seed has zero control edges → `blind`; group `nominal_coverage < 0.5` → suppress the headline TRY total (emit counts, currency mix, and per-member priced walls only — never a group ₺ figure). `excluded_at_ingest` reads `mkk_debt_report.json`'s unmatched `debt_count` sum (613 today) so the denominator is visible in the output itself, not a docstring. CLI prints the class first line.

**0.3 Cycle guard + apex fix (F6).**
(a) Post-load integrity check (new `--stage verify` or a check inside every loader): count CONTROLS cycles (`MATCH (a)-[:CONTROLS*1..6]->(a)`), fail loudly if > 0. (b) `resolve_group_root`: redefine apex as "no incoming CONTROLS *from outside the seed's ancestor set*" or, simpler given the small graph, detect the all-ancestors-disqualified case and raise instead of silently returning the seed as its own root (the injected-cycle test showed exactly this corruption). (c) Align docstring with code — the query traverses only CONTROLS; either add `SUBSIDIARY_OF` (2 KAP-only edges are currently invisible to rooting) or fix the docstring. Add the injection test from the audit as a permanent regression test.

**0.4 True control_hops (F4).**
GLEIF ultimate-consolidation shortcuts already carry `basis='ultimate-consolidation'` — no schema change needed. Compute `control_hops` over the subgraph `WHERE r.basis <> 'ultimate-consolidation'`; keep shortcut edges for reachability but stop letting them define depth. Re-run Koç: KCHOL→YKB→Yapı Kredi arms should report hop 2, not 1.

**0.5 Maturity-confidence passthrough (F10).**
`refinancing_wall` returns `min_maturity_confidence` per member; the `ddmmyyyy-multi` 0.5 path is currently unexercised but the wire should exist before it ever fires. One-line addition to the existing query.

**0.6 Eleven-row ISIN disambiguation (F9).**
Pure data entry into the existing `disambiguation` block of `data/reference/bist_isin.json` (the mechanism is already built): resolve EKGYO, ISDMR, ODAS, AVOD, MANAS, KRONT, MERIT, PNLSN, BLCYT, SANFM + 1 more by picking the traded common line, then `--stage bist`. ~1 hour including verification against KAP pages. EKGYO and İSDMR alone justify it.

---

## Phase 1 — Bounded universe widening (the gating change)

*Fixes: F3 root cause; unblocks F2. Effort: ~1–2 sessions, almost entirely offline — the data is already in caches.*

**1.1 Schema + policy.**
`listing_status='EXTERNAL'` (additive; no migration needed — STRING field exists). Stubs get `name`, `lei` (when from GLEIF), `source`, `extraction_method`, `confidence`; never sector, never equity Security. Every analytics denominator and the README coverage table filter EXTERNAL out — this is the contract that keeps the bounded decision bounded.

**1.2 External parents from GLEIF-L2 cache.**
The loader flag exists (`--create-missing-parents`); the parent LEIs/names are already in `gleif_parents.json` (74 direct + 36 ultimate-only, fetched 2026-06-08, zero new network calls). Run it with the EXTERNAL stamp. Expected: ~110 stubs, and groups like Zorlu (ZOREN's apex) become assemblable for the first time.

**1.3 SPV parents now in-universe.**
Re-run `--stage spv`: the 65 `no_in_graph_parent` SPV candidates in `spv_parent_report.json` get re-evaluated against the widened universe. The 0.70 gates (geo-token guard, unique-candidate rule, non-destructive) stay exactly as built — widening the universe must not loosen the matcher.

**1.4 Re-baseline.**
Regenerate every coverage metric post-widening (debt-issuer control coverage should jump well past the old 36% offline ceiling — that ceiling was an artifact of the universe rule, not of the data). Re-run the audit's F3 query: % of in-window instruments unreachable. Target: < 20% from 47%.

---

## Phase 2 — MKK ingest recovery (alias table + exclusion policy)

*Fixes: F2. Depends on Phase 1 for the big names. Effort: ~1 session offline (the MKK xlsx is back in the folder).*

**2.1 Policy exclusions become explicit.**
`_ISSUER_BLOCKLIST` grows reason codes: DİBS (303 instruments) → `sovereign-out-of-scope` — a decision, not a matcher accident. Report schema: `excluded_by_policy` vs `match_failed`, separately counted. Blast-radius `excluded_at_ingest` (0.2) reads only the second bucket.

**2.2 Alias table for match failures.**
Extend the verified `_ISSUER_ALIASES` (the VAKIFBANK→TVB pattern, MATCHER_VERSION bump → cache invalidation comes free, as designed): Ziraat Bankası, Denizbank, TEB, Destek Yatırım Bankası, Katılım VK, Anadolubank, Fibabanka → their new EXTERNAL stubs or listed entities where they exist. ~10–15 aliases recover ~230 of the 310 non-sovereign dropped instruments. Re-run `import_mkk_debt.py` + `--stage debt`; precision tests in `test_mkk_debt.py` already guard the matcher against regressions.

**2.3 Reconcile documentation with artifacts (F10).**
The README/memory coverage table is hand-written and has drifted (2,296/1,988/"~50 quarantined" vs the run reports' 1,683 written / 4 low-conf / 0 quarantined). Add a small script that regenerates the coverage table from `data/cache/*_report.json` — documentation becomes a build artifact, drift becomes impossible.

---

## Phase 3 — The FX channel (the money fix)

*Fixes: F1 root cause. The only phase with real source risk and network dependence. Effort: multi-session, harvester-style.*

**3.1 Currency labeling, offline first.**
602 XS instruments have `currency = NULL`. The MKK descriptions in committed `data/reference/mkk_debt.json` carry currency tokens for many (the word-boundary parser exists — extend/verify it for the XS rows; it was tuned so "EUR" ≠ "EUROBOND"). Even where nominal stays unknown, the output upgrades from "81 UNKNOWN" to "81 instruments, 74 USD / 7 EUR, unpriced" — the single cheapest honesty gain in the whole plan.

**3.2 FX nominal harvest from KAP yurt-dışı issuance disclosures.**
Same architecture as the proven TL pipeline: extend `kap_nominal_adapter` with an FX variant (drop the "Nominal Değer (TL)" label gate, add a currency-labeled gate; keep the adjacency + single-distinct-amount confidence rule that made the TL extractor safe), write `nominal_currency='USD'/'EUR'`, `nominal_confidence` per the existing tiers. Operational constraints from prior sessions apply verbatim: resumable time-budgeted harvester (~35s passes), persist cache on `__exit__`, expect KAP rate-limiting → fold into the existing weekly scheduled task rather than burst-harvesting.

**3.3 `fx-upper-bound` basis in outstanding.py.**
New basis for FX paper priced at *issue* size: folded into `outstanding_upper_by_currency` per currency, never into the confident TRY total, never FX-converted by us (report native currency; conversion is the analyst's job and rate choice). With 3.1+3.2, the Koç report becomes: "confident ₺16.9bn + upper-bound $Xbn / €Ybn" — the first version of the output an analyst could defend in a memo.

**3.4 Fallback if KAP FX disclosures prove thin:** GLEIF ISIN→issuer is already cached; public eurobond databases (e.g. exchange OData, prospectus aggregators) as a secondary reference file — same committed-JSON pattern, dated, `complete:false` until proven. Do not scrape anything login-gated (the MKK lesson).

---

## Phase 4 — Staleness plumbing

*Fixes: F11, F6-residual. Effort: ~1 session.*

- **4.1** `IN_SECTOR` gains `as_of`/`source` (additive `_MIGRATIONS`, backfill from `sector_backfill_report.json`'s `fetched_iso`).
- **4.2** Every analytics result header carries data ages: `{kap_seed, gleif_l1, gleif_l2, sectors, mkk_debt, last_nominal_refresh}` read from the report JSONs — one function, used by CLI and JSON output.
- **4.3** GLEIF cache TTL: entries older than 90 days re-fetched on next run (keyed check on each entry's stored date; `matcher_version` continues to handle logic changes — the two invalidation axes are independent and both needed).
- **4.4** Scheduled reseed: monthly `ingest_kap.py --seed` + `--stage classify` (new companies enter; fixes the `--from-graph` closed-world drift), then sector re-export reminder — sector refresh stays manual because the Sektörler export is user-supplied (no API, verified 2026-06-07).

---

## Phase 5 — Tests that catch data-shape regressions

*Fixes: F12. Effort: ~1 session, pays for itself the first time a source drifts.*

Invariant tests against **production run reports** (not fixtures), in the spirit of the existing live auto-skip pattern but always-on because they read committed/cached JSON:

- unmatched `debt_count` ≤ 5% of reference (would have caught F2 at 27%)
- zero EQUITY_TRADED companies with > N debt instruments and no control edge (catches the next ZOREN)
- every XS Security has non-null `currency` after Phase 3.1
- CONTROLS cycle count == 0 (the Phase 0.3 check, as a test)
- documented coverage table == regenerated table (catches F10-class drift)
- `coverage_class` refusal: blast radius on a blind seed must not emit a group total

---

## Explicitly deferred (with reasons)

- **Path-confidence compounding** (product over edges): premature until tier-splitting (0.1) has been used in anger; the worst current path is 0.66 and visible once 0.1 lands.
- **HOLDS_STAKE expansion via mid-cap KAP-subsidiary harvest**: real value (ownership-weighted contagion) but rate-limit-expensive and the audit showed structural fixes dominate; revisit after Phase 3. KSFIN-style JV misattribution is mitigated meanwhile by 0.1's tier marking.
- **Sector inheritance to SPV children** (F8 fix): do after Phase 1 so inheritance runs over the widened, corrected control graph, not before.
- **Phase-2 time series / EVDS / OHLCV**: unchanged priority; nothing above depends on it.

---

## Sequence summary

| # | Phase | Fixes | Network | Gate |
|---|-------|-------|---------|------|
| 0 | Honesty & integrity + 11 ISINs | F1p, F4, F5, F6, F7, F9, F10p | none | — |
| 1 | Bounded universe (stubs) | F3 | none (caches) | D1 ✔ confirmed |
| 2 | MKK aliases + exclusion policy | F2, F10 | none | Phase 1 |
| 3 | FX currency + nominals | F1 | KAP, budgeted | Phases 0.2, 1 |
| 4 | Staleness plumbing | F11 | light | — |
| 5 | Invariant tests | F12 | none | tracks 0–3 |

Rule of engagement until Phase 3 lands: **counts are meaningful; totals are not** — and after Phase 0, the system itself says so on every result.

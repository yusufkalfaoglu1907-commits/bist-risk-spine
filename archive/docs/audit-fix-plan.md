# Audit Fix Plan — BİST Knowledge Graph

Date: 2026-06-11 · Responds to: Adversarial Data-Quality Audit 2026-06-11 (F1–F12)
Decision on record (2026-06-11): in-universe-only rule amended — **curated external stub nodes approved** (supersedes the 2026-06-07 "listed-only, no `--create-missing-issuers`" decision for this bounded set only).

---

## 0. Corrections to the audit (verified against code before planning)

Three audit claims need adjustment; they change the plan's cost estimates.

- **F4 is cheaper than stated.** The audit says "the loader knows but the edge doesn't say" which GLEIF edges are ultimate-consolidation shortcuts. Wrong: `CONTROLS.basis` already carries `'direct-consolidation'` / `'ultimate-consolidation'` (verified in `ddl.py` line 151 and `gleif_l2_backfill.py`). The fix is a WHERE clause in `group_members`, not a schema/loader change.
- **F2's alias-table fix mostly cannot work.** The matcher-v2 hardening (2026-06-07) already established that the big unmatched issuers — Ziraat, Denizbank, TEB, Destek, TFKB, Fiba — are *true rejects*: their legal entities are not in the 729-company universe. An alias maps a name to an existing node; there is no node. The audit's "10–15 aliases recover ~230 instruments" only holds for issuers whose entity IS in-graph. Real recovery for the rest routes through F3's stub nodes. F2 and F3 are one workstream, not two.
- **F10's "~50 quarantined" mystery is resolved.** That figure belongs to the *equity* ISIN import (`import_bist_isin`, 2026-06-07: ~50 MKK xlsx rows rejected on ISO 6166 check digit), not the MKK *debt* run (which correctly reports `quarantined_isins: 0`). The docs conflated two pipelines. Fix is documentation, not investigation.

Everything else in the audit reproduced: `resolve_group_root` traverses only CONTROLS while its docstring claims SUBSIDIARY_OF (the 2 KAP-only SUBSIDIARY_OF edges are invisible to rooting today); no file in `analytics/` reads any `confidence` field.

---

## Constraints carried from prior build sessions (binding on every phase)

These were all hit live; ignoring them costs hours.

1. **Kuzu + synced folder:** the DB cannot be opened in-place from the sandbox (stale `.shadow` unlink → "Operation not permitted"). Build/verify on a fresh `/tmp/*.kuzu` filename, checkpoint, `cat` back over `data/tmkg.kuzu`. A timeout-killed run leaves an unclearable lock — always use a new /tmp filename.
2. **45s tool timeout, no background processes:** harvesters must be synchronous, time-budgeted (~35s), resumable. Adapter caches persist **only on `__exit__`** — a killed sweep loses its fetches. Use the cache-warmer pattern (`l2_warm.py` precedent).
3. **KAP rate-limits after bursts** (hit during the subsidiary harvest). Any new harvest is budgeted + scheduled, not a one-shot sweep.
4. **No public bulk feeds.** BİST/MKK ISIN registry is login-gated; İş Yatırım endpoints 401; new MKK exports are manual user uploads. Assume the same wall for XS eurobond pricing — plan around committed reference files (the `bist_isin.json` pattern), not scraping.
5. **Kuzu reserved param names:** `$desc`, `$end` already bit us; check new params against the keyword list.
6. **Decisions on record:** take-latest@0.5 multi-date maturity stays approved. Listed-only default stays for *fuzzy-matched* creation; only the curated stub set below is exempt.

---

## Phase 1 — Stop the output from lying (no new data, ~2–3 days)

All offline, all testable with existing fixture patterns. Until this phase ships, every blast-radius report carries the banner the audit demanded: **"counts are meaningful; totals are not."** Add the banner first (one line), then remove it when 1.1–1.4 land.

**1.1 Coverage-class preamble + refusal logic (F1, F5).** Every `group_blast_radius` result opens with: seed edge count, whether the seed appears in `spv_parent_report.json:no_in_graph_parent` or `mkk_debt_report.json:unmatched`, nominal coverage %, and `coverage_class: assembled | partial | blind`. Rules: `blind` → no `group_total` key at all (counts + currency mix only); nominal coverage < 50% → totals emitted under `partial_totals` with the unpriced instrument count adjacent, never as a headline figure. This converts the DNFIN failure (confident emptiness) and the Koç failure (₺16.9bn anchor) into the same honest shape.

**1.2 Provenance-tier split + path confidence (F7).** Per-member `min_path_confidence` (product over the member's control path); group totals split by worst-edge tier: `gleif_confirmed` / `kap_declared` / `inference_attached`. The Koç report must show "GLEIF-confirmed ₺13.9bn; inference-attached +₺3.0bn" natively. Flag the known false-precision case: KCHOL→KSFIN is a JV misattributed as sole control — demote or annotate that edge (`basis='spv-naming-convention-jv-suspect'`) as part of this item.

**1.3 Currency for XS paper (F1c).** Extend `parse_currency` in `mkk_debt_adapter.py` to recover USD/EUR from XS descriptions (audit confirms it's recoverable); re-run the offline debt stage. Output changes from "81 UNKNOWN" to "81 instruments, mostly USD/EUR, unpriced". Cheap, and it is the prerequisite for ever reporting an FX wall honestly.

**1.4 Ingest-exclusion line (F2 reporting half).** Every analytics result carries "instruments excluded at ingest: N (see mkk_debt_report)" read from the run report, so the denominator is honest even before Phase 2 recovers anything.

**1.5 Cycle defense + resolver fix (F6).** (a) Post-load integrity check counting CONTROLS cycles, fail-loud, wired into every `--stage` run; (b) apex definition becomes SCC-aware (collapse strongly connected components before rooting, or "no incoming edge from outside the SCC"); (c) resolve the doc/code divergence by **adding SUBSIDIARY_OF to the upward traversal** (not by editing the docstring down — the 2 KAP-only edges should count). Test with the injected KOCFN→KCHOL cycle from the audit as a fixture.

**1.6 `control_hops` over direct edges only (F4).** `group_members` computes hops over `basis <> 'ultimate-consolidation'`; membership query unchanged. One clause + one test (KCHOL→YKB→YKR must report hops 1, 2).

---

## Phase 2 — Graph completeness via curated stubs (decision taken, ~2–4 days)

**2.1 External stub nodes (F3 — highest leverage).** New `loaders/external_stub_backfill.py` + `--stage stubs`. Sources, in confidence order: `gleif_parents.json` external parents (74 skipped + 36 logged — LEI-keyed, 0.95), SPV report's 65 `no_in_graph_parent` rows (named parents, 0.70), MKK unmatched issuer list (53 issuers). Stub = Company node with `listing_status='EXTERNAL_STUB'`, `is_listed=false`, LEI where known, **no pricing, no equity ISIN, excluded from all equity-side analytics by the existing listing_status filter pattern**. Expected yield: ~65 orphaned SPVs become assembled groups (Zorlu, Fiba, participation-bank sukuk programs) at zero new-data cost. Bounded set (~120 nodes), every stub carries `source` + the report row that justified it — this is curation, not fuzzy creation, which is why it doesn't reopen the matcher-precision fight.

**2.2 MKK unmatched split (F2).** Reprocess the 613 lost instruments in three buckets: (a) **policy blocklist** — DİBS sovereign book (303) excluded explicitly with `reason='sovereign-out-of-scope'`, no longer an accident of token overlap; (b) **attach to stubs** — Ziraat (119), Denizbank (50), TEB (18), etc. issue against their new stub nodes; (c) **alias table** — only for the residual whose entity is genuinely in-graph. Re-run `--stage debt`; acceptance: unmatched ≤ 5% of reference, every exclusion has a reason code.

**2.3 The 11-row ISIN disambiguation file (F9 — user task, ~1h).** `data/reference/isin_disambiguation.json`, manually verified, EKGYO and ISDMR first. I can pre-fill candidates (TRE…00019 vs …00027 per name) from `bist_isin.json:ambiguous` for you to confirm — the confirmation must be human, per the provenance-first rule. Loader honors it in `--stage bist`.

**2.4 Sector inheritance (F8).** Propagate sector parent→NEI-child over CONTROLS with `sector_basis='inherited-from-parent'` (additive `IN_SECTOR` property, never overwrites a KAP-assigned sector). Plus: every sector-dimensional result reports **instrument-weighted** coverage next to company-weighted (the "606/729 classified but only 30% of instruments" trap).

---

## Phase 3 — Money (external-data-bound; honest before complete)

Ordering matters: after 1.3 the output already *says* "USD/EUR unpriced" instead of misleading. Pricing comes after labeling.

**3.1 XS pricing source, in feasibility order.** (a) **KAP eurobond issuance disclosures first** — extend the existing `kap_issuance` / `kap_nominal` machinery past the TL-label gate to FX-labeled bulletins; the machinery, resumability, and scheduled task already exist, only the currency gate is v1-scoped. (b) **User-supplied export** as fallback — the MKK-xlsx pattern: committed, dated reference file. (c) Do **not** plan on scraping bond OData/prospectus feeds — every comparable feed we probed was gated (constraint 4), and web-fetch restrictions apply in-sandbox. Whatever the source, FX nominals enter as `basis='fx-issue-size-upper-bound'`, reported in the upper-bound bucket only, **never folded into the confident total** — this fixes the audit's structural complaint that the upper-bound bucket is empty for XS paper.

**3.2 No single-currency headline.** Group walls report per-currency, full stop. No USDTRY conversion into a ₺ headline — a converted figure would re-create the anchoring hazard with an FX assumption stacked on top. If a user wants a converted number, they bring the rate.

**3.3 Maturity confidence pass-through (F10).** `refinancing_wall` emits min/mean `maturity_confidence` per member; instruments at conf < 0.9 counted separately. Wires the alarm the gate already arms — relevant the day take-latest@0.5 rows actually load.

---

## Phase 4 — Staleness + verification (unglamorous, ~2 days, prevents regression)

**4.1 Temporal markers (F11).** `as_of` + `source` on `IN_SECTOR` (additive migration); `fetched_iso` of each layer surfaced in every analytics result header. 90-day TTL on `gleif_lookups.json` entries (age check at read, refetch on expiry — respecting the cache-warmer pattern, constraint 2).

**4.2 Scheduled reseed (F11).** Extend the existing weekly task (Mon 07:03): add a monthly KAP `--seed` re-run + sector-membership drift check, budgeted for rate limits (constraint 3). New listings/SPVs stop being invisible.

**4.3 Invariant tests against production run reports, not fixtures (F12).** New `tests/test_invariants.py` reading `data/cache/*.json`: unmatched debt ≤ 5% of reference; no EQUITY_TRADED company with > 5 debt instruments lacking a control edge; XS instruments must have non-null currency; zero CONTROLS cycles; every figure in README coverage tables regenerated from reports (script, not hand-written). These would have caught F2 and F10 mechanically.

**4.4 Doc reconciliation (F10).** Single source of truth: a `scripts/regen_coverage_docs.py` that rewrites the coverage table in the README/docstrings from the run reports. Kill the 2,296/1,988/~50 drift class permanently.

---

## Order of execution and acceptance gate

Week 1: Phase 1 complete (banner removed only when 1.1–1.4 all land) + 2.3 candidate file prepared for your sign-off.
Week 2: Phase 2 (stubs → debt re-run → sector inheritance), then Phase 4.3 invariants immediately after, so the new state is locked in.
Week 3+: Phase 3 pricing (KAP FX bulletins first), 4.1–4.2, 4.4.

Audit's gate restated as the exit criterion: external-parent stubs (F3) ✓ Phase 2.1 · unmatched split + exclusion reporting (F2) ✓ 1.4 + 2.2 · provenance split + refusal logic (F1/F5/F7) ✓ 1.1 + 1.2 · cycle check (F6) ✓ 1.5. Until all four ship, every report self-labels "counts are meaningful; totals are not."

What this plan does **not** promise: a true Koç-group ₺ wall. Even after Phase 3, XS figures are issue-size upper bounds, not outstanding balances — amortization and buybacks on eurobonds are invisible to every source we can reach. The deliverable is a per-currency wall with honest bases, which is what the use case actually needs.

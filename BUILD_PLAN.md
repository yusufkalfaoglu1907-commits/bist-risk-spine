# BUILD_PLAN.md — Finance KG v2

Phased build of the v2 alpha system (`system-design-v2.md`). Read with `CLAUDE.md` (invariants, data contract, session protocol) and `VERIFICATION.md` (how each gate is checked).

## Sequencing philosophy (why the order is what it is)

A naïve plan builds L1 → L2 → L3 → pillars. That is wrong here, because the dominant risk in this project is **not** "does it run" — it is **self-deception**: a lookahead leak, a survivorship bias, or a flow factor leaking into "residual" that makes a dead strategy look alive. So the order is set by three rules:

1. **Falsify the cheapest-to-kill thesis first.** The correlation pillar is data-light and rests on one falsifiable claim: *stable residual structure survives the factor strip.* That experiment (M3) is cheap and can kill the lead pillar. Run it before investing in the scarce-data pillars (events, supply chain). Front-loading a kill-experiment is not pessimism — it's how you avoid building infrastructure for a dead idea.
2. **Build the judge before the contestants.** The promotion gate, signal registry, and PIT backtester (M4) exist *before* the first "real" signal (M5). Build the scoring harness after the signal and you will rationalize a pretty backtest.
3. **Bake in what can't be retrofitted.** Bitemporality and the PIT access wrapper (M0) come first because retrofitting them is a rewrite, not a patch.

**Process-is-procrastination check:** all of this scaffolding is justified *only* because it front-loads the M3 kill-experiment. If several sessions pass with lots of harness and still no M3 result, that is a smell — say so in `BUILD_LOG.md`.

Each milestone has an **exit gate** that is a *verifiable* go/no-go, not a vibe. Don't advance until it's green. Gates marked **[STOP]** are project-level — surface the result to the user before proceeding.

---

## M0 — Foundations & data-access proof  ✅ COMPLETE (2026-06-19)

**Status:** exit gate met — `make smoke` PASS (Matriks REST proven, ADR-0002), PIT-leak
detector GREEN, id-bridge round-trip GREEN, survivorship/W2 mechanism GREEN, `make verify`
GREEN end-to-end (96 passed / 4 skipped, the skips deferred to M1/M3). Next milestone: **M1**.

**Goal:** prove the data is reachable and stand up the spine everything else depends on. No pillar logic yet.

**Build:**
- **Task 1 — data-access smoke test (do this first, before anything else).** From Claude Code, call each connector once (Matriks OHLCV for one ticker; EVDS one series; KAP one filing). Confirm real data returns. Snapshot a tiny **golden sample** to `tests/golden/`. If any connector is unreachable → **[STOP]**, log, ask the user (per `CLAUDE.md` §4/§8). Do not stub.
- L2 store: DuckDB + Parquet layout; schema for prices/returns/factors/betas/residuals.
- **The PIT / bitemporal data-access wrapper** (`tmkg/pit/`): the single gateway to L1 and L2. Requires an `as_of` date; refuses any row with `knowledge_date > as_of`. Signal code uses *only* this.
- Verification-harness skeleton: a `make verify` (or `scripts/verify.sh`) entry point that runs the invariant suite (`VERIFICATION.md`), plus `.mcp.json` checked in.
- ID-bridge resolver + test: ticker ↔ ISIN ↔ mkkMemberOid ↔ LEI round-trips on the golden sample.

**Exit gate:** every connector reachable and golden-sampled · PIT-leak detector passes on golden samples (a read with `as_of = D` returns nothing dated after D) · id-bridge test green · `make verify` runs end-to-end. **Nothing downstream starts until this is green.**

---

## M1 — Clean return series (the financial-accuracy core)  ✅ COMPLETE (2026-06-20)

**Status:** exit gate met — `make verify` GREEN (146 passed / 2 skipped; the 2 skips are M3 signal-AST-scan + L1 provenance soft-edges, both later milestones), `make smoke` PASS (Matriks + EVDS). Each gate criterion mapped to its evidence:

| Exit-gate criterion | Evidence |
|---|---|
| Total return across a hand-verified corporate action matches to tolerance | `tests/golden/test_total_return_reconciliation.py` — EREGL rights ex-date 2024-11-27 = TRY −1.3972% / USD −1.3803%; ASELS rights 2023-08-25 = TRY +2.1081% (back-adjusted ⇒ no dilution gap) |
| USD-primary + CPI-real-TRY cross-check, end-to-end through L2/PIT | `tests/l2/test_cpi_pipeline_end_to_end.py` + `tests/golden/test_cpi_real_try_reconciliation.py` — FY2023 EREGL real −30.3486% vs nominal +7.6116%, reconciled read-back from L2; CPI knowledge-date gate holds |
| Limit-lock days flagged on a known locked sequence | `tests/returns/test_limit_lock.py`, `tests/invariants/test_limit_lock.py` — ±10% censoring + cumulative-across-lock-window |
| Staleness flags (non-trading / carried-forward) | `tests/returns/test_staleness.py` |
| Delisted name present in the as-of universe (survivorship) | `tests/invariants/test_real_delisting.py` — SODA (Soda Sanayii, delisted 2020-09-30 into Şişecam), bitemporal two-row model; `tests/invariants/test_survivorship.py` (mechanism) |
| accounting_regime correct across both FY2023 and FY2025 switches | `tests/invariants/test_accounting_regime.py` (state machine + no-straddle guard) + `tests/l2/test_accounting_regime_ingestion.py` (real KCHOL declaration-dated rows land in L2, knowledge_date = declarationDate) |
| Reconciliation reports written (§4) | `data/cache/{m1_ingestion,survivorship_ingestion,evds_smoke,matriks_smoke}_report.json` |

**Deferred (non-blocking, NOT gate criteria) — carried into M2:**
- **Real declared-dividend full-TR reconciliation through the pipeline.** The dividend-as-yield mechanism (`dividend_yields_from_raw` + the constructor) is unit-tested on a fixture and is correct; an end-to-end reconciliation on a *real* declared EREGL/ASELS dividend additionally needs the vendor's **unadjusted** close on the ex-date (the yield denominator), which is a fundamentals-data verification best done with the M2 factor/fundamentals pull. The exit gate is satisfied by the bedelsiz/rights reconciliation above; this only hardens the dividend path.
- **SODA's true first-listing `valid_from`.** Modelled as a documented sourced lower bound (2020-01-30 merger-announcement date); the true IPO date could not be cleanly sourced for the delisted entity (vendors surface the unrelated SODSN). All survivorship assertions sit inside the airtight 2020-06..2020-10 sourced window, so nothing depends on the bound. Backfill when an authoritative IPO date is sourced.

**Next milestone: M2.**

**Goal:** the one input every signal shares — a trustworthy USD-primary total-return series. The easiest thing to get silently wrong, so it gets its own milestone and golden masters.

**Build:**
- Ingestion adapter: Matriks OHLCV + corporate actions + dividends → L2, all bitemporal (publication/declaration dates as `knowledge_date`).
- Total-return construction: corporate-action-adjusted (bedelsiz, rights, splits), dividends added for total return. Confirm the W7 finding (Matriks daily bars are back-adjusted; add cash dividends).
- USD conversion (USD-primary) + CPI-real-TRY cross-check series.
- **Limit-lock detection** (±10% band, widened-band aware) → flag + cumulative-return-across-lock-window handling.
- **Staleness flags** (non-trading / halts) for later Dimson/Scholes-Williams correction.
- `accounting_regime` tag on every fundamental datum.
- Survivorship: ingest at least one delisted name end-to-end; confirm it persists.

**Exit gate:** **golden-master reconciliation** — total return across a hand-verified corporate action (a known bedelsiz/bonus issue) matches to tolerance · limit-lock days correctly flagged on a known locked sequence · delisted name present in the as-of universe · accounting_regime correct across both FY2023 and FY2025 switches · reconciliation report written. See `VERIFICATION.md`.

---

## M2 — Factor model + neutralization (the residual machine)  ✅ COMPLETE (2026-06-22)

**Status:** exit gate met — `make verify` GREEN (246 passed / 2–5 skipped; the 2 persistent skips are M3 signal-AST-scan + L1 provenance soft-edges; extra skips are live Matriks/EVDS/WGB drift tests auto-skipping after a heavy price pull rate-limits the gateway). **19 factor series + BIST-30 prices / total_returns / universe_class landed in L2**; `run_m2_factor_model(require_all_factors=True, window=60, min_obs=40)` over 2023–2026 → `m2_gate_diagnostics`. Each gate criterion mapped to its evidence:

| Exit-gate criterion | Evidence |
|---|---|
| Residuals orthogonal to the stripped factors by construction | `tests/invariants/test_neutralization_orthogonality.py` (structural — exact OLS projection orthogonal to every ladder rung; teeth-check that an un-stripped factor stays correlated) |
| Betas stable within a regime and *break* across the 19-Mar-2025 shock | `data/cache/m2_gate_report.json::regime_break_primary` — peri-shock parsimonious break on well-identified market/FX/credit betas: **XU100 1.82, TRCDS5Y 1.43** (>1, clean break), USDTRY 0.84 (Δβ 1.19, locally turbulent FX). The full 18-factor *partial*-beta break (~0.6) is collinearity-/drift-confounded — both lenses reported. Pinned by `tests/factors/test_diagnostics.py` (peri_obs isolates a local break; subset recovers a planted break) |
| Factor model explains a plausible variance share per `universe_class` | `data/cache/m2_gate_report.json::variance_share_by_class` — operating R²≈0.67, holding ≈0.80, gyo_reit ≈0.66 (30 names scored); `tests/factors/test_diagnostics.py` + `tests/l2/test_m2_gate_diagnostics.py` |
| No factor silently dropped | `ingest.pipeline.factor_coverage` / `require_all_factors=True` (run report `missing_factors=[]`, all 18 present); `tests/factors/test_registry.py` |
| Reconciliation/audit reports written (§4) | `data/cache/{bist30_ingestion,universe_class_ingestion,m2_factor_model,m2_gate,factor_ingestion}_report.json` |

**Key implementation notes (durable):**
- **Beta estimator standardizes regressors before Ledoit–Wolf shrinkage** (`factors/betas.py::_estimate_betas`) — LW acts on the scale-free correlation matrix, betas mapped back to raw units. Without this, the mixed-unit panel (FFLOW ~10² USD-mn vs simple returns ~0.02) crushed small-scale betas to ~1e-6. Exactly equivariant for OLS.
- **`universe_class` is derived from the v1-graph sector via a rule table** (`ingest/universe.py`), never hardcoded per name; unresolved names are refused, not guessed. BIST-30 = 25 operating / 4 holding / 1 gyo_reit.
- **The betas-break criterion is judged on a parsimonious peri-shock break** (`diagnostics.regime_break_on_subset` + `assess_regime_break(..., peri_obs=…)`), because the full-panel partial-beta marginals are masked by factor collinearity and long-regime drift. L2 `betas` stay the full-model betas the neutralization uses; the parsimonious betas are an in-memory diagnostic lens.

**Deferred (non-blocking, carried forward):** real declared-dividend full-TR reconciliation (needs the vendor unadjusted ex-date close — from M1); AKD daily foreign-flow overlay (~2025+ cross-check leg, secondary); MSCIEM/EEM (no source on Matriks/FRED — market rung covered by XU100+VIX).

**Next milestone: M3 — DONE ✅ GO (2026-06-23). Now M4 (promotion gate + signal registry + PIT backtester).**

---

### M2 — original plan (for reference)

**Goal:** produce honest residual returns by stripping common factors in the design's explicit order.

**Build:**
- Core factor set (L2): market, USD/TRY + EUR/TRY, TRY 2y/10y + Turkey CDS (proxy per data-sourcing W3), Brent/gas, BIST sector indices, **foreign-flow / ownership-tier factor**, holding-group, gold, VIX (FRED), MSCI-EM (EEM proxy).
- Rolling, regime-aware betas with **Ledoit–Wolf shrinkage**; fit **per `universe_class`** (operating / gyo_reit / holding / investment_trust / etf).
- **Explicit neutralization order:** market → FX → rates/CDS → energy → sector → foreign-flow → holding-group → residual. Residuals computed by this exact ladder so the residual claim is falsifiable.

**Exit gate:** residuals are orthogonal to the stripped factors by construction (test) · betas are stable enough within a regime and *break* across known regime boundaries (2025 March shock) as expected · factor model explains a plausible variance share per class · no factor silently dropped.

---

## M3 — Residual-survival gate **[STOP — project-level go/no-go]** ✅ GO (2026-06-23 — see `decisions/ADR-0003`)

**Status:** **GO (with documented caveat).** Wide-universe gate run over **573 names / 48 sectors / 605 residual dates**, full 18-rung strip ending `…>FFLOW>XHOLD` (residuals ⊥ FFLOW by the M2 orthogonality invariant — surviving structure is *by construction* not the flow factor). Decision = GO on the **scale-invariant `lift` metric** (chance-adjusted persistence **14–37× across every granularity × window cell**, robustness in `data/cache/m3_robustness_report.json`); the coded gate stays NO-GO on the absolute-Jaccard sub-check (~0.095 vs 0.10), which the sweep proved is **edge-count-confounded** (coarsening sectors *lowers* Jaccard while *raising* lift) — the wrong instrument at this universe size. Threshold **not** weakened (§8); GO is a documented human decision. Evidence:

| Exit criterion | Evidence |
|---|---|
| Residual (not raw) correlation engine; sector-restricted, shrinkage before inversion | `signals/correlation.py` (LW on standardized residuals, Alves block-diagonal, MST/PMFG, BH-FDR over within-sector candidate family) — 24 tests |
| Stability across rolling windows after the strip | `signals/stability.py` edge-set Jaccard vs random-overlap floor → `lift`; `data/cache/m3_gate_report.json` |
| A documented stability metric + a decision | median `lift` 19.6 (headline), 14–37× across the robustness grid; **GO** ratified by user, recorded in `ADR-0003` |
| Honest kill-test (a true NO-GO = lift ≈ 1) | lift is unanimously ≫3 → not the flow factor; pillar **survives** |

**Caveat carried to M5:** absolute window-to-window edge overlap is moderate (~9.5%) and weight rank-stability is low (ρ 0.14) — build residual stat-arb from the **persistent core** and prove it in the **venue-feasible** book, not just frictionless research.

**Goal:** answer the one question the correlation pillar lives or dies on — *does stable residual linkage survive the strip, or was the "alpha" just the foreign-flow factor we removed?*

**Build:**
- Correlation engine on **residuals** (not raw returns): factor-decomposed covariance with **sector-restricted residual covariance** (Alves-style) before any inversion (`p ≈ n` — never invert the raw sample covariance). Graphical-lasso / PMFG/MST filtering + **FDR control** (125k pairs guarantees false discoveries).
- Stability test: is the filtered residual network **stable across rolling windows** after neutralizing market + FX + foreign-flow?

**Exit gate [STOP]:** a documented stability metric and a decision.
- **GO:** stable residual structure survives → correlation pillar leads; proceed to M4/M5.
- **NO-GO:** it doesn't → the pillar's alpha *is* the flow factor. **The pillar fails honestly here, cheaply, not expensively in a backtest.** Re-plan: the event pillar (M6) may lead instead. Surface to the user either way.

This is the most important milestone in the plan. It is placed early on purpose.

---

## M4 — Promotion gate + signal registry + backtester (the judge, before any real signal)  🟢 HARNESS BUILT & SELF-TEST GREEN (2026-06-23) — adversarial review pending

**Status:** the judge is built and the **exit-gate self-test passes** (`tests/signals/test_harness_selftest.py` + `test_stats.py` + `test_backtest.py`); `make verify` GREEN (327 passed / 1 skipped). Each exit-gate criterion mapped to its evidence:

| Exit-gate criterion | Evidence |
|---|---|
| Known-null (shuffled labels) **fails** the gate | `test_known_null_shuffled_labels_is_rejected` — permuting the forward-return rows destroys the edge: candidate net Sharpe ≈ 0.02, DSR 0.03 (< 0.95), fails `beats_baselines` + `dsr_passes` + `pbo` ⇒ **not promoted** |
| Known-good toy **passes** | `test_known_good_candidate_is_promoted` — clean persistent predictor: candidate net Sharpe 1.19 vs best baseline 0.66 (persistence), DSR 1.000 (benchmark 0.121 after a 50-trial haircut), PBO 0.000 ⇒ **promoted** |
| Backtester reproduces a hand-checked toy P&L | `test_research_book_pnl_is_hand_checked` + `test_venue_feasible_book_pnl_is_hand_checked` — 2-name/2-date P&L reconciled to the penny incl. turnover cost + borrow |
| All three books produce output | `test_all_three_books_produce_output` — research / venue_feasible / stress all emit a finite net Sharpe over the same weights; constraints bite (`short_eligible` clip, blanket short-ban, limit-lock carry) |
| DSR / PBO pinned before any real signal | `test_stats.py` — DSR null-fails / good-passes asymmetry; PBO=1.0 on a sign-flip overfit, 0.0 on a real edge, ~0.5 on noise (CSCV) |
| Verdict is auditable + PIT-honest | `test_verdict_round_trips_through_l2_and_pit` — registry row lands in L2; a PITAccess read dated before the write sees nothing, after sees the verdict |

**Key implementation notes (durable):**
- **"`DSR > 0` after trial-count adjustment" is implemented as DSR (a probability ∈ [0,1]) ≥ a documented confidence (default 0.95).** The Bailey–LdP DSR is the Probabilistic Sharpe evaluated at the *expected-max* Sharpe of `n_trials` (the haircut). Gating on a literal ">0 probability" is meaningless; gating at 0.95 is the faithful reading of "the deflated excess Sharpe is *credibly* positive". Documented in `signals/stats.py`, **not** a weakened invariant.
- **PBO is computed over {candidate, *baselines}** as the strategy set (CSCV, `n_partitions=10`) — it asks whether the candidate's in-sample dominance over the ladder is a cross-validation artifact.
- **The promotion gate is the AND of four checks** (`signals/promotion.py::evaluate_candidate`): beats every baseline's net Sharpe · clears the capacity floor in the **venue-feasible** book · DSR passes · PBO < threshold. A lucky null that edged one baseline still dies on DSR/PBO.
- Modules: `stats.py` (DSR/PBO, pure), `backtest.py` (purge/embargo splits, `CostModel`, three `BookConfig`s, `capacity_curve`), `promotion.py` (ladder + gate), `registry.py` (bitemporal L2 write + §4 report). All L3-clean (no-network AST scan green).

**Remaining before M4 is declared COMPLETE:** ① **adversarial review** of the harness per `VERIFICATION.md` §4 (a fresh agent tries to make the gate promote noise / reject signal) — this is a [STOP]-style gate; surface to the user. ② a thin PIT *runner* that feeds **real** residual panels (from M2/M3 L2) into the gate — naturally folded into M5 (first real signal), since the self-test already proves the harness on synthetic worlds.

**Next milestone: M5 — first real signal (residual stat-arb), gated by this harness.**

**Goal:** the scoring harness that decides whether *any* future signal is real. Built before M5 so no signal is ever graded by a harness written to flatter it.

**Build:**
- **Naïve-baseline ladder:** (a) persistence/recurrence, (b) sector+FX differential-exposure, (c) sparse own-factor event-study. A candidate must **beat the ladder on the same PIT splits** to be promotable.
- **Signal registry:** every candidate logs hypothesis, feature family, train/test dates, trial count, cost assumption, survivorship handling, purge/embargo params, **Deflated Sharpe Ratio** and **PBO**. Promotion gated on `DSR > 0` after trial-count adjustment — not raw in-sample Sharpe.
- **PIT backtester:** purge + embargo, liquidity-screened, with an **explicit per-name cost + borrow model and a capacity curve** (not "net of costs" hand-waving). Produces **three books:** research (frictionless L/S) · venue-feasible (`short_eligible`/borrow/band/halt) · stress (short-ban + crowding + limit-lock).

**Exit gate:** a **known-null** signal (shuffled labels) correctly **fails** the gate (DSR ≤ 0, doesn't beat persistence) · a **known-good toy** signal passes · backtester reproduces a hand-checked toy P&L · all three books produce output. The harness must reject noise before you trust it to accept signal.

---

## M5 — First real signal: residual stat-arb

**Goal:** the first edge through the full harness (only if M3 = GO).

**Build:** residual mean-reversion pairs/baskets from surviving M3 edges → through M4's gate → write filtered `RESIDUAL_CORR` / `LEAD_LAG` snapshots back to L1 (time-stamped, never the dense matrix).

**Exit gate:** survives the **venue-feasible** book (not just research) · `DSR > 0` · clears a stated capacity floor · registry entry complete. If it survives only in the frictionless research book, **it is not real** — log and move on.

---

## M6 — Geopolitical event engine

**Goal:** differential-exposure event signal **and** a risk spine.

**Build:**
- `Event` ingestion (GDELT raw + taxonomy) with date precision; `TARGETS` channel mapping (modeled).
- **Cross-sectional differential exposure** (not single-name event studies — events cluster in Turkey): sort by exposure to the shocked channel, measure high-minus-low spread, test under-reaction drift.
- **Channel stress scenarios** (second output): each major event emits a signed shock vector; re-price the portfolio via the exposure tensor → stress P&L + worst-exposed names.

**Exit gate:** measure how many usable **control names survive a typical event** before trusting the design — a thin cross-section is a down-weight, not a fabrication · spread signal clears M4 · stress P&L reconciles against a hand-checked shock.

---

## M7 — Supply-chain / linkage pillar (last — scarcest data, least-proven premise)

**Goal:** the linked-firm predictability edge — built last because it needs the scarcest data and its premise is **US-documented, not BIST-proven** (treat as a hypothesis to falsify, §10).

**Build:**
- Tiered edges: tier-1 firm-level from KAP `includeNewBusiness` structured contracts (counterparty + amount + materiality) → tier-2 intra-group → tier-3 OECD-ICIO (sector). Confidence decays down tiers; **tier-3 never masquerades as tier-1.**
- Materiality-weighted `SUPPLIES_TO` edges (a 2%-of-revenue customer is not a channel).
- Lagged supplier/customer residual returns → predicted own return; validate as a portfolio sort through M4.

**Exit gate:** lead-lag signal clears M4 in the venue-feasible book · the intra-group-already-priced null is tested and rejected · no tier-3 edge enters a tier-1 traversal path.

---

## M8+ — Hardening & optional advanced layers

GraphRAG / NL-explanation over L1; OpenSanctions enrichment; **GNN overlays only if they clear the M4 gate** (at n ≈ 500 they likely won't — deferred to year 2 deliberately). Continuous: id-bridge monitoring, data-drift smoke checks, registry hygiene.

---

## Live risk register (from design §10 — keep visible, revisit each milestone)

| Risk | Where it bites | Mitigation in this plan |
|---|---|---|
| Residual = disguised flow factor | M3 | M3 is a [STOP] gate, placed early |
| `p ≈ n` covariance instability | M2/M3 | shrinkage + sector-restricted residual cov before inversion |
| Lookahead / restated-data leak | all | PIT wrapper (M0) + PIT-leak detector in every gate |
| Survivorship bias | M1+ | dead names retained; as-of universe test |
| Tradability illusion (thin names) | M4/M5 | venue-feasible + stress books, capacity curve, borrow model |
| Linked-firm effect unproven on BIST | M7 | built last, framed as falsification, intra-group-priced null tested |
| Overfitting across 125k pairs / many knobs | M3/M4 | FDR control + DSR/PBO + purge/embargo |
| Soft-edge provenance decay | M6/M7 | evidence_tier + confidence + uncertainty enforced; no silent promotion |
| id-bridge single point of failure | M0+ | resolver + round-trip test, monitored |

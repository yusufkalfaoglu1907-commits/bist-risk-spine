# ADR-0004 — M5 residual stat-arb = NO-GO (genuine frictionless edge, dies in the venue-feasible book)

- **Status:** Accepted (2026-06-23)
- **Context:** M5 is the first *real* signal through the M4 judge (`BUILD_PLAN.md` M5),
  run only because M3 = GO (ADR-0003: stable residual linkage survives the factor
  strip). The M3 GO came with an explicit caveat carried to M5: *prove it in the
  **venue-feasible** book, not just frictionless research* (edge overlap moderate,
  weight rank-stability low). M5 answers whether the surviving residual structure is
  not just statistically real but **economically tradable net of costs.** This is a
  project-level result (§8): surfaced, not auto-advanced.

## The signal

**Peer-relative residual mean reversion** (`tmkg.signals.statarb`): for each name, the
M3 surviving residual-corr edges define a comove-predicted residual
`r̂_i = Σ_j ρ_ij·r_j / Σ_j|ρ_ij|` (signed); the **dislocation** `s_i = r_i − r̂_i` is
the name's residual *in excess of what its linked peers explain*. The bet fades an
accumulated dislocation level (`−z(Σ s)`), z-scored per name, `shift(1)` for PIT. Using
the *graph* (peers) is what differentiates it from the naïve own-name reversion that is
baseline (c) in the M4 ladder — and on synthetic data it provably beats that baseline.

## What was run

`scripts/run_m5_statarb.py` (as_of 2026-06-15, 200 liquid names by residual coverage,
573-name sector map → 48 sectors). An honest 12-variant grid (`n_trials`=12 over
accumulation horizon × z-threshold × peer-selection strictness), each run through the
**venue-feasible** book to form the DSR/PBO mining family. **Purged walk-forward**
(`n_splits=5, purge=5, embargo=5`) selected the per-fold train-best variant and applied
it out-of-sample (505 OOS test dates) — the candidate is genuinely out-of-sample. Judged
through the full gate (beat-the-ladder · DSR · PBO · venue-feasible) across all three
books + a capacity curve. Report: `data/cache/m5_statarb_report.json`.

## The evidence

| book | net Sharpe (per-period) | note |
|---|---|---|
| research (frictionless) | **+0.074** | a small but genuine edge exists |
| venue-feasible (10 bps + 100 bps/yr borrow + limit-lock) | **−0.052** | costs eat it |
| stress (blanket short-ban + 3× crowding) | **−0.038** | — |

- The candidate **beats the naïve-baseline ladder** (cand −0.052 > best baseline
  `differential_exposure` −0.073) and **PBO = 0.40** (< 0.5) — the relative edge over the
  dumb baselines is not a pure CV artifact.
- But **DSR ≈ 0 (fails)** and the venue net Sharpe is **negative**, so it fails the
  capacity floor. **Not promoted.**
- The finding is **robust, not a selection artifact**: *every* in-sample variant is
  positive in research and negative in venue; the most turnover-efficient variant
  (accum 10 d, turnover 0.25/day) tops out at **−0.026** venue — still negative. Pushing
  the holding horizon further (accum 20–60 d) does **not** cross break-even because the
  reversal is genuinely short-horizon: the *frictionless* edge itself decays as the
  horizon lengthens (research Sharpe 0.076 → ~0.03), so cost reduction is outrun by
  signal decay.

## Decision

**NO-GO.** The residual stat-arb has a small, genuine frictionless edge (consistent with
the M3 GO) but is **economically too thin to survive realistic transaction costs.** Per
the M5 exit gate: *"If it survives only in the frictionless research book, it is not real
— log and move on."* We do **not** weaken the cost model to manufacture a pass — that
would defeat the entire "build the judge before the contestant" architecture (M4).

## Consequences

- The verdict is **recorded in `signal_registry`** (promoted=False, DSR, PBO,
  beat_baselines=True, n_trials=12) — a rejection is as durable as a promotion.
- The **filtered residual-corr snapshot is landed in L2 `residual_corr`** (92 FDR +
  sector-restricted survivor edges; economically sensible — insurance, REIT, retail
  pairs) — valuable structure regardless of the trade verdict, and the substrate any
  future correlation-pillar signal builds on.
- **Cost model not weakened** (CLAUDE.md §8). The 10 bps default stands; the honest
  conclusion is the point.
- **Open avenues if revisited** (not pursued now): (a) the venue short-feasibility map —
  `short_eligible` is empty (M2 blocked on the Matriks foreign-custodian list), so the
  venue book cannot police per-name short bans and the **stress** book is the binding
  short test; landing it would sharpen, not rescue, the verdict; (b) lower realized cost
  on a tighter BIST-30/most-liquid subset; (c) an event-conditioned reversion (only
  trade dislocations around a known shock) — but that is M6 territory.
- **Pillar sequencing unchanged:** the correlation pillar produced a clean residual
  substrate but no standalone tradable alpha at daily/weekly frequency. Proceed to **M6
  (geopolitical event engine)** — the next pillar — carrying the residual_corr snapshot
  as a reusable input.

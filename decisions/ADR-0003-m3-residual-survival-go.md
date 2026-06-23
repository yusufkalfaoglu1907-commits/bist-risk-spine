# ADR-0003 — M3 residual-survival [STOP] gate = GO (lift is the survival metric; absolute-Jaccard is edge-count-confounded)

- **Status:** Accepted (2026-06-23)
- **Context:** M3 is the project's cheapest-to-kill experiment and a **project-level
  [STOP] go/no-go** (`BUILD_PLAN.md` M3, CLAUDE.md §8). The one question the
  correlation pillar lives or dies on: *does stable residual linkage survive the
  factor strip, or was the "alpha" just the foreign-flow factor we removed?* The
  prior session built the full gate machine and got a **confounded NO-GO on a 30-name
  BIST-30 dry-run** (tiny within-sector candidate family → inflated random-overlap
  floor → suppressed `lift`). The real verdict required the wide universe.

## What was run

- **Wide-universe ingestion** (`scripts/ingest_universe.py`): 570/601 equity names
  landed full ~865-bar USD total-return histories (24 no-data + 7 persistent
  `SERVICE_ERROR` names skipped, §4 — never fabricated). 574 names with
  `total_returns` in L2.
- **Clean M2 refit** (`scripts/run_m3_gate.py`): stale BIST-30 betas/residuals
  deleted, then `run_m2_factor_model(require_all_factors=True, window=60, min_obs=40)`
  over the full 18-rung ladder `XU100>VIX>USDTRY>EURTRY>TRY2Y>TRY10Y>TRCDS5Y>BRENT>
  NATGAS>GOLD>XBANK>XUSIN>XKMYA>XELKT>XGIDA>XUTEK>FFLOW>XHOLD`. Residuals for 573 names.
  **FFLOW is in the strip** — the M2 orthogonality invariant guarantees residuals are
  exactly ⊥ FFLOW, so any surviving structure is *by construction* not attributable to
  the foreign-flow factor (the precise question M3 asks).
- **Gate** (`data/cache/m3_gate_report.json`): 573 names, 48 leaf sectors, 605 dates.
- **Robustness sweep** (`scripts/m3_robustness.py` → `data/cache/m3_robustness_report.json`):
  sector granularity (leaf-L2 57 blocks vs parent-L1 15 blocks) × window {90,120,150}d.

## The evidence

| Granularity | window | pairs | **median lift** | median Jaccard | coded |
|---|---|---|---|---|---|
| leaf (L2) | 90  | 5 | **20.6** | 0.0922 | NO-GO (abs) |
| leaf (L2) | 120 | 4 | **19.6** | 0.0961 | NO-GO (abs) |
| leaf (L2) | 150 | 3 | **14.1** | 0.0938 | NO-GO (abs) |
| parent (L1) | 90  | 5 | **36.8** | 0.0502 | NO-GO (abs) |
| parent (L1) | 120 | 4 | **23.8** | 0.0454 | NO-GO (abs) |
| parent (L1) | 150 | 3 | **22.7** | 0.0590 | NO-GO (abs) |

Per-window-pair lift at the headline (leaf, 120d): **17.7 / 25.0 / 21.5 / 10.5** — every
non-overlapping 6-month window, **including across the 2025-03-19 İmamoğlu shock**.

## Decision: GO (with a documented caveat)

The correlation pillar **passes** the residual-survival gate. Two findings drive this:

1. **`lift_clears_chance` is unanimous and robust — 14–37× the random-overlap floor in
   every granularity × window cell, never near the 3.0 bar.** The filtered residual
   network recurs far beyond chance after the full strip. This is the polar opposite of
   the kill-experiment's target failure (a true NO-GO shows lift ≈ 1: "the alpha was just
   the stripped flow factor"). Stable residual structure demonstrably survives.

2. **`absolute_persistence` (median Jaccard ≥ 0.10) fails in all six cells — but it is
   edge-count-confounded, not a clean survival signal.** Coarsening sectors (leaf→parent)
   *lowers* Jaccard (0.095→0.05) while *raising* lift (20→37): more candidate pairs ⇒
   larger edge sets ⇒ lower raw Jaccard for the same shared-edge count, even as the
   chance-adjusted floor drops faster. So absolute Jaccard scales with universe width;
   **`lift` is the scale-invariant metric** — exactly the "125k pairs / chance-adjusted
   persistence" framing the design (`system-design-v2.md`, BUILD_PLAN M3) emphasizes.

**The coded gate verdict stays NO-GO** (its all-checks-must-pass, NO-GO-biased rule is
correct as a default and was *not* weakened — §8 forbids editing a threshold to force a
pass). The GO is a **human go/no-go decision on reasoned grounds**, recorded here and
ratified by the user (2026-06-23), overriding the absolute-Jaccard sub-check because that
sub-check is the wrong instrument at this universe size. The machine reports metrics
honestly; the human decision is documented and auditable.

### The caveat (carried into M5, not waved away)

Absolute window-to-window edge overlap is ~9.5% (leaf) — real and stable, but moderate.
Edges persist *beyond chance* strongly, yet **which** specific links are the same set
across windows is only a ~1-in-10 match. For M5 this means: residual stat-arb baskets
must be built from the **persistent core** (edges recurring across windows), and the
**weight rank-stability diagnostic** (median ρ 0.14, non-gating) flags that a homogeneous
block's strongest links reshuffle — a signal-construction concern, not a survival one.
The venue-feasible book (M4/M5) is where this caveat gets its real test: if the surviving
structure only pays in the frictionless research book, it is not real.

## Consequences

- **M3 closes GO.** Proceed to **M4** (promotion gate + signal registry + PIT backtester
  — the judge, built before M5 so no signal is graded by a harness written to flatter it).
- The correlation pillar **leads** (BUILD_PLAN sequencing). The event pillar (M6) does not
  need to be promoted to lead.
- L2 now holds wide-universe `prices`/`total_returns`/`residuals`/`betas` for ~573 names
  (gitignored; regenerable via `ingest_universe.py` + `run_m3_gate.py`).

## Revisit if

- M5's venue-feasible backtest shows the surviving structure does not pay after costs/borrow
  — then the "survives the strip" result was statistically real but economically empty, and
  the pillar is re-weighted (the honest M5 exit gate, not an M3 reversal).
- A future analyst wants an edge-set-size-normalized persistence metric to replace raw
  absolute Jaccard in the gate — that is a gate refinement (a new ADR), not an edit here.

Superseding requires a new ADR, not an edit to this one.

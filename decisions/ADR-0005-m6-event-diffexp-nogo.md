# ADR-0005 — M6 differential-exposure event signal = NO-GO (structural; not rescuable by residuals / salience / LLM)

- **Status:** Accepted (2026-06-25)
- **Context:** M6 is the geopolitical-event pillar (`BUILD_PLAN.md` M6): a cross-sectional
  **differential-exposure** spread — within an event window, sort names by their exposure
  (M2 factor beta) to the shocked *channel* (factor-ladder role) and trade high-minus-low,
  betting on post-event under-reaction drift (§236). Built on the GDELT GKG feed + the
  taxonomy `TYPE_CHANNEL_PRIOR` (sign-only channel incidence per event type), judged through
  the M4 gate. This is a project-level result (§8): surfaced and **user-ratified**, not
  auto-advanced.

## What was run

Pilot **Turkey GKG backfill** (Q1-2025, full 15-min cadence): **71,665 events / 200,716
`event_targets`** over all 90 days, landed to L2 `events`/`event_targets` (PIT: `event_date =
knowledge_date = V2.1DATE`). Per-doc events deduped to distinct `(event_date, channel,
shock_sign)` cross-sections. `scripts/run_m6_gate.py` (as_of 2025-03-31, panel floored to the
event-active window) ran the differential-exposure spread (12-variant quantile×window×lag grid =
`n_trials`) through purged walk-forward OOS selection + the full gate across all three books +
capacity + the §240 channel-stress second output. Report: `data/cache/m6_event_diffexp_report.json`.

## The evidence

| book | net Sharpe | note |
|---|---|---|
| research (frictionless) | **−0.105** | negative *even frictionless* — no edge, unlike M5 |
| venue-feasible | **−0.180** | — |
| stress | **−0.134** | — |

The candidate **loses to its own static `differential_exposure` baseline (+0.186)**; DSR 0.053
(fails 0.95); median control fraction 0.888 and 52 OOS dates ⇒ **not a thin-cross-section escape
(§238)** — a genuine, judgeable test. Verdict landed in `signal_registry` (`m6_event_diffexp`,
promoted=False).

## Why — a four-agent statistical investigation (the decisive part)

A deep diagnosis traced the negative to a mechanism, and **four parallel read-only research
agents** then tested every candidate fix on the real data. All converge: the NO-GO is **structural**.

- **Factor confounding (Agent A):** the raw-return spread is a disguised market-direction bet —
  corr(spread, market move) **−0.51 raw → +0.02 on residuals**. Measuring drift on **residual**
  (M2-neutralized) returns is the correct base, **but rescues nothing**: every channel's residual
  spread bootstrap-CI includes 0. (Also surfaced: FFLOW betas have *zero* cross-sectional
  dispersion → degenerate sort; M2 residuals end 2025-03-18, missing the 03-19 İmamoğlu crash.)
- **No isolable shock (Agent B):** GKG is a total daily floor — **~796 events/day, 10.6/11 types
  every day, modeled severity saturates to 1.0 every day** — so |tone|-severity carries no
  day-discriminating information and **no salience filter can isolate a shock**. The cross-section
  also lacks a control leg (fx median control fraction 0.17 — ~83% of names highly fx-exposed).
- **Sign attribution / LLM ceiling (Agent D):** dominant-event attribution un-cancels the
  conflicting-sign channels (fx 0→−13, energy +1→+28), but the **oracle per-event sign is a
  hindsight trap** (50–98% of its edge is day-by-day sign-flipping; event content predicts the
  realized cross-sectional direction at **0.33–0.50 = coin-flip**). The realistic ceiling (best
  *fixed* sign) net of cost is **negative on rates_cds, breakeven on fx/foreign_flow**; the only
  positive channels (market, energy) are exactly the raw-confounded ones that vanish on residuals.
- **Inference / power (Agent C):** the diagnosis's large t-stats were **overlap + multiple-testing
  artifacts** — 76 overlapping h=5 windows = only **11 independent**; HAC halves the t-stats,
  non-overlap/bootstrap erase them; **0 of 108 cells survive Bonferroni or BH-FDR**. A 3-month
  window (n_eff≈52) needs an **annualized Sharpe ≈ 4.5** to clear the DSR gate — structurally
  impossible; a credible test needs **≥ 2 years**.

**Root cause:** the §236 design assumes *distinguishable shock events*; GDELT GKG's per-document
daily density ("every day is an event day for every channel") violates that, so the prior-seeded
superposition (i) cancels the informative channels by conflicting signs and (ii) collapses the
survivors into a market-beta bet. Neither residual returns, nor salience filtering, nor LLM
per-event sign extraction can manufacture a distinguishable shock that the data does not contain.

## Decision

**NO-GO** for the channel-beta differential-exposure event signal. It is not a tuning failure: it
is negative even frictionless, the "signal" was an inference artifact, and every candidate fix was
empirically falsified. We do **not** weaken the cost model or the inference standard to manufacture
a pass (§8). The **per-event-sign LLM extraction is explicitly judged not worth building** for this
signal (Agent D oracle); the only valuable LLM direction — **firm/sector-level event targeting** —
is a *different* hypothesis that belongs to the M7 supply-chain/linkage pillar.

## Consequences

- Verdict **recorded in `signal_registry`** (promoted=False) alongside the M5 NO-GO — a rejection
  is as durable as a promotion.
- The **§240 channel-stress risk-spine is delivered and works** (`tmkg.events.channel_stress` +
  `run_event_signal` second output: per-event shock re-priced through the exposure tensor → worst-
  exposed names + stress P&L). M6's **alpha is NO-GO; its risk-spine is a real deliverable.**
- The GDELT ingestion stack (`ingest/gdelt.py` adapter + `ingest_gdelt_events` + the resumable
  `scripts/ingest_gdelt.py` backfill + the smoke golden) and the Q1-2025 `events`/`event_targets`
  in L2 **remain** — reusable substrate for any future event work.
- A **durable honest-evaluation protocol** for event signals is recorded (BUILD_LOG 2026-06-25):
  non-overlapping observation unit; HAC + moving-block bootstrap; full search space in the DSR
  `n_trials`; ≥2-year window; residual returns; a real control leg + factor-move confirmation.
- **Two of three pillars (correlation M5, event M6) are now NO-GO on real data** through the
  venue/honest gate. The cost model and statistical standard held in both — the architecture is
  doing its job (rejecting plausible-but-unreal signals cheaply).
- **Open avenues if revisited** (not pursued now): discrete/clustered shock events from dated
  announcement sources (KAP / CBRT / economic calendar) where a shock day is genuinely rare;
  firm/sector-level event targeting via LLM (→ folded into M7); a ≥2-year GDELT window under the
  honest protocol.
- **Pillar sequencing:** proceed to **M7 (supply-chain / linkage pillar)** — the last and
  scarcest-data pillar — where the firm/sector-level LLM targeting the agents endorsed naturally
  lives. Carry the GDELT event substrate + the channel-stress spine forward.

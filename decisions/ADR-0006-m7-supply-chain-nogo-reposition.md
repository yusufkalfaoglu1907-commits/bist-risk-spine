# ADR-0006 — M7 supply-chain/linkage = NO-GO (all tiers); cross-sectional alpha search concluded; deliverable repositioned as research substrate + risk spine

- **Status:** Accepted (2026-06-27)
- **Supersedes:** nothing. **Concludes** the three-pillar alpha thesis opened by `system-design-v2.md` and sequenced in `BUILD_PLAN.md` (M3→M7).
- **Context:** M7 is the supply-chain / linkage pillar (`BUILD_PLAN.md` M7) and the **last and scarcest-data** of the three alpha pillars — the linked-firm predictability edge, which is **US-documented but not BIST-proven** and was scoped from the start as *a hypothesis to falsify* (design §10 risk register: "linked-firm effect unproven on BIST"). With M5 (correlation) and M6 (event) already NO-GO under the M4 venue/honest gate, M7's verdict is also the **project-level go/no-go** for the whole cross-sectional firm-linkage alpha program (§8). This ADR records the user's ratification (2026-06-27) — nothing was auto-advanced.

## What was run (all three tiers, cheapest-falsification-first per M0 discipline)

**Tier-1 — firm-level KAP new-business — NO-GO on DATA FEASIBILITY (fails before M4).**
The tier-1 spine assumed Matriks `includeNewBusiness` would deliver structured KAP "Yeni İş İlişkisi"
rows (counterparty + amount + materiality `rt`/`marValRt`, per `data-sourcing-v2.md` W6). On the
contract-legal **REST** path that flag is **ignored** by the gateway — the clean fields seen in the
June *interactive-MCP* probe were the MCP's post-parsed convenience output, not the REST contract.
Disclosures arrive as KAP news HTML (`newsHeadlineSearch="iş ilişkisi"`, `newsSource=["KAP"]`,
`newsCount<=100`). `scripts/probe_m7_newbusiness.py` parsed **290 disclosures (Jan-2025…Jun-2026)**:
unnamed 33% / government 12% / foreign 14% / anonymized 13% / named-domestic 28%. The token-matcher's
**upper bound** of listed-independent counterparties is 13.1%, but manual inspection leaves **~1 genuine
independent listed-BIST counterparty per 100 disclosures** (the rest are generic-token false positives
or intra-group = the already-priced null). Materiality disclosed in only 13%; amounts free-text only.
Market-wide that is **~10–15 tradeable listed-to-listed firm-level edges/yr, almost all one-off** —
structurally far too sparse for the cross-sectional differential-exposure sort tier-1 requires. Exactly
the W6 caveat. Report: `data/cache/m7_newbusiness_coverage_report.json`. (User chose, via §8
AskUserQuestion, to drop firm-level and pivot to tier-3.)

**Tier-2 — intra-group — NO-GO by construction.** Intra-group links (a holding and its listed
subsidiaries) are exactly the **already-priced null** the M7 gate exists to reject: the relationship is
public, static, and continuously arbitraged, so any "lead-lag" is mechanical co-movement already in the
factor/sector structure, not a tradeable surprise. Not a separate empirical run — it is the null,
not the alternative.

**Tier-3 — sector-IO lead-lag — NO-GO on OOS PREDICTABILITY.** The cheapest kill of the tier-3
*premise* **before** sourcing OECD-ICIO: does any stable sector-level lead-lag survive OOS in the M2
residual returns? `scripts/probe_m7_sector_io.py` built 21 sector residual series (≥8 names/day, 604
trading days) from L2 `residuals` × graph `IN_SECTOR`, train/test split, both a generic ridge-fit full
lead-lag matrix and the specific textbook supplier→customer IO pairs (energy→industry, base-metal→
metal-goods, chemicals→textile, cement→construction…), at daily/weekly/biweekly/monthly horizons.
Report: `data/cache/m7_sector_io_feasibility_report.json`.

| horizon | in-sample | generic OOS IC | textbook pairs train-confirmed |
|---|---|---|---|
| daily (n=604) | IC +0.24 (t=18) | **−0.005 (t=−0.49)** | 0 / 8 |
| weekly (n=120) | — | +0.003 | 0 / 8 |
| biweekly (n=60) | — | +0.017 | 0 / 8 |
| monthly (n=28, 13 OOS blocks) | — | +0.037 | 3 / 8 — but sample-starved |

Dead at every horizon. The in-sample IC of +0.24 collapses to **OOS ≈ 0**; top in-sample lead-lag pairs
agree OOS at coin-flip (7/15); the only mildly-positive horizon (monthly, where customer-momentum should
peak) rests on **13 independent OOS blocks** — uninferable, point estimates zero-to-negative, textbook
mean OOS corr −0.097. **OECD-ICIO cannot rescue this:** it only re-weights sector-pair lead-lags that
themselves carry no stable OOS signal, and an honest monthly test needs ~10+ years (we have 3). Same wall
as M6's small-sample / overlap-artifact diagnosis.

## Decision

1. **M7 supply-chain/linkage is NO-GO** across all three tiers — tier-1 on data feasibility, tier-2 as
   the already-priced null, tier-3 on OOS predictability. Not a tuning failure: tier-1 fails on a hard
   data wall, tier-3 fails out-of-sample at every horizon.
2. **The cross-sectional firm-linkage alpha search is CONCLUDED.** All **three pillars are now NO-GO** on
   real BIST data through the venue/honest M4 gate: **M5 correlation** (ADR-0004 — a genuine frictionless
   residual edge too thin to survive 10bps + borrow), **M6 event** (ADR-0005 — structurally dead, the
   apparent signal an overlap/multiple-testing artifact), **M7 supply-chain** (this ADR). Tradeable
   cross-sectional alpha on BIST residual returns appears **exhausted** at n≈500 over the available window.
3. We do **not** weaken the cost model or the inference standard to manufacture a pass, and we do **not**
   build the OECD-ICIO ingestion + BIST crosswalk: it re-weights lead-lags that carry no OOS signal, so it
   would spend a multi-session sourcing effort to re-confirm a null (§8 — no invariant weakened to pass).
4. **Reposition the deliverable.** The project's output is **not** a live alpha book; it is an **honest
   research substrate + risk spine**: a PIT/bitemporal Turkish-equities research engine that (a) measures
   exposures and residual structure correctly, (b) **cheaply and credibly rejects** plausible-but-unreal
   signals, and (c) re-prices shocks through the exposure tensor. A rigorously-established NO-GO across
   three pillars **is** the result — it is the answer to "is there tradeable firm-linkage alpha here," and
   it is worth more than an overfit book that would have lost money live.

## Consequences

- **No L2 write of an M7 signal verdict** — unlike M5/M6, M7 never reached the M4 gate (tier-1 died on
  data feasibility, tier-3 on a pre-gate OOS feasibility probe), so there is no `signal_registry` row to
  land. The two probe scripts + two JSON coverage reports are the durable, reproducible evidence.
- **The substrates are the deliverable, and they are real and trustworthy:**
  - PIT/bitemporal L1 graph + the PIT data-access wrapper (the honest-backtest guarantee).
  - USD-primary corporate-action-adjusted total-return series; limit-lock censoring; `accounting_regime`
    / `short_eligible` state machines (M1).
  - The M2 factor/residual machine (19 factor series, foreign-flow stripped) + the M3 residual-survival
    substrate and its non-degeneracy / emittable-coverage guards.
  - The **M4 promotion judge** (DSR + PBO/CSCV, PIT backtester with purge/embargo/cost+borrow/3 books/
    capacity, baseline ladder + AND-gate, L2 `signal_registry`) — the component that earned its keep by
    rejecting M5 and M6 cheaply.
  - The **M6 §240 channel-stress risk-spine** (`tmkg.events.channel_stress` + `run_event_signal` second
    output): per-event shock re-priced through the exposure tensor → worst-exposed names + stress P&L.
    This is a *delivered, working* risk product independent of the dead alpha.
  - Reusable data substrate: the GDELT event stack + Q1-2025 `events`/`event_targets`; the residual_corr
    snapshot (M5); tier-1 new-business as an **exposure annotation** (a known-counterparty tag), never a
    tradeable edge.
- **A durable honest-evaluation protocol** is now battle-tested across three pillars and recorded
  (BUILD_LOG 2026-06-25): non-overlapping observation unit · HAC + moving-block bootstrap · full search
  space in the DSR `n_trials` · ≥2-year window · residual returns · a real control leg. This is reusable
  IP for any future signal test.
- **The architecture did its job.** Three plausible alpha theses were each tested and rejected *cheaply*,
  before any capital, without weakening a single invariant in CLAUDE.md §5. A research substrate that can
  kill a bad signal for the cost of a probe script is the asset.
- **Open avenues if ever revisited** (explicitly **not** pursued now): a ≥2-year (ideally ≥10-yr for the
  monthly horizon) window that could re-power the tier-3 / event tests; discrete, genuinely-rare dated
  shock sources (KAP / CBRT / economic calendar) for a firm-level event study; the linkage graph used
  **non-cross-sectionally** (descriptive network / GraphRAG / explanation over L1, M8 scope) rather than
  as a return predictor.

## Status of the build going forward

The three-pillar alpha program is **closed**. There is no M8 alpha milestone queued. Remaining work, if
any, is **hardening / non-alpha use** of the substrate (M8: id-bridge monitoring, drift smoke checks,
registry hygiene, optional GraphRAG/NL-explanation over L1) — to be opened only on explicit user
direction, not auto-advanced. The repo is left GREEN (`make verify` 395 passed / 4 skipped; `make smoke`
PASS) with no fabricated data and no weakened test.

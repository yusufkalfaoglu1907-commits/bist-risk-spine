# BİST Risk Spine & Point-in-Time Research Substrate

A research engine over **Turkish equities (BİST, ~500 names)** that treats correlation,
geopolitical events, and supply-chain links as three views of one object — how a shock
propagates through ownership and factor exposure to a price.

Its distinguishing feature is not a winning trade. It is an **honest evaluation harness**
rigorous enough to *reject* plausible-but-unreal signals before any capital is committed —
and a **risk spine** that re-prices shocks through the graph. Built on a strictly
point-in-time, bitemporal core so that every backtest is reproducible and leak-free.

> **Result, stated plainly:** the original goal was a tradeable cross-sectional alpha signal
> across three pillars. All three were built and run through the same venue-feasible,
> Deflated-Sharpe / PBO promotion gate, and **all three returned NO-GO** — a genuine but
> too-thin correlation edge, a structurally dead event signal, and a supply-chain layer with
> no out-of-sample predictability. A rigorously established NO-GO *is* the deliverable: it is
> worth more than an overfit book that loses money live. The substrate and risk spine that
> proved it are the durable assets.

---

## Why this is interesting

Most "quant" portfolios show a backtest with a beautiful equity curve. This one shows the
opposite discipline: **the machinery that kills beautiful equity curves that aren't real.**

- **A promotion judge that cannot be fooled cheaply.** Deflated Sharpe Ratio + Probability of
  Backtest Overfitting (PBO via CSCV), a purged/embargoed walk-forward backtester with
  transaction costs *and* borrow, evaluated across three books (research → venue-feasible →
  stress). It rejected two live candidates and never had its cost model weakened to manufacture
  a pass.
- **Invariants engineered in from the first commit**, not bolted on: bitemporal point-in-time
  reads (nothing with `knowledge_date > as_of` is ever returned), USD-primary corporate-action
  adjusted total returns, limit-lock censoring, an `accounting_regime` state machine (IAS-29 is
  suspended by law FY2025–2027), per-name-per-date short eligibility, and a foreign-flow factor
  in the core set so flow-driven comovement can't masquerade as linkage.
- **Fail-loud, never fabricate.** If a data source is unreachable the adapter stops — it never
  interpolates or invents market data. Every ingestion run writes a JSON audit report.

## Architecture

A strict layered data contract: **only the ingestion layer touches the network.** Everything
downstream reads a local cache, so no backtest depends on a live connection.

```
  Matriks · EVDS · KAP · GDELT · GLEIF   (network — ingestion layer only)
            │
            ▼
  ingestion adapters ──▶ local cache
            │            L1  KuzuDB graph  (identity, ownership, control, sectors)
            │            L2  DuckDB + Parquet  (prices, returns, factors, residuals)
            ▼
  factor · correlation · event · risk · backtest engines   (never hit the network)
            │
            ▼   all reads pass through the point-in-time wrapper (requires an as_of date)
```

- **L1 — structural graph (KuzuDB):** embedded, local-first, no server. 802 companies with
  ownership/control/sector edges. ([ADR-0001](decisions/ADR-0001-graph-store.md))
- **L2 — quant store (DuckDB + Parquet):** prices, returns, factors, betas, residuals — never
  stored as graph properties. At ~500 names this is the right scale.
- **L3 — signal & risk (Python):** factor/residual machine, the promotion judge, the risk
  spine — all behind the point-in-time wrapper.

## The identity graph

The foundation is a trustworthy entity/ownership core. Identity is **confidence-tiered** —
ambiguous cases are logged for review, never guessed:

- **802 `Company` nodes** (730 ticker-bearing); ISIN and sector **100%** on equity-traded names.
- **LEI coverage 92%**, matched via a diacritic-aware, brand-token-scored GLEIF join that
  refuses low-confidence matches rather than inventing them.
- **212 verified `CONTROLS` edges**, forming a directed acyclic control graph.
- Delisted / merged / renamed names stay in the graph with dead histories (survivorship-safe);
  sector membership is time-varying.

## The risk spine

The part that graduated to a standing deliverable:

- **Scenario re-pricing** — a macro channel shock (e.g. a rate or FX move) re-priced through the
  exposure tensor to surface the worst-exposed names and a stress P&L.
- **Linkage propagation** — an idiosyncratic shock to one name cascaded through the
  ownership/control graph (look-through + blast radius).
- **Three standing health monitors** — id-bridge integrity, data-source drift, and signal-registry
  hygiene — that make the substrate's single points of failure observable and regression-guarded.

```bash
# re-price a channel shock as of a date, over an event window
PYTHONPATH=src python scripts/run_scenarios.py 2026-06-15 2025-03-18 2025-03-25
# cascade a -20% shock to ARCLK through the ownership graph
PYTHONPATH=src python scripts/run_linkage_shock.py ARCLK:-0.20
```

## Repository layout

```
src/tmkg/
  pit/         point-in-time / bitemporal access wrapper + id-bridge resolver
  l2/ ingest/  DuckDB+Parquet quant store + network ingestion adapters (audited)
  returns/     USD-primary corporate-action-adjusted total-return series
  factors/     factor / neutralization / residual machine (foreign-flow stripped)
  signals/     the promotion judge (DSR, PBO/CSCV, PIT backtester, signal registry)
  events/      GDELT ingestion + channel-stress engine
  risk/        scenario re-pricing + linkage-graph propagation
  monitor/     id-bridge, data-drift, and registry health monitors
tests/         invariant, reconciliation, and golden-master suite
scripts/       reproducible ingestion / gate / risk runners
decisions/     append-only ADRs — one consequential decision each
docs/          design, data-sourcing, build plan, and full session journal
```

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# build the identity/ownership core (offline fixtures)
PYTHONPATH=src python scripts/build_phase1.py --db ./data/tmkg.kuzu --fresh

# run the verification suite (invariants, reconciliation, golden masters)
PYTHONPATH=src python -m pytest tests/ -q
```

Ingestion scripts (`scripts/ingest_*.py`) refresh the local L1/L2 caches from live sources;
signal and risk code then reads only the cache. Live-source tests auto-skip when offline.

## The three pillars — all tested, all NO-GO

| Pillar | Verdict | Why |
|---|---|---|
| **Asset correlation** | NO-GO ([ADR-0004](decisions/ADR-0004-m5-residual-statarb-nogo.md)) | A genuine *frictionless* residual edge, but too thin to survive 10 bps + borrow in the venue-feasible book. The cost model was not weakened to force a pass. |
| **Geopolitical events** | NO-GO ([ADR-0005](decisions/ADR-0005-m6-event-diffexp-nogo.md)) | Structurally dead — negative even frictionless; the apparent significance was an overlap / multiple-testing artifact (nothing survives FDR). Its channel-stress *risk* output survives as the risk spine. |
| **Supply-chain linkage** | NO-GO ([ADR-0006](decisions/ADR-0006-m7-supply-chain-nogo-reposition.md)) | Firm-level disclosures too sparse (~10–15 tradeable listed-to-listed edges/yr), intra-group is the already-priced null, sector-IO has no out-of-sample signal. |

**Net:** tradeable cross-sectional alpha on BİST residuals appears exhausted at this scale
(n ≈ 500). The honest-evaluation protocol that killed three plausible signals cheaply — before
any capital and without weakening an invariant — is itself the reusable asset.

## Design & decisions

- **[docs/system-design-v2.md](docs/system-design-v2.md)** — the authoritative design and the
  exposure-tensor idea it's built around.
- **[decisions/](decisions/)** — append-only ADRs recording each consequential fork.
- **[docs/data-sourcing-v2.md](docs/data-sourcing-v2.md)** — verified sources and their walls.
- **[docs/BUILD_LOG.md](docs/BUILD_LOG.md)** — the full append-only session journal, kept for
  transparency into how the conclusions were reached.

---

*Personal, non-commercial research. Vendor reference PDFs (Matriks, GDELT codebooks) are used
locally under their own terms and are intentionally not redistributed in this repository.*

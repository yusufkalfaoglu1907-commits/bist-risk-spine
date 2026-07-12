# Finance KG v2 — System Design

**Purpose:** Map asset correlations, supply-chain dependencies, and geopolitical-event impact across BIST-listed equities (~500 names), in service of **alpha/signal generation**.

**Scope of this document:** the *system and data model only*. What data actually exists and where to source it is deliberately out of scope — that is the next chat's job. Here, every data requirement is tagged by *expected* availability so the sourcing research has a target list, but no design choice is constrained by it.

> **Revision v2.1 (2026-06-19) — hardening pass.** Surgical changes after evaluating two external research reviews (see `v2.1-hardening-memo.md`). The architecture is unchanged. Edits: (a) §3 hyperinflation rewritten from a one-time FY2023 boundary to an **accounting-regime state** — inflation accounting is now suspended FY2025–2027 by law; (b) §3 short-sale updated to a per-name/per-date **state** after the 2025 ban toggled 6×; (c) §4.2 soft edges gain an uncertainty field + verified/inferred tier split; (d) §7.1 pins an explicit **neutralization order** and sector-restricted residual covariance; (e) §7.3 event engine now emits **channel stress scenarios** as a second output; (f) §9 adds a **signal registry** (Deflated Sharpe + PBO) and a mandatory **naive-baseline-ladder gate**; (g) §10 adds the three-book framing and the residual-survival / capacity / linked-firm-unproven risks.

### Locked decisions (inputs to this version)

- **Frequency:** daily. No intraday requirement.
- **Return base:** **USD-primary**, with CPI-real-TRY as a cross-check (§3).
- **Special securities:** REITs (GYO), investment trusts, holding companies, and ETFs are **segmented into their own sub-universes** with separate factor models, but stay in the graph for cross-exposure and group-contagion (§4.3).
- **Signal construction:** **long-short** (ideal-case alpha isolation). Tradability caveat under BIST short-sale constraints is tracked as a live risk (§10).

---

## 0. The one idea that makes this coherent

Your three goals look like three datasets. They are not. They are three views of **one object**: how a shock to one thing propagates to an equity's price. Build three disconnected datasets and you get a database. Build the shared object and you get a system.

That object is the **Exposure Tensor**: `Company × Channel × Time`, where a *Channel* is anything a price can be exposed to — a macro factor (USD/TRY, Brent, rates, CDS), a geography (revenue/assets by country), a commodity input, or a counterparty (a specific customer/supplier).

Everything reduces to it:

- **Correlation** between two stocks = *shared channel exposure* (systematic comovement) + *direct linkage propagation* (supply chain) + idiosyncratic noise. The interesting part is what's left after you remove shared channels — the **residual** linkage.
- **Geopolitical impact** = an event shocks one or more *Channels*; the shock propagates through the tensor and the linkage graph to specific equities, weighted by their exposure.
- **Supply chain** = the direct-linkage layer that *both* creates counterparty channels *and* provides the propagation paths along which shocks and returns travel.

If you remember one thing from this design: **do not store raw pairwise correlations as graph edges.** Raw correlation networks among 500 stocks are dense, unstable, and mostly rediscover "everything moves with USD/TRY and BIST100." That is noise dressed as structure. The signal is *residual / conditional* linkage after common channels are removed. This single decision is the difference between a sophisticated system and an expensive heatmap.

---

## 1. The alpha thesis (so the design has a target)

You chose alpha generation. A design optimized for alpha must name its edge. This system targets three well-grounded, distinct return-predictability mechanisms:

1. **Economically-linked-firm predictability.** Returns of a firm's suppliers/customers predict its own returns with a lag, because investors are slow to propagate news across the value chain (the Cohen–Frazzini / Menzly–Özbaş effect). This is the supply-chain pillar's payoff. *It needs the scarcest data — see risks.*
2. **Residual-comovement stat-arb.** After stripping common factors, clusters of residually-correlated names reveal pairs/baskets that mean-revert. This is the correlation pillar's payoff and needs only price + factor data — the most available.
3. **Differential-exposure event drift.** Around a geopolitical event, names with high vs low exposure to the shocked channel diverge, and the market under-reacts at first. This is the geopolitics pillar's payoff.

Naming these matters because they impose a hard requirement most KG projects skip: **point-in-time correctness** (Section 6). An alpha system whose graph cannot say *"what did we know on date D"* cannot be honestly backtested. Everything below is built so that constraint holds.

---

## 2. Design principles

1. **Separate structure from quantity.** A property graph is excellent for entities and stable relationships (ownership, supply, sector, exposure edges, events). It is *terrible* for dense numeric time series (prices, returns, rolling correlations). Use two stores, not one. (Section 5.)
2. **Exposure is a first-class edge, not a derived afterthought.** `Company —EXPOSED_TO→ Channel` with a *signed* magnitude is the connective tissue of all three pillars.
3. **Strip common factors before you call anything a "linkage."** Comovement = systematic + residual. Only residual comovement is informative for alpha; systematic comovement is just shared beta.
4. **Bitemporal by default.** Every edge and attribute carries *valid time* (when it was true in the world) and *transaction time* (when we learned it). Non-negotiable for backtesting and for a market with frequent name changes, M&A, and delistings.
5. **Provenance and confidence on every soft edge.** Supply-chain and event links are uncertain. An edge without a source and a confidence score is a liability, not an asset.
6. **Turkey-specific accuracy is not optional** (Section 3). Nominal-TRY returns are distorted by inflation and FX; FX is often *the* dominant factor for Turkish equities, not a side variable.
7. **Graceful degradation.** Where firm-level data is missing, fall back to holding-group, then sector input-output structure — with confidence decaying down the tiers.

---

## 3. Turkey-specific accuracy requirements

These are the things a generic equity-graph design gets wrong and that would quietly corrupt every signal. They are not caveats — they are the **immunity spec**. Each is engineered in, not flagged and ignored.

- **Return base = USD-primary.** All factor models, correlations, and event studies run on **USD-converted total returns**, with **CPI-real-TRY as a parallel cross-check** (signals that hold in only one base are treated as fragile). Nominal TRY is retained for reference only. Rationale: in a 40–75% inflation regime nominal returns conflate price moves with currency debasement; USD is market-observable, has no CPI-revision lag, and matches a foreign-flow-driven tape. CPI-real captures domestic purchasing power for domestic-demand names, hence the cross-check.
- **FX as a primary factor.** USD/TRY and EUR/TRY belong in the *core* factor set alongside the market index. Many BIST names split cleanly into FX winners (exporters, FX-revenue) and FX losers (FX-debt, import-cost) — that split is itself a tradeable exposure dimension.
- **Energy import dependency.** Turkey imports nearly all its hydrocarbons. Brent/natural-gas and the current-account channel hit the whole market and specific sectors (airlines, utilities, chemicals, cement) differently. Commodity channels must be explicit.
- **Regime breaks.** 2018 and 2021–2023 FX crises, plus episodic CBRT regime shifts, break the assumption of stable betas. Estimation must be **rolling and regime-aware**; a single full-sample beta is meaningless here.
- **Holding-group structure.** Koç, Sabancı, and peers run material *intra-group* trade and cross-holdings. Group-internal supply edges are both more discoverable and more economically real than arm's-length ones — model `HoldingGroup` explicitly.
- **Survivorship.** Delisted / merged / renamed tickers must stay in the graph with their dead histories. Dropping them inflates every backtest.
- **Price-limit censoring.** BIST imposes daily price limits (±10%, widened via market-maker), so a name that "wants" to move 30% prints +10% three days running — a *censored* return. Raw daily returns on limit-lock days are not true returns; treating them as such corrupts betas, correlations, and event CARs. **Detect limit-lock sequences and use cumulative returns across the lock window** (or a Tobit-style censored-return adjustment). Flag every limit-locked observation.
- **Inflation accounting is a *regime state*, not a one-time boundary (TMS 29 / IAS 29).** Turkish issuers applied **inflation-adjusted** reporting for **FY2023–FY2024**, but parliament **suspended** the requirement for **FY2025–FY2027** (annual and interim; president may extend a further three years; banks/leasing/factoring/financing were already carved out by the BDDK). So comparability breaks at **two** switches — nominal→IAS 29 at FY2023 *and* IAS 29→suspended at FY2025 — and "post-2023 statements are inflation-adjusted" is no longer true going forward. Carry an `accounting_regime ∈ {nominal_pre2023, ias29_2023_2024, suspended_2025_2027, …}` state on every fundamental datum; never compute a growth/intensity/materiality figure that straddles *either* switch without converting to a common basis. This is tractable because the price vendor serves **both** as-reported and inflation-adjusted figures per quarter (see sourcing §9), so the regime state selects the comparable basis rather than forcing a restatement.
- **Corporate actions: bonus shares & rights.** Turkish issuers do frequent **bedelsiz (bonus) issues** and rights offerings. Total-return construction must adjust for bedelsiz, rights, and splits correctly, or price series show fake gaps that masquerade as returns. This is a hard requirement on the price data, not a nicety.
- **Foreign-flow factor.** A large share of BIST comovement is driven by **foreign investor flows**, largely independent of fundamentals; high-foreign-ownership names move together on risk-on/off regardless of sector. Include a **foreign-flow / ownership-tier factor** in the core factor set, or its comovement leaks into "residual" linkage and generates false supply-chain signals.
- **Trading halts & stale prices.** Single-stock halts and thin trading produce **stale prices** — last-trade carried forward. Stale prices fake low volatility and low correlation. Use lagged-beta corrections (Dimson / Scholes–Williams) and a staleness flag; screen names with excessive non-trading out of correlation estimation.
- **Short-sale availability is a per-name/per-date *state*, and it moves fast.** Shorting on BIST is restricted and **periodically banned outright during stress**. This is not an occasional caveat: in 2025 alone the regime toggled at least six times — partial lift Jan 2 (BIST-50), full reimposition Mar 23 after the İmamoğlu detention, four extensions, final lift Aug 29. It bounds tradability *exactly* when event/stat-arb signals peak. The (long-short) signal design is unchanged, but model a `short_eligible` state per name per date and treat the **venue-feasible book** (§10.5) as a first-class output, not a footnote.

---

## 4. The ontology (structural graph)

### 4.1 Node types

| Node | Role | Key identifiers / attributes |
|---|---|---|
| **Issuer** | Legal company entity | ISIN, KAP `mkkMemberOid`, LEI, name (+ historical names) |
| **Security** | A tradable share class; **price series attach here, not to Issuer** | ticker, ISIN, class, listing/delisting dates |
| **HoldingGroup** | Conglomerate parent (Koç, Sabancı…) | name, group LEI |
| **Sector / Subsector** | Existing KAP taxonomy (`SUBSECTOR_OF`) | code, level |
| **Product** | Output a firm sells | name, HS/NACE code |
| **Input / Commodity** | Raw material / energy consumed | name, commodity code, reference price ptr |
| **Facility** | Plant / site | geo-coordinates, type, capacity |
| **Geography** | Country / region | ISO code, region |
| **Counterparty** | Customer/supplier that may be non-listed or foreign | name, country, type |
| **Factor** | Observable macro series | USD/TRY, EUR/TRY, Brent, gas, TRY 2y/10y, Turkey 5y CDS, MSCI EM, gold, VIX, BIST sector indices, **foreign-flow / ownership-tier factor** |
| **Event** | A dated geopolitical/macro occurrence | date(+precision), actors, geography, severity, type |
| **EventType** | Event taxonomy node | category hierarchy |
| **Index / Basket** | BIST30/100, sector indices, ETFs | constituents (via edges), flows |
| **Person** *(optional)* | Exec/board, for interlocks | name, role |

### 4.2 Edge types (the important part)

Every edge carries: `source`, `confidence ∈ [0,1]`, `valid_from`, `valid_to`, `knowledge_date`. Soft edges additionally carry `evidence_tier ∈ {verified, inferred}` and an `uncertainty` (dispersion) field alongside the point `confidence`. Two rules follow: (1) an **inferred** edge (NLP-extracted, link-predicted, proxy-derived) is **never** silently promoted into a `verified` traversal path — the propagation engine queries the tiers explicitly and the alpha layer weights inferred edges down; (2) the portfolio layer sizes by `uncertainty`, not just by point score, so a high-score/high-dispersion edge does not get a full-size bet.

**Structural / direct linkages**

- `(Issuer)-[:SUPPLIES_TO]->(Issuer|Counterparty)` — `product`, `materiality_to_supplier` (% of supplier revenue), `materiality_to_customer` (% of customer COGS), `tenure`. Directionality is the whole point; store the arrow from seller → buyer.
- `(Issuer)-[:COMPETES_WITH]->(Issuer)` — `market`, `overlap_score`.
- `(Issuer)-[:CONTROLS]->(Issuer)` — `stake_pct`, `voting_pct`.
- `(Issuer)-[:PART_OF_GROUP]->(HoldingGroup)`.
- `(Security)-[:MEMBER_OF]->(Index)`.

**Exposure edges (the connective core)**

- `(Issuer)-[:OPERATES_IN]->(Geography)` — `revenue_pct`, `assets_pct`, `sign`.
- `(Issuer)-[:CONSUMES]->(Input)` — `cost_intensity` (% COGS), `price_elasticity`.
- `(Issuer)-[:SELLS]->(Product)`; `(Issuer)-[:LOCATED_AT]->(Facility)-[:IN]->(Geography)`.
- `(Issuer|Security)-[:EXPOSED_TO]->(Factor)` — `beta` (signed), `method` (`disclosed` | `regression` | `structural`), `r2`, `half_life`, `regime`. **This edge is partly structural (from disclosures/segments) and partly derived (from regression).** Keep both, tagged by method, and reconcile.

**Event edges**

- `(Event)-[:TARGETS]->(Geography|Sector|Factor|Commodity)` — the event's primary incidence (modeled, not price-derived).
- `(Issuer)-[:AFFECTED_BY]->(Event)` — `estimated_CAR`, `channel`, `confidence` (**derived** in the signal layer, written back).

**Derived quantitative edges (written back from the signal layer, time-stamped snapshots, filtered)**

- `(Security)-[:RESIDUAL_CORR {window, value, p, sign}]->(Security)` — stored **only** when it survives a statistical + economic filter (graphical-lasso edge, or PMFG/MST membership). Never the dense matrix.
- `(Security)-[:LEAD_LAG {lag, strength, method}]->(Security)` — directional predictability (the alpha edge).

### 4.3 Universe segmentation (special securities)

Every `Security` carries a `universe_class ∈ {operating, gyo_reit, holding, investment_trust, etf}`. Factor models, residual-correlation networks, and event studies are **fit per class**, because NAV- and leverage-driven returns (GYO, trusts, ETFs) obey a different generating process than operating equities and would otherwise distort the shared covariance.

Critically, the classes are **segmented for estimation but not severed in the graph**: a `holding` issuer keeps its `CONTROLS` / `PART_OF_GROUP` edges so holding-company group-contagion (Koç, Sabancı) still propagates, and a `gyo_reit` keeps its `OPERATES_IN` / `LOCATED_AT` edges so property-geography exposure still routes geopolitical shocks. Segmentation governs *where a name's returns enter a statistical estimate*, not whether it exists as a node.

---

## 5. Architecture (three layers)

```
┌─────────────────────────────────────────────────────────────┐
│  L1  STRUCTURAL GRAPH  (property graph, bitemporal)          │
│      entities · linkages · exposures · events · provenance   │
│      → traversal, explanation, propagation paths             │
└───────────────▲─────────────────────────────────┬───────────┘
                │ writes back filtered             │ reads structure
                │ derived edges + signals          ▼
┌───────────────┴─────────────────────────────────────────────┐
│  L3  DERIVATION / SIGNAL LAYER  (Python)                     │
│      factor models · residual returns · correlation          │
│      filtering · event studies · propagation signals ·       │
│      point-in-time backtester                                │
└───────────────▲─────────────────────────────────────────────┘
                │ reads/writes numeric series
┌───────────────┴─────────────────────────────────────────────┐
│  L2  QUANT STORE  (columnar / array)                         │
│      prices · total returns · volume · factor series ·       │
│      betas · residuals · CARs · rolling corr snapshots       │
└─────────────────────────────────────────────────────────────┘
```

**Why split L1/L2:** the graph answers *"who is connected to whom and how, and what did we know when."* The quant store answers *"what were the numbers."* Forcing daily return vectors into graph properties destroys both query performance and analytical flexibility. Correlations and betas are *computed* in L3 against L2 and only their *filtered conclusions* land back in L1 as edges.

**Tech (recommendation, not requirement — revisit in the data chat):**

- **L1:** keep a property graph (Neo4j / Memgraph). Bitemporality via `valid_from/valid_to/knowledge_date` convention on every edge, or a natively-bitemporal store (XTDB/Datomic) if you want it enforced rather than disciplined.
- **L2:** columnar — DuckDB + Parquet for research scale (500 names is small); ClickHouse/TimescaleDB only if you go intraday/high-frequency.
- **L3:** Python (pandas/polars, statsmodels, scikit-learn, `scikit-network`/`networkx`), with a strict point-in-time data-access wrapper.

Your existing graph work (identifiers, sector taxonomy, CONTROLS group-contagion, KAP/GLEIF adapters) maps cleanly onto **L1** and is largely reusable. The debt-blast-radius analytics is the template for L3 propagation, repointed from debt to supply/exposure.

---

## 6. Point-in-time integrity (the make-or-break for alpha)

A backtest is only honest if, on each historical date, the system sees *only what was knowable then*. Three failure modes to design against:

1. **Lookahead via restated data.** Financial statements get restated; segment disclosures arrive months after period-end. Store the *publication date*, and have the backtester query by `knowledge_date ≤ D`.
2. **Survivorship.** Keep delisted/merged issuers and their dead price series. Index membership is itself time-varying — store `MEMBER_OF` with valid intervals.
3. **Edge vintage.** A supply-chain edge "discovered" from a 2024 annual report must not be visible to a 2021 backtest date. Bitemporality handles this automatically; a single-timestamp graph cannot.

This is why bitemporality is principle #4 and not a nice-to-have.

---

## 7. Pillar data requirements & methodology

Availability tags are *expectations* for the sourcing chat: **[A]** readily available · **[B]** partial / derivable / needs work · **[C]** scarce or must be constructed.

### 7.1 Correlation / comovement engine

| Data | Tag |
|---|---|
| Daily **corporate-action-adjusted total return** series, all BIST securities (incl. delisted) | price [A] / clean total-return [B] |
| Volume, free float, bid-ask / liquidity proxies (for stale-price correction) | [A/B] |
| Factor series: USD/TRY, EUR/TRY, Brent, gas, TRY 2y/10y, Turkey 5y CDS, MSCI EM, gold, VIX, BIST sector indices | [A] |
| Foreign-flow / non-resident ownership series (the BIST comovement driver) | [B] |
| Daily price-limit / limit-lock and halt flags (to identify censored returns) | [B] |
| Intraday / higher-frequency (for lead-lag) | [B/C] |

**Methodology (financial-accuracy critical):**
- Compute returns **USD-primary** (real-TRY cross-check), on **limit-lock-corrected** series so censored daily returns don't enter the estimate (§3).
- Fit a **multi-factor model** per `universe_class`, rolling and regime-tagged; take **residual returns**. Strip factors in an **explicit hierarchical order** so the residual claim is falsifiable rather than aspirational: **market → FX (USD/TRY, EUR/TRY) → rates/CDS → energy/commodity → sector → foreign-flow/ownership-tier → holding-group → residual.** A "residual linkage" is only trusted once it is shown *not* to be a disguised bet on USD/TRY, Turkey CDS, oil, or a holding-cluster. The foreign-flow factor in particular must be in the model or flow-driven comovement masquerades as residual linkage.
- Build the network on residuals, not raw returns. With ~500 names and limited clean history you are in the `p ≈ n` regime, so the sample covariance is **ill-conditioned** — apply **shrinkage (Ledoit–Wolf)** or impose factor structure before inverting anything. Preferred estimator: **factor-decompose the covariance, then impose sector restrictions on the residual covariance** before inversion (Alves-style), which conditions the matrix without pretending the off-diagonals are all real.
- **Residual-survival gate (go/no-go on the lead pillar).** Before trusting any `RESIDUAL_CORR` edge, test whether *stable* residual structure even survives the strip above. If, after neutralizing market+FX+foreign-flow, the residual network is unstable across rolling windows, the correlation pillar's "alpha" is largely the flow factor you removed — and the pillar fails honestly here rather than expensively in a backtest.
- Correct **asynchronous trading** for thin names (Dimson / Scholes–Williams betas) or illiquid pairs will show fake low correlation.
- **Filter** the network: graphical lasso (sparse precision matrix) or PMFG/MST, plus a significance threshold with **FDR control** — 500×500 ≈ 125k pairs guarantees false discoveries otherwise.
- Store only surviving edges as `RESIDUAL_CORR` snapshots.

### 7.2 Supply-chain / linkage engine

| Data | Tag |
|---|---|
| Firm-level customer/supplier identities | [C] |
| Major-customer / customer-concentration disclosures (annual reports, KAP) | [B] |
| Revenue segmentation by product & geography | [B] |
| Holding-group structure & intra-group transactions | [B] |
| TÜİK input-output table (sector-level structural fallback) | [A] (sector, not firm) |
| Customs / foreign-trade by HS code & partner country | [B] |
| Commodity input intensity per sector/firm | [B] |
| NLP-extracted relationships from filings/news | [B] (noisy) |

**Methodology:**
- **Tiered edges:** firm-level (high confidence) → holding-group inferred → sector input-output (low confidence, structural). Confidence decays down tiers; the alpha layer weights by it.
- Weight every edge by **materiality** (% revenue / % COGS), not mere existence — a 2%-of-revenue customer is not a transmission channel.
- **Alpha mechanism:** lagged supplier/customer residual returns → predicted own return; long top-quintile predicted, short bottom. Validate as a portfolio sort, net of costs.

### 7.3 Geopolitical event engine

| Data | Tag |
|---|---|
| Structured event feed: type, date(+precision), actors, geography, severity | [B/C] (curate/NLP) |
| Event → channel mapping (which factor/geo/commodity the event shocks) | [B] (modeled) |
| Exposure bridge (reuses `OPERATES_IN`, `CONSUMES`, `EXPOSED_TO`) | derived |
| Historical labeled events for calibration | [B] |

**Event taxonomy (top level):** FX/monetary shock · sanctions/export controls · armed conflict · diplomatic rupture/rapprochement · trade-policy/tariff · energy-supply disruption · CBRT/regulatory action · elections/political transition · terror/security · natural disaster · pandemic.

**Methodology — and a real accuracy upgrade:** classical single-name **event studies** (estimation window `t−250…t−30`, event window, abnormal returns vs a multi-factor model, cumulative CAR) are the textbook approach, **but they break in Turkey** because events cluster — FX, politics, and regional shocks overlap constantly, so you can rarely attribute a return move to one event in a time-series. The defensible design is **cross-sectional differential exposure**: within the same event window, sort firms by their *exposure* to the shocked channel and measure the *spread* between high- and low-exposure portfolios (a difference-in-differences logic). This controls for the market-wide move and isolates the event's channel. The under-reaction drift in that spread is the tradable signal.

*Caveat (Turkey-specific):* when the dominant channel (FX/political) shocks the whole tape at once — as on 19 Mar 2025 — clean low-exposure **control** names barely exist and the spread thins out precisely in the events that matter most. Measure how many usable control names survive a typical event before trusting the design; a thin cross-section is a reason to down-weight, not to fabricate exposure dispersion.

**Second output — channel stress scenarios (resilience, not just alpha).** Every major `Event` also emits a **channel shock vector** (signed shocks to FX/CDS/oil/gas/rates and the affected geographies), independent of any alpha signal. The portfolio is re-priced against that vector via the exposure tensor (§8) to produce a stress P&L and a worst-exposed-names list. This gives the system a risk spine it otherwise lacks, and it reuses the exact same exposure machinery — the event engine serves alpha **and** resilience from one model.

---

## 8. The connective core, concretely

The Exposure Tensor `E[company, channel, t]` is materialized as the union of `OPERATES_IN`, `CONSUMES`, and `EXPOSED_TO` edges (structural where disclosed, regressed where not), reconciled into a single signed exposure per (company, channel, date).

It does three jobs at once:

1. **Explains correlation.** Predicted comovement of two names from shared channels = `Eᵢ · Cov(channels) · Eⱼ`. Subtract it from observed comovement → residual linkage (Section 7.1).
2. **Routes geopolitical shocks.** An `Event` shocks a set of channels (`TARGETS`); the impact on each name = its exposure row dotted with the shock vector, plus second-order propagation along `SUPPLIES_TO` edges.
3. **Generates alpha.** Where *direct linkage* (supply chain) implies comovement that *exposure-explained* correlation does not yet show in prices, there is an under-priced relationship — the lead-lag edge.

This is what makes the system one system rather than three folders.

---

## 9. Signal / derivation layer (L3)

- **Factor engine:** PIT rolling betas with shrinkage; residual returns; regime tags.
- **Correlation engine:** residual covariance → graphical-lasso/PMFG → filtered `RESIDUAL_CORR` edges → stat-arb pair/basket candidates.
- **Propagation engine:** linked-firm lagged-return signals along weighted `SUPPLIES_TO` edges (reuse the blast-radius traversal, repointed).
- **Event engine:** differential-exposure portfolio sorts around `Event` nodes; write `AFFECTED_BY` CARs back to L1.
- **Promotion gate (anti-self-deception, mandatory).** No signal — and emphatically no "advanced" layer (learned graph overlay, NLP-inferred edge, complex event model) — enters the combiner until it **beats a ladder of naive baselines** on the *same* point-in-time splits: (a) a recurrence/persistence baseline, (b) a sector+FX differential-exposure baseline, (c) a sparse own-factor event-study baseline. Complexity must earn its keep against dumb defaults; if it cannot beat persistence, it is out. This single rule is what keeps the platform from becoming a signal-mining engine, and it is why graph-neural-net overlays are deferred (year-2, only if they clear this gate — at ~500 names they likely will not).
- **Signal registry.** Every candidate alpha is logged with: hypothesis, feature family, train/test dates, number of trials, transaction-cost assumption, survivorship handling, purge/embargo params, and a post-selection significance result — **Deflated Sharpe Ratio** and **Probability of Backtest Overfitting (PBO)**. Promotion is gated on `DSR > 0` after trial-count adjustment, not on raw in-sample Sharpe.
- **Combiner + backtester:** strict point-in-time, liquidity-screened (explicit per-name cost + borrow model and a capacity curve — *not* "net of costs" as hand-waving), net-of-cost. Produces **three standing books**: (1) **research** — frictionless long-short, for clean alpha isolation; (2) **venue-feasible** — enforces the per-name/per-date `short_eligible`, borrow, ±band and halt state; (3) **stress** — short-ban + crowding + limit-lock. If an edge survives only in book (1), it is not real for you. This harness is the asset; the edges are raw material.

---

## 10. Honest risks & limitations

1. **The best alpha needs the worst data — and rests on the least-tested premise.** Linked-firm predictability (your most novel edge) depends on firm-level supply-chain identities — the scarcest input [C]. Worse, the Cohen–Frazzini / Menzly–Özbaş effect is *US-documented, not BIST-proven*: in Turkey intra-group trade is related-party and opaque, and the group structure is already *visible*, so the cross-firm lag may already be priced. Treat this pillar as a hypothesis to **falsify**, not a payoff to harvest. Mitigation: lead with the correlation and event pillars (data-light), grow supply-chain coverage incrementally, and never let a tier-3 (sector-IO) edge masquerade as a firm-level one.
2. **Covariance instability (`p ≈ n`).** 500 names, limited clean Turkish history. Without shrinkage/factor structure the residual network is numerical noise. Designed-in (Section 7.1) but easy to get wrong.
3. **Regime non-stationarity.** Turkish betas are unstable across FX crises. Full-sample estimates are actively misleading; rolling + regime-aware is mandatory, and even then signals may not survive regime changes.
4. **Confounding in events.** Overlapping events make single-name attribution unreliable; the cross-sectional design mitigates but does not eliminate it.
5. **Tradability illusion.** Many BIST names are thin. A signal that backtests beautifully on illiquid names is untradeable. Liquidity-screened, cost-net backtesting is the only honest test.
   - **Short-leg fragility (live).** You chose long-short. BIST short-selling is constrained and **periodically banned during stress** — the very regimes where event/stat-arb signals peak, and the regime toggled 6× in 2025 alone (§3). The research book can stay long-short for clean alpha isolation, but the **venue-feasible** and **stress** books (§9) — enforcing the per-name/per-date `short_eligible`/borrow/band state — are the ones that decide whether the edge is real for you. If it survives only in the frictionless research book, it is not.
   - **Capacity / cost reality (own addition, not in the external reviews).** Signals concentrate in thin names, where backtests flatter and live trading kills. "Net of costs" is not a control unless it is an explicit per-name cost + borrow model with a **capacity curve** and a liquidity floor. Put a number on tradable capacity before believing any pillar.
6. **Multiple-testing / overfitting.** Hundreds of thousands of candidate edges and many model knobs guarantee false discoveries without FDR control and strict out-of-sample / walk-forward discipline.
7. **Soft-edge provenance debt.** NLP- and news-derived supply/event edges decay in quality; without enforced confidence and source, the graph degrades silently.

If any of these is unacceptable for your use case, say so — it changes which pillar leads.

---

## 11. Hand-off checklist for the data-sourcing chat

What the next chat needs to find a source for, in rough priority for an alpha system:

1. **Adjusted total-return daily series for all BIST securities, including delisted**, with corporate-action handling and **publication-dated** fundamentals (point-in-time).
2. **Factor series** (FX, Brent/gas, TRY yields, Turkey CDS, MSCI EM, gold, VIX, BIST sector indices) plus a **foreign-flow / non-resident-ownership series** — long history.
3. **Segment disclosures**: revenue by product & geography, customer concentration — from KAP filings / annual reports, with filing dates.
4. **Holding-group structure & intra-group transactions.**
5. **TÜİK input-output table** (structural fallback) and **customs/foreign-trade by HS code & partner**.
6. **A structured geopolitical event feed** (or the raw news + a taxonomy to build one).
7. **Liquidity / free-float** data for tradability screening and stale-price correction.
8. **Daily price-limit / limit-lock flags, trading-halt flags, and corporate-action records** (bedelsiz, rights, splits) — required to build clean, censored-return-aware total-return series.
9. **TMS 29 / IAS 29 restatement markers** on fundamentals (FY2023+) so exposure weights don't straddle the inflation-accounting boundary.
10. **Per-name short-availability / short-ban history** — to compute deployable long-short P&L.

For each: history depth, point-in-time availability (does it preserve *what was known when*?), update frequency, licensing, and identifier mapping back to `ISIN / mkkMemberOid / LEI`.

---

*Decisions still open — but these are **build-sequencing** choices, not design forks, and none of them block the data-sourcing chat: (a) which pillar leads the first implementation (recommended: correlation engine — most data-light); (b) whether the graph store stays Neo4j with a bitemporal convention or moves to a natively-bitemporal engine (XTDB/Datomic); (c) exact liquidity threshold and transaction-cost model for the backtester. The design is otherwise complete and self-contained for handing to the data-source evaluation.*

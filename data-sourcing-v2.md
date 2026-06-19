# Finance KG v2 — Data Sourcing & Feasibility

**Companion to `system-design-v2.md`. Answers §11's hand-off checklist: where each datum comes from, whether we verified it, history depth, point-in-time (PIT) support, licensing, ID mapping, and where the walls are.**

Date: 2026-06-19 · Method: live MCP probing (Matriks) + web verification of external APIs. Verification legend: **✓ verified live** (called the tool, saw the data) · **◐ web-verified** (confirmed via docs/official pages, not yet pulled) · **⚠ needs verification** (claim rests on inference — test before relying).

---

## 0. Bottom line

**You can build v2 without a fundamental wall.** The correlation pillar (your recommended lead) is almost entirely covered by the **already-connected Matriks MCP**, which I verified returns clean daily bars back to **2006**, quarterly fundamentals with **publication dates back to 2009**, both TMS-29-adjusted *and* unadjusted financials, dividend/capital-action records, and a domestic/export revenue split. The event pillar is feasible from free feeds (GDELT + Matriks news). The supply-chain pillar remains the scarce one — exactly as §10 predicted — and no source fixes that for you.

But "no fundamental wall" is not "no walls." Eight specific ones, ranked by how early you must decide:

| # | Wall | Status (updated 2026-06-19) |
|---|---|---|
| **W1** | Foreign-flow daily/investor-level | **✅ RESOLVED** — MKK license bought & **verified live**: broker-level daily flows now return, incl. foreign houses (Bank of America net −6.4M sh, HSBC, etc.). Residual: per-name `investor/historic` %-series throws `400` (param, not auth) — tune at build; EVDS weekly aggregate as cross-check. |
| **W2** | Delisted/survivorship history | **◐ DEFERRED (your call)** — metadata retains deleted symbols (`deleted:true` returns records ✓); full delisted-**equity OHLCV** test to run at ingestion. BIST DataStore is the backstop. |
| **W3** | CDS / MSCI EM / VIX | **✅ PLAN SET** — VIX free (FRED/Yahoo); MSCI EM via EEM/TUR ETF proxy (free) or paid MSCI; Turkey CDS via free scrape (MacroVar/WorldGovBonds) **+ eurobond-spread proxy from your own debt layer**. See §7. |
| **W4** | Input-output table stale | **✅ UPGRADED** — use **OECD ICIO 2025 ed.** (Turkey, 1995–**2022**, free) instead of TÜİK 2012. |
| **W5** | Price-limit / halt flags | **◐ DESIGN-IN** — derive limit-lock from ±band vs prior close; halts via Matriks `suspended` dataset + KAP notices. Algorithm in §7. |
| **W6** | Firm-level supply-chain identities | **◐ SOFTENED** — KAP "new-business" disclosures (Matriks `includeNewBusiness`) return **structured, dated, counterparty-named contracts with built-in materiality ratios** — a real high-confidence tier; OECD-ICIO sector fallback below it. |
| **W7** | Corporate-action adjustment | **✅ RESOLVED (empirical)** — bars are back-adjusted for splits/bonus/rights (continuous across EREGL 2024-11-27 & ASELS 2023-08-25 ex-dates; 2006 prices scaled down). `adjusted:false` ≠ raw. Add cash dividends for TOTAL return. |
| **W8** | Turkish FinBERT | **✅ DROPPED** — LLM extraction adopted (your decision). |

Net: W1/W7 resolved, W3/W4/W6/W8 have a set plan, W2/W5 are deferred-but-understood. Detail and the sequenced build plan are in §7.

---

## 1. Recommended source stack

Map sources to the three layers, and to what's already connected vs. what to add.

**L2 quant store (prices, factors, flows) — spine = Matriks MCP (connected).**
- Matriks covers daily OHLCV+volume, BIST sector indices, FX, gold, Brent, central-bank rates, dividends/capital actions, fundamentals, monthly foreign flow, VIOP derivatives, and a Turkish news feed. This is the workhorse and it's already in your hands.
- **EVDS (TCMB)** — free keyed REST API — fills the macro factors Matriks is thin on: TRY benchmark yields (2y/10y), policy rate, CPI (for real-TRY cross-check), and the **weekly non-resident securities-holding series** (your foreign-flow fallback).
- **Scrape/paid** for the three Matriks/EVDS gaps: Turkey 5y CDS, MSCI EM, VIX.

**L1 structural graph (entities, linkages, exposures, events) — spine = your existing KAP/GLEIF graph.**
- **KAP** (free, unauthenticated JSON API) — financial statements with filing dates, segment/related-party/major-customer footnotes, material events, board/ownership. You already have adapters.
- **GLEIF L2 + KAP subsidiary edges** — already built (control/ownership structure).
- **GDELT** (free; API + public BigQuery dataset) — raw geopolitical event stream with actors/geography/tone; Turkey well covered.
- **Turkish legislation MCP** (connected) — CBRT/SPK regulatory-action event nodes + regulation text.

**L3 derivation — your code (the blast-radius traversal repointed), unchanged by sourcing.**

**Identifier bridge (critical, easy to under-design):** Matriks keys on **ticker** (`GARAN`), KAP on **`mkkMemberOid`**, your graph on **ISIN/LEI**. You already hold a `ticker→ISIN` map (`bist_isin.json`, 1,014 rows) and KAP `mkkMemberOid`s — so Matriks joins to the graph through that existing bridge. Make the bridge a first-class, tested table; every L2↔L1 join depends on it.

---

## 2. Per-requirement feasibility (§11 checklist)

### 1 — Adjusted total-return daily series (incl. delisted) + PIT fundamentals
- **Prices:** Matriks `historicalData` — **✓ verified** daily OHLCV+volume for GARAN back to **2006-01-02**; ~500+ names enumerable. Built-in benchmark block already returns USD/TRY, BIST100, gram-gold return/β/correlation per query.
- **PIT fundamentals:** Matriks `declarationDates` — **✓ verified** quarterly balance-sheet *announcement dates back to 2009Q1*, and it even shows a re-declared period (2016Q2 twice) — your bitemporal `knowledge_date` hook, delivered. KAP filings independently carry filing timestamps.
- **Total return:** must be *constructed* — see item 8 (dividend/action records exist).
- **Delisted/survivorship: ⚠ the open risk.** Not confirmed Matriks retains dead tickers' histories. BIST **DataStore** sells historical/reference files (incl. delisted) as a one-off purchase fallback.
- Tag: price **[A] ✓** · PIT-fundamentals **[A] ✓** · clean total-return **[B]** · delisted **[B/C] ⚠**

### 2 — Factor series + foreign-flow
- **In Matriks (✓/◐):** USD/TRY, EUR/TRY (forex) ✓; gold (XAU/GLDGR) ✓; Brent (commodity/warrant) ◐; **242 BIST indices incl. sector indices** ✓; central-bank rates, LIBOR ✓.
- **EVDS (◐):** TRY 2y/10y benchmark yields, policy rate, CPI, FX — free API.
- **Gaps (◐):** **MSCI EM** (Matriks returned 0 for "MSCI" ✓-absent → proxy with EEM/TUR ETF prices, or paid MSCI), **VIX** (free: Cboe/FRED), **Turkey 5y CDS** (not in EVDS → Investing.com/WorldGovernmentBonds/cbonds — scrape w/ ToS risk, or paid LSEG/cbonds).
- **Foreign-flow:** Matriks *monthly* per-name net foreign buy/sell — **✓ verified** (GARAN May-2026 net −1.30M TL). Daily/investor-level **✗ 401 license-gated** (W1). EVDS weekly aggregate non-resident holdings = fallback ◐.
- Tag: FX/indices/gold/Brent/yields **[A]** · CDS/MSCI/VIX **[B] external** · foreign-flow monthly **[B] ✓**, daily **[C] license**

### 3 — Segment disclosures: revenue by product & geography, customer concentration
- Matriks dashboard — **✓ verified** quarterly **domestic vs export** sales split (TUPRS). That's a real geography axis, but only domestic/export — not country-level, not product-level.
- Full product/geography segments + **major-customer concentration** live in KAP annual-report footnotes — extractable via KAP API but **unstructured** (PDF/footnote → LLM parsing, see §5).
- Tag: domestic/export **[B] ✓** · product/geo/customer-concentration **[B/C]** (KAP + NLP)

### 4 — Holding-group structure & intra-group transactions
- Structure: **already built** (GLEIF L2 + KAP subsidiary edges). [A]
- Intra-group *transactions*: KAP related-party footnotes (Şirket Genel Bilgi Formu + financial-statement notes) → **[B/C]** via NLP/LLM.

### 5 — TÜİK input-output + customs/foreign-trade by HS & partner
- **IO table: ◐ latest symmetric table is 2012** (W4) — portal download, weak API → scrape. Down-weight as a structural, dated fallback.
- Trade by HS+partner: TÜİK data portal (~292 trade datasets) — scrape; or commercial REST (e.g. turkeytradedata.com) — paid, clean.
- Tag: IO **[A]-but-stale** · trade-by-HS **[B]**

### 6 — Structured geopolitical event feed
- **GDELT ◐** — free, Turkey-covered, API + BigQuery; events back to 1979 (v1) / Feb-2015 with GKG+translation (v2). Raw actor/geo/tone stream.
- **Matriks `newsAndEvents` ✓** — KAP + Matriks + **Reuters + AA**, categories incl. **SIYASET (politics)** and **DUNYA (world)**, headline search + date filter. Turkish-language event raw material.
- ACLED ◐ (political violence/protest, human-coded, limited-license) for high-precision conflict events.
- Structured event→channel mapping must be **built** (taxonomy + LLM extraction).
- Tag: raw feed **[A] ✓** · structured events **[B/C] build**

### 7 — Liquidity / free-float
- Matriks: volume/quantity **✓ verified**; free-float via `includeCirculation` (fiili dolaşım) ◐; bid/ask via `includeBidAsk` but **≤15-day window** ⚠ (so no long bid-ask-spread history — use volume/turnover liquidity proxies for backtests).
- Tag: volume **[A] ✓** · free-float **[A/B]** · bid-ask history **[B]** (window-limited)

### 8 — Price-limit/limit-lock flags, halt flags, corporate-action records
- **Corporate actions ✓ verified:** Matriks `dividendsCapital` returns dividends back to 2006 with ex-dates + capital increases (bedelli/rights). Bonus (bedelsiz)/splits sit in capital-action records → total-return adjustment constructible. **(But confirm price-feed adjustment — W7.)**
- **Limit-lock flags: [B] derive** — not delivered; infer from close hitting ±10% band vs prior close (watch market-maker-widened bands). Implement §3's cumulative-return-across-lock-window logic.
- **Halt flags: [B]** — Matriks `historicalData` exposes a `suspended` daily dataset ◐; BIST/KAP publish halt notices.
- Tag: corporate actions **[A] ✓** · limit-lock **[B] derive** · halts **[B]**

### 9 — TMS 29 / IAS 29 restatement markers
- **✓ verified — best-case outcome.** Matriks returns **both** as-reported and inflation-adjusted figures per quarter (`qReturns` 143.7bn vs `qReturnsAdjusted` 226.1bn for TUPRS 2025Q2), plus an `unadjusted` toggle and `isUnadjustedSymbol` flag. Your restatement-boundary immunity is delivered, not just flaggable.
- Tag: **[A] ✓**

### 10 — Per-name short-availability / short-ban history
- BIST publishes the **daily short-eligible list** (*açığa satışa konu olabilecek paylar*) ◐ and SPK/BIST ban announcements: 2020 COVID (BIST30→50), 2022 Russia-Ukraine, **2023 Feb earthquake → ran into 2024**, partial lift Jan-2025 (BIST-50). ◐
- No clean API → **scrape current lists + curate the announcement timeline; archive going forward** (history isn't served retroactively).
- Tag: **[B/C] scrape + curate**

---

## 3. Source × requirement matrix

Primary source per requirement, with verification + a key caveat. (Full grid also in `data-sourcing-matrix-v2.xlsx`.)

| # | Requirement | Primary source | Backup | Verify | Tag | Key caveat |
|---|---|---|---|---|---|---|
| 1 | Daily OHLCV + PIT fundamentals | **Matriks** | BIST DataStore | ✓ | A | Delisted retention ⚠ (W2) |
| 2a | FX / gold / Brent / BIST sector idx | **Matriks** | EVDS | ✓/◐ | A | — |
| 2b | TRY 2y/10y yields, CPI, policy rate | **EVDS** | Matriks | ◐ | A | Free, keyed |
| 2c | CDS / MSCI EM / VIX | Scrape / paid | ETF proxy | ◐ | B | Not in Matriks/EVDS (W3) |
| 2d | Foreign-flow | Matriks (monthly) | EVDS weekly | ✓ | B/C | Daily = license-gated (W1) |
| 3 | Segments / customer concentration | **KAP** (+NLP) | Matriks dom/exp | ✓/◐ | B/C | Footnotes unstructured |
| 4 | Holding-group + intra-group txns | **Graph** / KAP | — | ✓ | A / B-C | Txns need NLP |
| 5 | Input-output / trade-by-HS | **TÜİK** | Commercial API | ◐ | A-stale / B | IO=2012 (W4) |
| 6 | Geopolitical events | **GDELT** + Matriks news | ACLED | ✓/◐ | A / B-C | Structuring = build |
| 7 | Liquidity / free-float | **Matriks** | KAP/BIST | ✓ | A/B | Bid-ask ≤15d |
| 8 | Corp actions / limit / halt | **Matriks** (actions) | BIST/KAP | ✓ | A / B-derive | Limit-lock = derived (W5) |
| 9 | TMS-29 restatement markers | **Matriks** | KAP | ✓ | A | Both bases delivered |
| 10 | Short-availability / ban history | BIST lists + SPK | — | ◐ | B/C | No API; curate (W10) |

---

## 4. Connector & MCP evaluation

**Keep / core:**
- **Matriks** (`3181c780…`) — primary L2 + much of L1. Verified deep. The single most valuable connector you have.
- **Turkish legislation** (`8953a9d8…`, mevzuat + bedesten) — relevant: CBRT/SPK regulatory-event nodes and regulation text. Keep.
- **BigQuery** (finance plugin) — dual use: host the L2 quant store *and* query the **GDELT public dataset** in place. Worth wiring.
- **n8n** (`8b0cca99…`) — not a source; the orchestration layer to schedule EVDS/KAP/GDELT pulls. Useful infra.

**Dismiss for sourcing (right tool, wrong job):** Gmail, Box/Drive (storage/comms); Gamma, Figma (presentation/design); Daloopa (US/SEC fundamentals — no BIST); Amplitude/Hex/Definite (product analytics/BI); `4d9c8763…` (accounts/CRM-style, not market data).

**Worth adding (not connected):**
- **`borsa-mcp`** (community, GitHub `saidsurucu/borsa-mcp`) — Turkish + US exchange & **TEFAS fund** data via KAP/public sources. Useful overlap/redundancy, **but unvetted third-party — review the code before trusting it in a pipeline.**
- **Nimble** (registry) — crawler/extractor MCP; a managed alternative to Firecrawl for the scrape targets (CDS, BIST short-lists, TÜİK, VAP).
- **LSEG/Refinitiv** (registry) — *if budget exists*: CDS, yield curves, FX in one vetted feed — closes W3 cleanly. Enterprise-priced.
- **S&P Global Kensho kFinance** (registry) — has business-relationship/supply-chain data, but global-large-cap focused and BIST-thin; unlikely to crack W6.

---

## 5. Better ways to extract (your explicit question)

**1. Stop using web search as an extraction tool — use the APIs directly.** Web search is for *discovery and licensing terms*, which is what I used it for here. The actual numeric data should come from: Matriks MCP (connected), **EVDS keyed REST** (free), **KAP JSON API** (free, unauthenticated), **GDELT** (free + BigQuery). You already have KAP/GLEIF adapters — extend that pattern; don't scrape what an API serves.

**2. Reserve scraping (Firecrawl / Nimble / Claude-in-Chrome) for the genuinely unstructured tail only:** Turkey 5y CDS, the BIST daily short-eligible PDF, the TÜİK portal, VAP investor demographics. Note a compliance point: scraping Investing.com violates its ToS — prefer WorldGovernmentBonds/official PDFs or a paid CDS feed. Firecrawl is best driven from *your* environment (its API key, or via n8n); I can use Claude-in-Chrome for low-volume verification, not bulk pulls.

**3. Drop FinBERT — DECIDED (you confirmed: removed).** There is **no off-the-shelf Turkish FinBERT.** What exists is English FinBERT (ProsusAI) and general Turkish BERTurk (dbmdz) / a general Turkish sentiment model — none finance-tuned for Turkish. Your options are (a) fine-tune BERTurk on a labelled Turkish financial corpus (a real mini-project, and you'd have to build the labels), or (b) **use an LLM with structured-output prompts for event/relationship/sentiment extraction.** In 2026, (b) is the better call: FinBERT gives you a 3-class sentiment score; the alpha thesis (§7.2–7.3) needs *typed events, actors, channels, and signed exposures* — extraction tasks an LLM does far better than a sentiment classifier. Use FinBERT only if you later need to score millions of headlines cheaply at scale, as a distilled second pass. Don't lead with it.

**4. One-off purchases beat fragile pipelines for static history.** Delisted price history (W2) and the 2012 IO table aren't streams — buy them once from BIST DataStore / pull from TÜİK once, version them in `data/reference/`, and stop re-fetching.

---

## 6. Decisions to make now (so you don't hit walls later)

1. **Foreign-flow granularity (W1).** Is the foreign-flow factor daily or monthly? If daily and core, price the MKK/AKDE license now; otherwise commit to EVDS weekly aggregate + Matriks monthly and accept the resolution.
2. **Delisted history (W2).** Before any backtest, test whether Matriks serves a known delisted ticker's full history. If not, budget a BIST DataStore purchase — survivorship bias is non-negotiable for honest alpha.
3. **Corporate-action adjustment (W7).** Confirm with Matriks exactly what `adjusted:false` means for bonus/splits vs dividends. Get one worked example (a name with a known bedelsiz) right end-to-end before trusting any return series.
4. **Factor gaps (W3).** Pick the CDS/MSCI/VIX path: ETF proxies (free, lead here) now, paid feed later if proxies prove too lossy.
5. **NLP approach (W8).** Adopt LLM extraction over FinBERT; design the event/relationship schema first.

Everything else degrades gracefully and matches the design's own risk register (§10). The data exists. The discipline — PIT correctness, censored returns, survivorship, provenance — is still where this lives or dies.

---

## 7. Resolutions & build plan (decisions applied 2026-06-19)

### 7.1 Wall resolutions

- **W1 foreign-flow — resolved.** The MKK license is live: the broker-level flow (`institutionalFlow`, previously `401`) now returns full daily buyer/seller-by-broker data, and foreign custodians (Bank of America, HSBC, …) are visible and classifiable. Build the foreign-flow factor by tagging brokers foreign vs domestic and netting; cross-check the market-wide level against EVDS weekly non-resident holdings. One residual: the per-name `v1/investor/historic` "% non-resident" series returns `400` (a parameter issue now, not auth) — resolve at build via `investorDates`/period params or fall back to settlement (takas) custody data, which the same license unlocks.
- **W3 CDS/MSCI EM/VIX — plan set.** Per factor: **VIX** → FRED `VIXCLS` or Yahoo `^VIX` (free, long history). **MSCI EM** → the index is licensed, so use the **iShares EEM** (or MSCI Turkey `TUR`) ETF total return as a free proxy; buy MSCI direct only if a proxy proves too lossy. **Turkey 5y CDS** → two complementary routes: (a) a free scrape of MacroVar / WorldGovernmentBonds (daily, basis points) via Firecrawl; (b) an **eurobond-spread proxy** built from your *own* debt layer — Turkey USD sovereign eurobond YTM minus matched-maturity US Treasury (FRED) = sovereign credit spread, which tracks CDS closely, carries no ToS risk, and reuses the 836 XS eurobonds you already hold. Lead with the proxy where eurobond pricing is available; use the scrape as the headline CDS cross-check. Avoid Investing.com (ToS).
- **W4 input-output — upgraded.** Replace the stale TÜİK-2012 table with the **OECD ICIO 2025 edition** (Turkey included, annual **1995–2022**, free CSV, scriptable via `pymrio`/R `iotr`). Being inter-country, it also gives sector-level **import dependency** — directly useful for the energy-import channel (§3). Keep TÜİK only for finer domestic HS/partner trade detail.
- **W5 limit-lock & halts — design-in.** No flag is served, so derive: mark a bar **limit-locked** when |close/prior_close − 1| sits at the session band (≈ ±10%, widened for market-maker names and certain regimes — make the band a per-name/per-date parameter, not a constant) *and* the intraday range is pinned at the limit. Apply §3's cumulative-return-across-the-lock-window treatment and carry a `limit_locked` flag on every such observation. Source **halts** from Matriks `historicalData` `dailyReportDataset:"suspended"` plus KAP halt notices.
- **W6 supply-chain — softened, tiered.** A real firm-level source exists: KAP **"Yeni İş İlişkisi" (new business relationship)** disclosures, delivered structured by Matriks `includeNewBusiness` — each row carries counterparty + scope (`desc`), `amount`, `cur`, disclosure `date`, KAP `url`, and **materiality ratios** (`rt` = contract/▸, `marValRt` = contract/market-value). Example pulled live: ASELSAN 780M EUR SSB contract (deliveries 2028–32), 845M USD, 271M USD, 114.7M USD export deals. Build the supply-chain layer in tiers: **tier-1** firm-level from KAP new-business + customer-concentration footnotes (LLM-extracted, materiality-weighted) → **tier-2** intra-group (your existing graph) → **tier-3** OECD-ICIO sector fallback. Confidence decays down the tiers; never let tier-3 masquerade as tier-1. Caveat: tier-1 is *sparse* (only material, disclosed deals) and counterparties are often government/foreign (non-listed) — it seeds the high-confidence edges, it does not map the whole chain.
- **W7 corporate-action adjustment — resolved empirically.** Pulled daily bars across two capital-action ex-dates: **EREGL 2024-11-27** (24.48 → 24.14 → 24.65, market-like) and **ASELS 2023-08-25** (36.82 → 37.60 → 39.17, rising) — both perfectly continuous, no mechanical gap, despite the response flagging `adjusted:false`. Combined with 2006 prices sitting far below nominal, the conclusion is firm: **Matriks daily bars are back-adjusted for splits/bonus/rights and are safe for price-return computation.** They are *price-return*, not total-return — so reconstruct **total return** by adding cash dividends (ex-date + amount from `dividendsCapital`, both verified available). One belt-and-braces step: validate once on a known 100% bedelsiz at build time, and confirm with Matriks exactly what `adjusted:false` refers to (almost certainly "dividends not reinvested").

### 7.2 Connector decisions

- **borsa-mcp — INCLUDE (it adds, with a caveat).** Beyond what Matriks already gives, it adds three useful things: (1) **EVDS as a ready MCP tool** (`get_evds_data`: 145 categories, catalog search, `yoy_pct` formulas) — saves writing an EVDS adapter; (2) **TEFAS fund data** (`get_fund_data`: 836+ funds, portfolio *allocations* and flows) — a domestic-institutional ownership/flow tier that complements the foreign-flow factor; (3) convenient **TR 2Y/5Y/10Y bond yields** (`get_bond_yields`) and Mynet ownership/subsidiary cross-checks. **Caveat:** it's a community MCP whose remote endpoint routes through a third-party host — for a research substrate, **self-host it** (it's MIT/open-source, `uvx --from git+…`) and use it as a convenience/cross-check layer, while going direct-to-source (EVDS API, TEFAS API) for production pulls.
- **Nimble — SKIP.** It's a managed crawler/extractor MCP that overlaps almost entirely with the **Firecrawl** you already have. It adds nothing over Firecrawl for the scrape tail (CDS, BIST short-list, TÜİK, VAP); only revisit if you specifically want an in-agent MCP scraper instead of Firecrawl.
- **FinBERT — REMOVED** from the stack (LLM extraction replaces it everywhere: events, relations, segment/customer parsing).

### 7.3 Build plan (correlation → event → supply-chain)

1. **Phase 0 — Identifier bridge & universe.** Lock the `ticker ↔ ISIN ↔ mkkMemberOid ↔ LEI` table as a tested, first-class object (extend `bist_isin.json`). Enumerate the universe including `deleted:true` symbols; **run the W2 delisted-equity OHLCV test here** and decide BIST DataStore if gaps appear.
2. **Phase 1 — L2 spine + correlation pillar (data-light, lead here).** Ingest Matriks adjusted daily bars + dividends → build **total-return** series (W7 recipe); derive `limit_locked`/halt flags (W5). Load factors: Matriks FX/gold/Brent/242 indices + EVDS yields/CPI/policy-rate + W3 set (FRED VIX, EEM proxy, CDS proxy/scrape). Build the **foreign-flow factor** from MKK broker-level flows (W1) + EVDS weekly. → run the residual-correlation / graphical-lasso engine.
3. **Phase 2 — Event pillar.** GDELT (via BigQuery) + Matriks news (politics/world categories) → **LLM extraction** → typed `Event` nodes + `TARGETS` channel mapping; regulatory/CBRT/SPK events via the legislation MCP; curate the short-ban timeline + per-name short-eligibility (W10/short-availability).
4. **Phase 3 — Supply-chain pillar (tiered).** Tier-1: KAP new-business (Matriks `includeNewBusiness`) + customer-concentration footnotes, LLM-extracted and materiality-weighted (W6). Tier-2: intra-group edges (existing graph). Tier-3: OECD-ICIO 2022 sector fallback (W4).
5. **Phase 4 — Integrity & backtester.** Wire PIT (`knowledge_date` from declaration dates), TMS-29 dual-basis handling (already delivered), liquidity/free-float screens, short-availability state, censored-return logic; then the walk-forward, cost-net backtester.
6. **Cross-cutting infra.** n8n schedules the EVDS/KAP/GDELT pulls; BigQuery hosts the L2 store and queries GDELT in place; self-hosted borsa-mcp as the EVDS/TEFAS/yields convenience + cross-check layer; Firecrawl for the scrape tail (CDS, short-lists, TÜİK, VAP).

---

## 8. Sources

- Matriks MCP — live tool probes (`historicalData`, `fundamentalAnalysis`, `institutionalFlow`, `symbolSearch`, `newsAndEvents`), 2026-06-19.
- [EVDS / TCMB API guide](https://www.scribd.com/document/800522379/EVDS-Web-Service-Usage-Guide) · [EVDS3 portal](https://evds3.tcmb.gov.tr/) · [TCMB Securities Statistics](https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/Main+Menu/Statistics/Monetary+and+Financial+Statistics/Securities+Statistics)
- [Borsa İstanbul — Historical Data Sales](https://www.borsaistanbul.com/en/data/historical-data-sales) · [Data Dissemination](https://www.borsaistanbul.com/en/data/data-dissemination) · [Market Functioning (short-selling)](https://www.borsaistanbul.com/en/markets/equity-market/market-functioning)
- [KAP](https://kap.org.tr/en) · [pykap wrapper](https://github.com/cemsinano/pykap) · [kap_sdk](https://pypi.org/project/kap_sdk/)
- [TÜİK Veri Portalı — foreign trade](https://data.tuik.gov.tr/Kategori/GetKategori?p=dis-ticaret-104&dil=2) · [TÜİK Supply-Use/IO tables (overview)](https://www.iioa.org/conferences/16th/files/Papers/Kula%20SU%20and%20IOT%20for%20Turkey.doc)
- [GDELT Project — data](https://www.gdeltproject.org/data.html) · [GDELT Cloud](https://gdeltcloud.com/) · [ACLED](https://acleddata.com/)
- [MKK — VAP (Veri Analiz Platformu)](https://www.vap.org.tr/) · [VAP indices news](https://www.mkk.com.tr/tr-tr/haberler/Sayfalar/Merkezi-Kayit-Kurulusu-Veri-Analiz-Platformunda-Iki-Yeni-Endeks-Yayinlanmaya-Basladi.aspx)
- [Turkey 5Y CDS — WorldGovernmentBonds](https://www.worldgovernmentbonds.com/cds-historical-data/turkey/5-years/) · [SPK partial lift of short-selling ban (2024)](https://www.esin.av.tr/2024/12/09/cmb-partially-lifts-short-selling-ban/)
- NLP: [ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert) · [BERTurk (dbmdz)](https://huggingface.co/dbmdz/bert-base-turkish-cased) · [savasy Turkish sentiment](https://huggingface.co/savasy/bert-base-turkish-sentiment-cased)
- Connectors: [borsa-mcp](https://github.com/saidsurucu/borsa-mcp) (28 tools — KAP, Yahoo, TEFAS, EVDS, borsapy/İş Yatırım, Mynet; MIT, self-hostable) · registry (Nimble, LSEG, S&P Global Kensho, FMP).
- W4 upgrade: [OECD ICIO 2025 edition (Turkey 1995–2022)](https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html).
- W3: [Turkey 5Y CDS — MacroVar (free)](https://macrovar.com/turkey/turkey-credit-default-swaps/) · [WorldGovernmentBonds](https://www.worldgovernmentbonds.com/cds-historical-data/turkey/5-year/); VIX → FRED `VIXCLS`; MSCI EM → iShares EEM proxy.
- Live verification 2026-06-19: MKK broker-flow unlock (W1), corporate-action continuity test on EREGL/ASELS (W7), KAP new-business structured contracts (W6) — all via Matriks MCP probes.

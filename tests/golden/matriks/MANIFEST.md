# Golden samples — Matriks MCP

Real data snapshots captured from the **Matriks MCP** (the verified data spine, server `3181c780-…`) in a Cowork session on **2026-06-19**, before M0. They give Claude Code's M0 smoke test and M1 reconciliation **known answers** to check live fetches against.

These are **real captured data, not fixtures.** They live under `tests/golden/` (not `fixtures/`) and they may be read by tests, but per the data contract (`CLAUDE.md` §4) they must **never** be loaded into L2 as if freshly sourced — they are reconciliation targets only. Each file carries a `_provenance` block (exact tool + params + capture date) and a `_golden_master` block (which invariant/gate it backs and the expected behavior).

## What M0 must do with these

1. **Re-fetch each via the same tool+params** and confirm the live response matches (a drift guard, like the v1 `smoke_check()`). A mismatch = upstream drift → stop and log.
2. Wire them as assertions in `tests/golden/` (the suite in `VERIFICATION.md`).
3. If any connector is unreachable from Claude Code, that is the M0 **[STOP]** — do not fabricate; report.

## File → what it backs

| File | Backs (invariant / milestone) | Known answer it pins |
|---|---|---|
| `ohlcv_EREGL_2024-11.json` | corp-action back-adjustment · M1 | rights ex-date 2024-11-27 shows **no price gap** → series back-adjusted; `adjusted:false` ≠ raw |
| `ohlcv_ASELS_2023-08.json` | corp-action back-adjustment · M1 | same conclusion at ASELS rights ex-date 2023-08-25 (2nd example) |
| `corpactions_EREGL_ASELS.json` | ground truth for the two masters · M1 | the exact ex-dates + that blank ex-date strings exist (handle, don't guess) |
| `factors_USDTRY_XU100_2024-11.json` | core factor series · M2 | USD/TRY (volume=0 is fine for an FX rate) + XU100 aligned to the equity calendar |
| `declaration_dates_KCHOL.json` | **PIT-leak detector** · M0 / bitemporal | `knowledge_date = declarationDate`; e.g. 202503 known only from 2025-04-30; coverage back to FY2008 |
| `accounting_regime_KCHOL_202412.json` | **accounting_regime** invariant · M1 | FY2024 IAS-29-adjusted revenue 2,109bn vs unadjusted 1,611bn (~+31%); both bases served |
| `foreign_flow_GARAN_monthly.json` | foreign-flow factor · M2 | MKK/AKDE license is **live** (BofA/HSBC visible); monthly net-flow shape |
| `foreign_flow_GARAN_historic_2025Q1.json` | foreign-flow factor · M2 | **quirk RESOLVED** — `mode='historic'`+dates returns broker-level history; foreign houses classifiable; `v1/investor/historic` still 400 |
| `takas_GARAN_2025_shock.json` | foreign-flow (takas) + M2 regime break | MKK takas custody **live**, dated daily series; 19-Mar-2025 shock visible (custody value −23% over 3 sessions) |
| `news_sample_2026-06-19.json` | event/news feed · M6 | feed schema; KAP filings + circuit-breaker + geopolitical items share one feed |
| `universe_bist30.json` | universe + id-bridge + universe_class · M0 | 30-name liquid cross-section incl. EKGYO (reit), KCHOL/SAHOL (holding), bank templates |

## Data-access findings for M0 (verified live in this session)

- **Reachable now:** `historicalData` (OHLCV + USDTRY/XU100/gold benchmark in one call), `fundamentalAnalysis` (financials, **declaration dates**, **adjusted+unadjusted**, dividends/capital actions), `symbolSearch`, `institutionalFlow` (**AKDE/MKK license active** — broker-level returns), `newsAndEvents`. OHLCV back to 2006; declaration dates back to 2008/09.
- **Quirk — RESOLVED 2026-06-21 (M2).** `institutionalFlow` ignored `foreignPeriods=['202411']` because `mode='monthly'` is a CURRENT-SNAPSHOT service (returns the latest month, no historical backfill). The fix is the RANGE mode **`foreignInvestorMode='historic'` + `startDate`/`endDate`**, verified live now that the MKK license is bought: broker-level foreign history returns, foreign houses classifiable. `newsAndEvents` still needs client-side `category` filtering. See `foreign_flow_GARAN_historic_2025Q1.json`.
- **Foreign-flow construction (decided 2026-06-21):** broker-netting (curated foreign-custodian map, net the foreign houses) + **EVDS weekly non-resident holdings** as the market-wide cross-check, **daily** granularity (loop `historic` mode per day). The **takas custody** path (`historicalData includeHistoricalTakasIndicator`) is also live and dated — a candidate for the per-name foreign-holdings series once the foreign-custodian agent filter is confirmed.
- **STILL 400 (escalate to Matriks/MKK):** the per-investor **%-non-resident demographic series** (`v1/investor/historic`) returns 400 even with MKK, via both tool paths. Is it a higher VAP tier, a different param shape (discrete `investorDates`, not a range), or unexposed? The chosen broker-netting path does **not** depend on it.
- **Classification caveat:** foreign vs domestic = non-resident **custody**, not parent nationality (GARANTI BBVA is domestic despite the BBVA parent). The foreign-broker map must be a **curated, verified** reference; unknown brokers are surfaced/refused, never bucketed as domestic.
- **Not in Matriks:** **VIX** confirmed absent (`symbolSearch('VIX')` → 0 results) → **FRED `VIXCLS`** (free API key) needed. Turkey CDS → scrape/paid feed (W3). **MSCI-EM/EEM** is a US ETF — may be reachable via Matriks `foreignMarkets` (US/NBBO); verify before adding an external source.

## Re-capture

Every `_provenance.params` is the exact call. Re-run the same call from Claude Code (with the Matriks MCP in `.mcp.json`) to refresh or extend. Keep windows tiny — these are reconciliation anchors, not the ingestion dataset (that is M1's job through the adapter → Parquet).

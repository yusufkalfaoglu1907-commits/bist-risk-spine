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
| `news_sample_2026-06-19.json` | event/news feed · M6 | feed schema; KAP filings + circuit-breaker + geopolitical items share one feed |
| `universe_bist30.json` | universe + id-bridge + universe_class · M0 | 30-name liquid cross-section incl. EKGYO (reit), KCHOL/SAHOL (holding), bank templates |

## Data-access findings for M0 (verified live in this session)

- **Reachable now:** `historicalData` (OHLCV + USDTRY/XU100/gold benchmark in one call), `fundamentalAnalysis` (financials, **declaration dates**, **adjusted+unadjusted**, dividends/capital actions), `symbolSearch`, `institutionalFlow` (**AKDE/MKK license active** — broker-level returns), `newsAndEvents`. OHLCV back to 2006; declaration dates back to 2008/09.
- **Quirk — period filters not always honored.** `institutionalFlow` ignored `foreignPeriods=['202411']` and returned the latest month (202605); `newsAndEvents` did not strictly honor `category`. The build agent must verify how to pull a *specific historical* foreign-flow month (likely `foreignInvestorMode='historic'` + dates) and filter news categories client-side. **Do not assume these params filter.**
- **Re-test in M0, don't assume:** per-name daily *investor-level* foreign data (`foreignInvestorMode='investor'`) was previously a 400; limit-lock/halt flags are not delivered (derive ±10% band — design §3 / data-sourcing W5); EVDS, KAP-direct, and GDELT are separate connectors not snapshotted here (add their own golden samples when wired).
- **Not in Matriks (per data-sourcing-v2):** Turkey CDS, VIX, MSCI-EM → proxies (FRED VIX, EEM, scraped CDS). No golden sample yet.

## Re-capture

Every `_provenance.params` is the exact call. Re-run the same call from Claude Code (with the Matriks MCP in `.mcp.json`) to refresh or extend. Keep windows tiny — these are reconciliation anchors, not the ingestion dataset (that is M1's job through the adapter → Parquet).

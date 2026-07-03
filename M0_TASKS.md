# M0 — Foundations & data-access proof (task list for Claude Code)

The exit gate for M0 (from `BUILD_PLAN.md`): **every connector reachable and golden-sampled · PIT-leak detector passes · id-bridge test green · `make verify` runs end-to-end.** Nothing in M1+ starts until this is GREEN.

Scaffolding is already in place (stubs raise `NotImplementedError` with a `TODO(M0)`). Your job is to fill the stubs and turn the skipped invariant tests green. Work top-to-bottom; **T1 is a hard [STOP] gate** — if the connector is unreachable, stop and tell the user, do not fabricate.

First, session start (CLAUDE.md §7): read the latest `BUILD_LOG.md` entry, run `make verify` to see the current baseline (v1 suite + the new golden/invariant tests should already be green), then start T1.

---

## T1 — Wire and PROVE the Matriks data path  ⟶ [STOP] gate

Auth is fully documented (`Matriks_MCP_Dokumani.pdf`). Three identities live in `.env` (gitignored): `MATRIKS_USERNAME` (5-digit, `00000`), `MATRIKS_CLIENT_ID` (OAuth Client ID), `MATRIKS_API_KEY` (`sk_live_…`). Two **header-auth** transports — *not* the `/claude` endpoint (that is Claude-Desktop OAuth only):

- **REST API** = what the ingestion adapter uses (plain httpx, reproducible; MCP tools can't be called from Python). `POST {MATRIKS_REST_URL}/tools/{tool}/execute`, headers `X-API-Key: <username>:<key>` + `X-Client-ID: <username|client_id>`, JSON body = params. Schema: `https://mcp.matriks.ai/openapi.json`. Verified example: `POST .../tools/market_price/execute` body `{"symbol":"THYAO"}`.
- **Header-auth MCP** (optional, interactive only) = `https://mcp.matriks.ai/mcp`, `type: http`, headers `X-Client-ID` + `X-API-Key`. `.mcp.json` is preconfigured but Claude Code expands `${VARS}` from the **shell env, not .env**.

Steps, in order:
1. `set -a && source .env && set +a` (so the REST adapter and `.mcp.json` both see the creds; `config.py` also auto-loads `.env` for Python via python-dotenv).
2. Fetch `https://mcp.matriks.ai/openapi.json`; confirm exact tool slugs + params for: `market_price`, `historical_data`, `fundamental_analysis`, `institutional_flow`, `news_and_events`, `symbol_search`. Fix `MatriksAdapter.TOOL_PATHS` if any slug differs.
3. Implement `MatriksAdapter.fetch()` (REST `httpx.post(self._rest_endpoint(tool), headers=self._rest_headers(), json=params)`; raise `SourceUnreachable` on failure — never fabricate) and `smoke_check()` (re-fetch each golden sample's `_provenance.params` in `tests/golden/matriks/`, assert the live response matches; raise `ContractDrift` on mismatch). The header/URL helpers are already implemented in the stub.
4. Sanity-check the known quirks (`tests/golden/matriks/MANIFEST.md`): pull a **specific historical** foreign-flow month (try `historic` mode + dates), and filter `news` categories client-side.
5. *(Optional)* interactive MCP check — either rely on the preconfigured `.mcp.json` (after step 1's `source .env`) or run:
   `claude mcp add --transport http matriks-finance https://mcp.matriks.ai/mcp --header "X-Client-ID: 00000" --header "X-API-Key: <sk_live_…>"` — then `claude mcp list` shows it green.

**Exit:** `make smoke` → PASS (REST reachable + golden samples match). Record in `BUILD_LOG.md` which transport/headers worked (→ ADR-0002). If unreachable after checking creds + openapi.json → **STOP, log, ask the user**.

## T2 — Stand up L2 (DuckDB + Parquet)

1. Implement `L2Store.connect()` and `bootstrap_schema()` in `src/tmkg/l2/store.py` (execute `src/tmkg/l2/schema.sql`).
2. Land the EREGL/ASELS golden bars into `prices` via a `MatriksAdapter` → `L2Store.write_parquet` round-trip; confirm read-back equals the golden bars.

**Exit:** schema bootstraps; all tables present; golden bars round-trip equal.

## T3 — PIT / bitemporal access wrapper (the keystone)

1. Implement `PITAccess.series()`, `.graph()`, `.universe()` in `src/tmkg/pit/access.py` so every read filters `knowledge_date <= as_of`.
2. Unskip `tests/invariants/test_pit_leak.py::test_no_read_returns_knowledge_date_after_as_of` and implement it against `declaration_dates_KCHOL.json`: with `as_of=2025-04-15`, the latest KCHOL fundamental visible must be period **202412** (declared 2025-02-18), **not 202503** (declared 2025-04-30).

**Exit:** PIT-leak detector GREEN. No raw `conn.execute`/`SELECT` anywhere outside `tmkg.pit` / `tmkg.ingest`.

## T4 — id-bridge resolver + test

1. Implement ticker ↔ ISIN ↔ `mkkMemberOid` ↔ LEI resolution over the v1 Kuzu graph; ambiguous cases **refuse and log**, never guess (v1 confidence-tiered pattern).
2. Test round-trips on the golden universe anchors (EREGL, ASELS, KCHOL, GARAN) from `universe_bist30.json`.

**Exit:** id-bridge round-trip test GREEN.

## T5 — Wire verify + confirm the known-good baseline

1. Confirm the v1 Kuzu graph still loads and its ~70 tests pass. (Native macOS Claude Code should avoid the Cowork-sandbox lock; if you hit "Operation not permitted" on `.shadow`/`.wal`, build on a local path and `cp` the `.kuzu` back — CLAUDE.md §6.)
2. Add a **survivorship** check: ingest one delisted name end-to-end and assert it appears in `PITAccess.universe()` for a past as-of date (test the W2 wall early — see `data-sourcing-v2.md`).
3. `make verify` runs all of it.

**Exit (M0 done):** `make verify` GREEN end-to-end; `BUILD_LOG.md` updated with the connector decision (which auth worked), the survivorship/W2 finding, and the single next action (begin M1 — clean return series). Commit: `M0: data-access proof + L2 + PIT wrapper + id-bridge`.

---

### Out of scope for M0 (resist the temptation)
Returns construction (M1), factor model (M2), any signal (M3+). M0 is *only* the proof and the spine. Adding pillar logic before the PIT-leak detector is green is how lookahead bugs get baked in.

### Connector golden samples still missing (capture when each is wired)
EVDS, KAP-direct, GDELT, and the W3 proxies (FRED VIX, iShares EEM, scraped Turkey CDS) have **no** golden sample yet — add one (same `_provenance` + `_golden_master` shape as the Matriks files) when you wire each, so its smoke_check has an anchor.

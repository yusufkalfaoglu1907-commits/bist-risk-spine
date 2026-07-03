# ADR-0002 — Matriks ingestion transport = REST API, single `X-API-Key` header

- **Status:** Accepted (2026-06-19)
- **Context:** M0 T1 (the data-access [STOP] gate). The ingestion adapter must reach
  the verified Matriks data spine from headless Python (not an interactive MCP
  session) and reproduce the committed golden samples (`tests/golden/matriks/`).
  Two transports were documented in `Matriks_MCP_Dokumani.pdf`: a header-auth MCP
  endpoint (`/mcp`) and a REST API (`/mcp-api/v1`). The exact auth-identity
  combination was unproven from Claude Code's environment.

## Decision

**The ingestion pipeline uses the REST API**, proven live on 2026-06-19:

```
POST  https://mcp.matriks.ai/mcp-api/v1/tools/{tool}/execute
headers:
    X-API-Key:    "<MATRIKS_USERNAME>:<MATRIKS_API_KEY>"   e.g. "00000:sk_live_…"
    Content-Type: application/json
body: {<params>}            # e.g. {"action":"price","symbol":"THYAO"}
```

- **Tool slugs are camelCase** (`historicalData`, `fundamentalAnalysis`,
  `institutionalFlow`, `newsAndEvents`, `symbolSearch`, `marketPrice`), confirmed
  against `https://mcp.matriks.ai/openapi.json` (22 tools). `MatriksAdapter.TOOL_PATHS`
  maps logical snake_case names to these slugs; camelCase passes through unchanged.
- **Response is an MCP-style envelope:** `{"content":[{"type":"text","text":"<json-string>"}],
  "isError":bool,"_meta":{…}}`. The real payload is `json.loads(content[0]["text"])`.

## The auth finding (the non-obvious part)

Authentication succeeds on the **single `X-API-Key` header alone**, in
`<username>:<key>` form (the 5-digit `MATRIKS_USERNAME`, not the long hex OAuth
`MATRIKS_CLIENT_ID`). **Sending an additional `X-Client-ID` header — as the stub
originally did — makes the gateway return HTTP 500 `{"error":"INTERNAL_ERROR",
"message":"Authentication failed"}`.** Verified by elimination:

| `X-API-Key` | `X-Client-ID` | result |
|---|---|---|
| `username:key` | `username` | 500 auth failed |
| `client_id:key` | `client_id` | 500 auth failed |
| `client_id:key` | `username` | 500 auth failed |
| **`username:key`** | **(absent)** | **200 OK** |

`MatriksAdapter._rest_headers()` therefore sends only `X-API-Key`. A unit test
(`tests/test_matriks_live.py::test_auth_header_is_username_key_no_client_id`)
locks this so the header is never silently re-added.

## Smoke gate = honest, two-mode drift guard

`make smoke` re-fetches each golden sample via its `_provenance` tool+params:

- **VALUE anchors** — the two raw, immutable OHLCV captures (`ohlcv_EREGL_2024-11`,
  `ohlcv_ASELS_2023-08`) — must reproduce field-for-field (containment match,
  ignoring live-only additive keys like `timestamp`). Mismatch ⇒ `ContractDrift`.
- **REACHABILITY anchors** — the other single-call goldens, which were curated/
  reshaped at capture (`symbolSearch` golden = live's inner `index` object;
  `declaration_dates` is a derived extract) **or are inherently volatile**
  (`newsAndEvents` is a live feed; `institutionalFlow` returns the latest month
  regardless of the period param) — are proven reachable + non-error only. We do
  **not** fabricate a value-match where the data legitimately moves.
- **COMPOSITE anchors** — `accounting_regime`, `corpactions`, `factors` (their
  `_provenance.params` is a *list* of calls) — are M1/M2 reconciliation anchors,
  skipped by the smoke gate.

Every run writes `data/cache/matriks_smoke_report.json` (§4 audit rule).

## Why not MCP for ingestion

MCP tools can only be invoked by an interactive agent, not imported into a module;
a backtest that depends on a live MCP session is not reproducible (§4). The
header-auth MCP (`.mcp.json` → `/mcp`) stays available for *interactive* agent
queries only — never the pipeline.

## Cost / risk accepted

- The reachability-anchor tools have no live **value** drift guard yet. **Mitigation /
  follow-up (M1):** re-capture raw single-call golden samples for declaration dates,
  foreign flow, and the BIST-30 universe (store the raw unwrapped payload, not a
  reshaped extract) so they graduate into VALUE anchors.
- Auth is environment-coupled to the 5-digit username. If Matriks rotates the
  identity model, the unit + live tests fail loudly and point here.

## Revisit if

- Matriks changes the envelope shape, tool slugs, or auth identity (the live drift
  guard will fail first), **or**
- a future need requires the MCP transport for ingestion (it should not — keep the
  network boundary in REST).

Superseding requires a new ADR, not an edit to this one.

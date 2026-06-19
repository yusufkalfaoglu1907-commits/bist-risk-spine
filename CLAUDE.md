# CLAUDE.md — Finance KG (tmkg) operating constitution

This file is auto-loaded every session. Keep it lean. It tells you **what this project is, what you may never break, and how to start/work/end a session.** Detail lives in the docs this file points to — read them when the work needs them, not before.

---

## 1. What this is

A Turkish-equities (BİST, ~500 names) research substrate for **alpha/signal generation** across three pillars: asset correlations, geopolitical-event impact, supply-chain dependencies. Personal, non-commercial, solo.

- **Authoritative design:** `system-design-v2.md` (v2.1). The hardening rationale is in `v2.1-hardening-memo.md`. **Do not re-derive the design — implement it.** If you think the design is wrong, log it in `BUILD_LOG.md` and raise it; don't silently deviate.
- **Data sourcing (already verified live):** `data-sourcing-v2.md` + `data-sourcing-matrix-v2.xlsx`. The verified spine is the **Matriks MCP**; supporting sources are EVDS, KAP, GDELT, borsa-mcp.
- **The one idea (don't lose it):** everything is one object — the **Exposure Tensor** `Company × Channel × Time`. Correlation, events, and supply chain are three views of how a shock propagates to a price. **Never store raw pairwise correlations as edges** — only *residual* linkage after common channels are stripped.

## 2. Current state

The **v1 equities identity/ownership core is built and trustworthy** (see `README.md`): KuzuDB graph at `data/tmkg.kuzu`, 802 `Company` nodes, ISIN/sector 100% on equity-traded names, LEI 92%, 212 verified `CONTROLS` edges (a DAG). The off-mission debt subsystem was retired (archived in `archive/`). ~70 passing tests.

**v2 is the build ahead of you.** The current roadmap and gates are in `BUILD_PLAN.md`. Always check it for the current milestone before starting.

## 3. Tech stack & locked decisions

- **L1 structural graph:** **KuzuDB** (embedded, local-first, no server). Reuses every v1 adapter. Decision + reasoning: `decisions/ADR-0001-graph-store.md`. Bitemporality is **enforced by a data-access wrapper**, not left to discipline (§5).
- **L2 quant store:** **DuckDB + Parquet.** 500 names is small; this is the right scale. Prices/returns/factors/betas/residuals/CARs live here, **never** as graph properties.
- **L3 signal layer:** **Python** (polars/pandas, statsmodels, scikit-learn, networkx/scikit-network) behind a strict point-in-time data-access wrapper.
- **Return base:** **USD-primary**, CPI-real-TRY as cross-check. Nominal TRY is reference only.
- Other consequential choices get a numbered ADR in `decisions/`. ADRs are append-only; supersede, don't edit.

## 4. The data-access contract (read this before writing any ingestion or signal code)

```
  Matriks / EVDS / KAP / GDELT  (MCP or keyed REST)
                │   ← ONLY the ingestion layer may touch the network
                ▼
  ingestion adapter  →  local Parquet / Kuzu cache  (L2 / L1)
                │   ← everything downstream reads the local cache
                ▼
  factor · correlation · event · backtest engines (L3)   ← never hit the network
```

Three hard rules, no exceptions:

1. **Signal/backtest code never makes a network call.** It reads L2/L1 only. A backtest that depends on a live connection is not reproducible.
2. **Never fabricate, synthesize, mock, or "fill in" market data.** If a source is unreachable, the adapter **fails loudly and stops** — it does not return placeholder, interpolated, or invented numbers. Fabricated quant data that looks real is the most dangerous bug in this project. (Illustrative *fixtures* for unit tests are fine **only** under `fixtures/` and clearly labelled — they may never reach L2.)
3. **Every ingestion run writes a JSON audit report** to `data/cache/` (matched/skipped/refused counts, source, as-of date) — extend the v1 pattern. Invariant tests read these reports.

**Data access (Matriks):** the **ingestion adapter uses the Matriks REST API** (`mcp-api/v1/tools/{tool}/execute`, header auth `X-API-Key: <username>:<key>` + `X-Client-ID`) — plain Python, reproducible; MCP tools cannot be called from a module. The header-auth MCP (`.mcp.json`, `https://mcp.matriks.ai/mcp`) is for *interactive* agent queries only, never the ingestion pipeline. **Milestone 0, task 1 is a data-access smoke test** (`make smoke`) that proves Matriks is reachable and matches the golden samples in `tests/golden/matriks/`. Until it passes, write no other v2 code.

## 5. Non-negotiable invariants (the immunity spec)

These are the things that quietly corrupt every signal. They are engineered in from commit 1, never "flagged and ignored." Full spec in `system-design-v2.md` §3/§6; condensed:

- **Bitemporal / point-in-time.** Every edge and datum carries `valid_from`, `valid_to`, `knowledge_date`. **All** graph and L2 reads go through the PIT wrapper, which requires an `as_of` date and refuses to return anything with `knowledge_date > as_of`. No raw `conn.execute` / `SELECT` in signal code. This is the make-or-break for an honest backtest and **cannot be retrofitted** — get it right first.
- **USD-primary returns**, on **corporate-action-adjusted total-return** series (bedelsiz/bonus, rights, splits, dividends). A signal that holds only in TRY is fragile.
- **Limit-lock censoring.** BIST ±10% bands censor returns; flag limit-lock days and use cumulative returns across the lock window. Raw daily returns on locked days are not returns.
- **`accounting_regime` state**, not a one-time boundary: `{nominal_pre2023, ias29_2023_2024, suspended_2025_2027}`. IAS-29 is suspended FY2025–2027 by law. Never compute a figure straddling a switch without converting to a common basis (Matriks serves both bases — select, don't restate).
- **`short_eligible` per name per date.** The 2025 short-ban toggled 6×. The **venue-feasible book** is a first-class output, not a footnote.
- **Foreign-flow factor in the core factor set.** If it's not stripped, flow-driven comovement masquerades as residual linkage and fabricates supply-chain signals.
- **Survivorship.** Delisted/merged/renamed names stay in the graph with dead histories. `MEMBER_OF` is time-varying.
- **Provenance on every soft edge:** `source`, `confidence`, `evidence_tier ∈ {verified, inferred}`, `uncertainty`. An **inferred edge is never silently promoted into a verified traversal path.** Tier-3 (sector-IO) never masquerades as tier-1 (firm-level).
- **The id-bridge** (ticker ↔ ISIN ↔ mkkMemberOid ↔ LEI) is a single point of failure. It has its own resolution test (see `VERIFICATION.md`).

## 6. Coding standards (extend v1, don't reinvent)

- Package `tmkg` under `src/`. Run with `PYTHONPATH=src`. Tests: `PYTHONPATH=src python -m pytest tests/ -q`.
- **One external source = one adapter with a `smoke_check()` drift guard** (the v1 KAP/GLEIF/sector pattern). The smoke check fails loudly when the upstream contract drifts.
- **Confidence-tiered writes:** only high-confidence results are written; ambiguous cases are logged to a `data/cache/*_report.json`, never guessed.
- **Every behavior change ships with a test in the same change.** New invariant → new invariant test (see `VERIFICATION.md`).
- Small vertical slices over big-bang branches. Keep the repo green between sessions.
- Don't open the Kuzu DB on a path you're also syncing if you hit lock errors; build on a local path and copy the main `.kuzu` file. (Native Claude Code on macOS usually avoids the Cowork-sandbox lock issue — but if you see "Operation not permitted" on `.shadow`/`.wal`, that's the cause.)

## 7. Session protocol (this is the handoff mechanism)

**START — bootstrap context and verify a known-good baseline. Never trust that the last session left it green.**
1. Read the latest entry in `BUILD_LOG.md` and the current milestone in `BUILD_PLAN.md`.
2. Run the verification suite (`VERIFICATION.md` → "Run it"). If it's RED, your first job is to get it green or understand why before adding anything.
3. State the one concrete next action you're taking.

**WORK — small slices, verify continuously.**
- Stay inside the current milestone's scope and honor its exit gate.
- Obey §4 (data contract) and §5 (invariants) without exception.
- Add/extend tests with each change. Run the relevant invariant checks as you go.
- If a source is unreachable or a result is ambiguous: **stop, log, ask** — do not fabricate or guess.

**END — leave a clean handoff.**
1. Run the full verification suite. Leave the repo green, or log RED explicitly with the reason.
2. Append a `BUILD_LOG.md` entry: what you attempted, what passed/failed, decisions made, open threads, and the **single next action**.
3. Update this file or `BUILD_PLAN.md` **only** for durable invariants/decisions (not transient state). New consequential decision → new ADR.
4. Commit with a structured message referencing the milestone (e.g. `M1: limit-lock censoring + reconciliation test`).

## 8. Hard stops — halt and ask the user

- A data source is unreachable or its contract has drifted (smoke check fails).
- A verification gate fails and the fix would require weakening an invariant in §5.
- A **project-level go/no-go gate** is reached (e.g. the residual-survival gate in `BUILD_PLAN.md` M3) — surface the result and decision, don't auto-proceed.
- You're about to deviate from `system-design-v2.md`, delete data, or weaken a test to make it pass.

## 9. Doc map

| File | Purpose | Update cadence |
|---|---|---|
| `CLAUDE.md` (this) | Constitution: invariants, contract, session protocol | Rare — durable rules only |
| `system-design-v2.md` | Authoritative design (v2.1) | Don't edit during build; raise issues in BUILD_LOG |
| `BUILD_PLAN.md` | Phased milestones + go/no-go gates | When a milestone completes or re-plans |
| `VERIFICATION.md` | Standing QA protocol, invariant suite, definition-of-done | When a new invariant/gate is added |
| `BUILD_LOG.md` | Append-only session journal (the handoff) | **Every session** |
| `decisions/ADR-*.md` | One consequential decision each, append-only | When a fork is decided |
| `data-sourcing-v2.md` + `.xlsx` | Verified sources, walls, connectors | Reference |

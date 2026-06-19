# ADR-0001 — L1 structural graph store = KuzuDB

- **Status:** Accepted (2026-06-19)
- **Context:** v2 build kickoff. `system-design-v2.md` §5 pencilled in Neo4j/Memgraph for L1 but explicitly hedged ("recommendation, not requirement — revisit"). v1 is already built on KuzuDB. Decision was delegated ("you decide whichever is best").

## Decision

**Keep KuzuDB for L1.** Enforce bitemporality with a thin **PIT data-access wrapper** (`tmkg/pit/`) that every read goes through — rather than by relying on a natively-bitemporal engine or on developer discipline.

## Why

1. **The hard problem isn't in L1.** L1 is ~800 nodes and a few thousand edges — trivial at this scale. The genuinely difficult, failure-prone work (`p ≈ n` covariance, point-in-time backtesting, residual estimation) lives in **L2/L3**. Spending a migration on L1 optimizes the easy layer.
2. **Local-first fits a solo non-commercial substrate.** Kuzu is embedded — no server to run, runs in CI and offline. Neo4j/Memgraph adds operational weight for marginal benefit at this size. (The design itself rejected NIST-style governance overhead for the same reason.)
3. **Maximum reuse.** Every v1 adapter, loader, the schema in `ddl.py`, and the confidence-tiered write + JSON-audit-report patterns carry over unchanged. A Neo4j move would re-litigate all of it.
4. **Bitemporality was never going to come "for free" anyway.** Even on a bitemporal engine, the project needs a disciplined as-of access path. Putting that in an explicit wrapper makes the guarantee **testable** (the PIT-leak detector in `VERIFICATION.md`) instead of trusting an engine's semantics we'd still have to verify.

## Cost / risk accepted

- Bitemporality is by-convention, not engine-enforced → **mitigated** by routing *all* L1/L2 reads through the PIT wrapper and gating on the PIT-leak detector. No raw `conn.execute` in signal code.
- Kuzu's Cypher dialect and tooling are less mature than Neo4j's (GraphRAG, visualization). Acceptable for now; revisit only if a concrete need bites.

## Revisit if

- Complex temporal/recursive Cypher becomes a recurring bottleneck, **or**
- M8 GraphRAG/visualization needs Neo4j-specific tooling, **or**
- the node/edge count grows by orders of magnitude (not expected at ~500 names).

Superseding requires a new ADR, not an edit to this one.

# VERIFICATION.md — the anti-self-deception spine

In an alpha system, **"the tests pass" does not mean "it's correct."** A backtest can be green, fast, and profitable *and* completely wrong — because of a lookahead leak, a dead name dropped from the universe, a flow factor leaking into "residual," or fabricated data that looks plausible. Ordinary unit tests don't catch those. This file defines the checks that do.

Three layers, run in this order of trust:

1. **Invariant suite** — machine-checkable guards on the §5 invariants. Run every session, gate every milestone.
2. **Golden masters / reconciliation** — known answers verified by hand once, then asserted forever.
3. **Adversarial review** — a fresh agent tries to break the result at each `[STOP]` gate.

---

## 1. Standing invariant suite (`tests/invariants/`)

Each invariant test maps to a `CLAUDE.md` §5 rule. A new invariant in §5 → a new test here, same change.

| Invariant | Test asserts | Maps to |
|---|---|---|
| **PIT-leak detector** | for sampled dates `D`, every read via the PIT wrapper returns **zero** rows with `knowledge_date > D`; restated values resolve to the version known at `D` | bitemporal/PIT |
| **No-network-in-signal-layer** | importing/running L3 with the network stubbed to raise still completes (signal code touches only L2/L1); only `tmkg/adapters/`+`tmkg/ingest/` may import the network client | data contract rule 1 |
| **No-fabrication** | nothing under `fixtures/` is ever loaded into L2; an unreachable source raises, never returns placeholder/interpolated values | data contract rule 2 |
| **Survivorship / as-of universe** | a known delisted name **is** in the universe for an as-of date inside its listed life and **carries its dead price history** | survivorship |
| **id-bridge round-trip** | ticker → ISIN → kap_oid → LEI → back resolves consistently on the golden universe anchors; ambiguous cases are *refused* and logged, not guessed (`kap_oid` is the graph's org-id leg; MKK's `mkkMemberOid` is reconciled when Matriks ingestion lands, M1) | id-bridge |
| **Provenance completeness** | no soft edge exists without `source`, `confidence`, `evidence_tier`, `uncertainty`; no `inferred` edge appears in a `verified`-only traversal | provenance |
| **accounting_regime no-straddle** | any growth/intensity/materiality computation refuses inputs spanning the FY2023 *or* FY2025 switch without a common-basis conversion | accounting_regime |
| **Limit-lock censoring** | a known limit-locked sequence is flagged and its daily returns are replaced by the cumulative cross-window return | limit-lock |
| **Neutralization orthogonality** | residual returns from M2 are statistically orthogonal to each stripped factor, in the specified order | factor strip |
| **Short-eligibility state** | the venue-feasible book refuses a short on a name/date flagged `short_eligible = false` (e.g. inside the Mar 23–Aug 29 2025 ban) | short_eligible |

## 2. Golden masters / reconciliation (`tests/golden/`)

Known answers, verified by hand **once**, then frozen as assertions. These catch the errors invariants can't express as a rule.

- **Total-return reconciliation:** total return of one name across one hand-verified corporate action (a specific bedelsiz/bonus issue) matches the constructed series to tolerance. This single test catches most corporate-action bugs.
- **Group-exposure reconciliation:** aggregated Koç-group portfolio weight matches the hand count (the v1 exit query: 70.0%). Confirms graph traversal didn't silently change.
- **Factor sanity:** a known FX-exporter has the expected `EXPOSED_TO USD/TRY` sign; a known FX-debtor the opposite.
- **Golden data sample:** the M0 connector snapshots — re-fetching must match (drift guard on the upstream feeds, like the v1 `smoke_check()` pattern).

When a golden master legitimately changes (e.g. a vendor corrects history), update it **deliberately** in its own commit with the reason in `BUILD_LOG.md` — never silently to make a run pass.

## 3. Promotion gate & signal registry (the judge — built in M4, used forever after)

No signal — and emphatically no "advanced" layer — is trusted until the harness certifies it. Mechanics:

- **Baseline ladder:** candidate must beat (a) persistence, (b) sector+FX differential exposure, (c) sparse own-factor event study, on the **same PIT splits**.
- **Statistics:** **Deflated Sharpe Ratio > 0** after trial-count adjustment (not raw Sharpe) + **PBO** below threshold. FDR control on multi-pair tests.
- **Three books:** survives in **venue-feasible** (enforcing `short_eligible`/borrow/band/halt), not just **research** (frictionless). If it lives only in the research book, it is not real.
- **Capacity:** clears an explicit per-name cost + borrow model and a stated capacity floor.
- **Registry entry:** hypothesis, feature family, train/test dates, trial count, cost assumption, survivorship handling, purge/embargo params, DSR, PBO, book results.

**Harness self-test (M4 exit):** a **shuffled-label (known-null)** signal must **fail** the gate, and a **known-good toy** signal must pass. A harness that can't reject noise can't certify signal.

## 4. Adversarial review at each `[STOP]` gate

This project has already paid off from adversarial auditing once (the 2026-06-11 audit caught the blast-radius being ≈1–2% of reality). Repeat it at project-level gates (M3 residual-survival, M4 harness self-test, M5 first real signal):

- Spawn a **fresh agent** (no build context) and task it to *falsify* the result — find the lookahead, the dropped names, the flow leak, the overfit knob. Verify-first beats trust.
- Record findings and dispositions in `BUILD_LOG.md`. A gate is not GO until the adversarial pass is clean or its findings are explicitly dispositioned.

## 5. Definition of Done (per task — paste into the BUILD_LOG entry)

- [ ] Behavior change ships with a test in the **same** change.
- [ ] New invariant (if any) added to the suite in §1.
- [ ] Touches data? An ingestion adapter with a `smoke_check()`; a `data/cache/*_report.json` written; nothing fabricated.
- [ ] Reads L1/L2 in signal code **only** through the PIT wrapper.
- [ ] Full invariant suite green (or RED explicitly logged with reason).
- [ ] `BUILD_LOG.md` updated; consequential decision → new ADR.
- [ ] Milestone exit gate re-checked if the task completes one.

## Run it

```bash
make verify                                            # full session start/end gate (scripts/verify.sh)
make smoke                                              # M0 data-access [STOP] gate: live Matriks REST matches golden samples

# layered:
PYTHONPATH=src python -m pytest tests/invariants -q   # the §5 guards
PYTHONPATH=src python -m pytest tests/golden -q        # known-answer reconciliation
PYTHONPATH=src python -m pytest -m "live" -q           # connector drift guards (skip offline)
```

`make smoke` needs the Matriks creds in the shell env: `set -a && source .env && set +a`.
The smoke gate value-matches the two immutable OHLCV golden anchors and proves the
other connector tools reachable (ADR-0002); it writes `data/cache/matriks_smoke_report.json`.

Live connector tests auto-skip when offline (the v1 pattern), so the suite stays green in CI without credentials — but the **drift guard fails loudly** when run with access and the upstream contract has changed.

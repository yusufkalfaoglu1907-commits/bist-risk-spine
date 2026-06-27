# Turkish Equities Knowledge Graph (tmkg)

Property-graph + point-in-time quant research substrate over BİST entities (~500 names) —
ownership, control, governance, events, regulation, and macro sensitivity. Authoritative
design: `system-design-v2.md`; phased build + go/no-go gates: `BUILD_PLAN.md`; session journal:
`BUILD_LOG.md`; consequential decisions: `decisions/ADR-*.md`.

## Project status (2026-06-27)

The **v1 equities identity/ownership core is built and trustworthy**, and on top of it a full
**v2 quant substrate + risk spine** has been built. The original goal — a tradeable
**cross-sectional alpha** signal across three pillars (asset correlation, geopolitical-event
impact, supply-chain linkage) — was pursued to a rigorous conclusion: **all three pillars
returned NO-GO** through an honest venue-feasible / Deflated-Sharpe / PBO promotion gate
(ADR-0004 correlation, ADR-0005 event, ADR-0006 supply-chain). Tradeable cross-sectional
firm-linkage alpha on BIST residuals appears **exhausted at this scale (n≈500)**.

So the deliverable was **repositioned (ADR-0006): an honest research substrate + risk spine,
not a live alpha book.** What it is now:

- **A point-in-time, bitemporal research engine** — USD-primary corporate-action-adjusted
  returns, a factor/residual machine (foreign-flow stripped), and limit-lock / accounting-regime /
  short-eligible state machines — that **cheaply and credibly rejects** plausible-but-unreal
  signals. The M4 promotion judge (Deflated Sharpe + PBO/CSCV, a purged-walk-forward backtester
  with cost+borrow across three books) earned its keep by rejecting two live candidates.
- **A risk spine (M8):** *scenario re-pricing* (a macro channel shock re-priced through the
  exposure tensor → worst-exposed names + stress P&L) and *linkage propagation* (an idiosyncratic
  shock cascaded through the ownership/control graph), plus three standing health monitors
  (id-bridge, data-drift, registry hygiene).

A rigorously-established three-pillar NO-GO **is** the result — worth more than an overfit book
that would lose money live. The substrate and risk spine are the durable assets.

**Live graph: 802 `Company` nodes (730 ticker-bearing), ISIN/sector 100% on equity-traded names,
LEI 92%, `CONTROLS` 212-edge verified DAG.** Identity is confidence-tiered — ambiguous cases are
logged, never guessed.

### v2 code map — where it lives

- `src/tmkg/pit/` — the point-in-time / bitemporal access wrapper (the honest-backtest keystone) + the id-bridge resolver.
- `src/tmkg/l2/` + `src/tmkg/ingest/` — the DuckDB+Parquet quant store and the network ingestion adapters (Matriks / EVDS / FRED / WorldGovBonds / GDELT); every run writes a `data/cache/*_report.json` audit.
- `src/tmkg/returns/`, `src/tmkg/factors/` — USD total-return series + the factor / neutralization / residual machine (M1/M2).
- `src/tmkg/signals/` — the M4 promotion judge (Deflated Sharpe, PBO/CSCV, PIT backtester, baseline ladder, L2 `signal_registry`) + the M3/M5 residual correlation stat-arb.
- `src/tmkg/events/` — GDELT event ingestion + the §240 channel-stress engine.
- `src/tmkg/risk/` — the risk spine: scenario re-pricing (`scenarios`/`repricing`/`run_scenarios`) + linkage-graph propagation (`linkage_propagation`/`run_linkage`).
- `src/tmkg/monitor/` — the standing health monitors (id-bridge health, smoke-drift, registry hygiene).

Run the risk tools: `PYTHONPATH=src python scripts/run_scenarios.py 2026-06-15 2025-03-18 2025-03-25` ·
`scripts/run_linkage_shock.py ARCLK:-0.20` · the monitors `scripts/monitor_{idbridge,smoke_drift,registry}.py`.

---

The sections below document the **v1 identity/ownership core** (still accurate and foundational).

## What's here

```
src/tmkg/
  config.py                 paths + .env (optional in Phase 1)
  graph/connection.py       KuzuDB connection (embedded, local-first)
  schema/ddl.py             all node/rel tables; Phase-1 subset is populated
  adapters/kap_adapter.py   LIVE KAP adapter: own member-list fetch + kap-client
                            for disclosures; smoke_check() guards API drift
  adapters/gleif_adapter.py LIVE GLEIF Level-1 adapter: diacritic-aware name
                            matching, coverage scoring, cache; smoke_check() drift guard
  adapters/sector_adapter.py KAP sector taxonomy from a committed reference file:
                            tree + ticker→leaf lookups + roll-up; smoke_check() drift guard
  loaders/gleif_backfill.py back-fill lei/legal_form/jurisdiction onto Company
                            nodes for confident matches; writes an audit report
  loaders/sector_backfill.py Sector nodes + SUBSECTOR_OF hierarchy + IN_SECTOR
                            (company→leaf) onto the live graph; writes an audit report
  loaders/identity.py       Company / Person / Security / Sector / Portfolio (fixtures)
  loaders/ownership.py      HOLDS_STAKE / CONTROLS / SUBSIDIARY_OF / BOARD_MEMBER_OF / IN_SECTOR
  loaders/kap_ingest.py     LIVE: seed companies/securities + ingest disclosures
  analytics/exposure.py     Phase-1 exit query: aggregated group exposure
fixtures/                   ILLUSTRATIVE Koç-group sample data (offline)
data/reference/sectors.json committed KAP sector taxonomy (see data/reference/README.md)
scripts/build_phase1.py     create schema, load fixtures, run exit query (offline)
scripts/ingest_kap.py       LIVE: seed from KAP + pull disclosures
scripts/backfill_gleif.py   LIVE: match seeded companies to GLEIF, write LEIs
scripts/import_sectors.py   parse a KAP Sektörler .xlsx export → data/reference/sectors.json
scripts/backfill_sectors.py apply the sector taxonomy to the live graph
tests/test_phase1.py        offline smoke test (5 tests)
tests/test_sectors.py       offline sector adapter/loader tests + live-reference check (7)
tests/test_kap_live.py      live KAP drift guard (3 tests; auto-skip if offline)
tests/test_gleif.py         offline matcher unit tests + live GLEIF drift guard
                            (11 tests; live ones auto-skip if offline)
```

## Sector classification (KAP Sektörler taxonomy)

KAP seeds Company identity but exposes no sector field, so the live graph starts
with zero `Sector` nodes. The authoritative classification is KAP's two-level
"Sektörler" listing — committed as a dated reference file and applied to the graph:

```bash
# (re)generate the reference file from a KAP Sektörler .xlsx export
PYTHONPATH=src python scripts/import_sectors.py "Sektörler.xlsx" \
    --source "KAP Sektörler listing (kap.org.tr) export"

# apply: Sector nodes + SUBSECTOR_OF hierarchy + IN_SECTOR (company→leaf)
PYTHONPATH=src python scripts/backfill_sectors.py --db ./data/tmkg.kuzu
# preview coverage without writing:
PYTHONPATH=src python scripts/backfill_sectors.py --db ./data/tmkg.kuzu --dry-run
```

Each company links to its **leaf** sub-sector; the main sector is one
`SUBSECTOR_OF` hop up, so a sector roll-up is a single traversal:

```cypher
MATCH (c:Company)-[:IN_SECTOR]->(:Sector)-[:SUBSECTOR_OF]->(main:Sector)
RETURN main.name, count(c) ORDER BY count(c) DESC
```

Unmatched companies (debt-only issuers, funds, names absent from the equities
taxonomy) are left unlinked and listed in `data/cache/sector_backfill_report.json`
— never guessed. See `data/reference/README.md` for the file format and refresh path.

## GLEIF back-fill (the identity-spine join keys: LEI + ISIN)

```bash
# LEI + ISIN back-fill for the first 25 listed companies (quick proof)
PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --limit 25

# full LEI + ISIN back-fill for every listed company still missing them
PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu

# just one stage (LEIs must exist before the ISIN stage runs)
PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --stage isin
```

GLEIF supplies the canonical external join keys (architecture §3): the LEI is
what Phase-4's OpenSanctions matching keys on, and the equity ISIN is what
Phase-2's time-series price join keys on. The API is public, CC0, no key
required (api.gleif.org).

### Matching is fuzzy — so it's scored and audited (verified 2026-06-06)

KAP gives company names with Turkish diacritics ("...SANAYİ VE TİCARET A.Ş.");
GLEIF stores them inconsistently — sometimes fully diacritic ("KOÇ HOLDİNG
ANONİM ŞİRKETİ"), sometimes ASCII-folded ("TURKIYE GARANTİ BANKASI ANONIM
SIRKETI") — and its name filter is diacritic-SENSITIVE per token. So the adapter:

- strips legal-form boilerplate (`A.Ş.`, `ANONİM ŞİRKETİ`, `SANAYİ VE TİCARET`)
  but keeps distinctive tokens like `HOLDİNG` (a holding ≠ its operating namesake);
- tries diacritic brand-token queries, then ASCII-folded, then a single-token
  last resort — taking whichever returns candidates;
- scores by **brand-token coverage** (not raw string similarity, which breaks on
  the very different name lengths), discounts generic-only matches, and **skips
  fund/foundation candidates** that collide on embedded brand names;
- writes only matches at/above a confidence threshold (default 0.6) to the graph,
  and logs **every** attempt — matched, below-threshold, or no-candidate — to
  `data/cache/gleif_backfill_report.json` for human review.

On a 30-company sample this auto-matched 26 with zero cross-brand false
positives; the 4 misses (e.g. Aksigorta has no GLEIF record under that name; ADESE
trades under a different registered name) are correctly held back for review
rather than guessed. Name matching is INFERRED, never filings-grade — the report
is its provenance record. `GleifAdapter.smoke_check()` + `tests/test_gleif.py`
guard against API drift.

### ISIN selection — equity vs. everything else (verified 2026-06-06)

A GLEIF LEI maps to ALL of an issuer's instruments. Turkish ISINs encode the
class in the 3rd character: **TRA/TRE = common shares**, TRS = rights, TRF =
debt, TRW = warrants. The back-fill picks the listed equity by:

- `TRA+ticker` — a TRA ISIN embedding the full ticker (e.g. Garanti equity
  `TRAGARAN91N1`, never the warrant `TRWGRAN...`). Garanti has 700+ instruments;
  fetching pages and stops as soon as this is found.
- `single-equity` — exactly one equity-class (TRA/TRE) ISIN exists, so it must
  be the listed line (covers newer TRE-coded issuers whose abbreviated code ≠
  ticker, e.g. `TREACSS00017` for ACSEL).
- otherwise it **refuses and logs candidates**: `ambiguous-multi-equity` (several
  share classes, no type field to choose — common for GYOs/holdings with A/B
  groups) or `no-equity-class` (GLEIF lists only debt/rights for that LEI).

Only confident picks are written (to both `Company.isin` and the issued EQUITY
`Security.isin`); the rest land in `data/cache/gleif_isin_report.json` with their
candidate ISINs. This deliberately trades recall for precision — a wrong ISIN
would silently corrupt the Phase-2 price join. Full coverage of the ambiguous
names needs an authoritative BİST/MKK ticker→ISIN map (a later add).

## Live KAP acquisition

```bash
# seed all 729 listed (IGS) companies + equity securities
PYTHONPATH=src python scripts/ingest_kap.py --db ./data/tmkg.kuzu --seed

# seed + pull a year of disclosure metadata for chosen tickers
PYTHONPATH=src python scripts/ingest_kap.py --db ./data/tmkg.kuzu --seed \
    --tickers KCHOL,TUPRS,FROTO --start 2025-01-01 --end 2025-12-31 --cache-raw
```

The member list is cached on disk and refreshed weekly; `--cache-raw` stores raw
disclosure HTML so Phase-3 LLM extraction can re-run without re-hitting KAP.

### kap-client reality check (verified 2026-06-06)

The architecture (§11) recommended leaning on `kap-client`. Half of it works, half
doesn't — exactly why §11 said to isolate it behind an adapter:

- **Member list — BROKEN in kap-client 1.1.1.** It queries stale member-type codes
  and its row model expects field names KAP no longer returns, so
  `fetch_companies()`/`find_company()` return empty. The adapter fetches the member
  list itself from the live `/tr/api/company/items/{TYPE}/{A|P}` endpoint.
- **Disclosures/attachments — work**, but the query keys on `mkkMemberOid`, NOT
  `kapMemberOid`. The adapter delegates to `kap-client`, passing the mkk OID.

`KapAdapter.smoke_check()` (and `tests/test_kap_live.py`) re-verify both halves and
fail loudly if KAP drifts again.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

PYTHONPATH=src python scripts/build_phase1.py --db ./data/tmkg.kuzu --fresh
PYTHONPATH=src python -m pytest tests/ -q
```

Phase-1 exit test output (the "does it earn its keep" check from architecture §6):

```
[KOÇ] TUPRS  w=0.25   (1 hop)
[KOÇ] FROTO  w=0.20   (1 hop)
[KOÇ] ARCLK  w=0.15   (1 hop)
[KOÇ] YKBNK  w=0.10   (1 hop)
[   ] THYAO  w=0.30
>>> Aggregated Koç-group portfolio weight: 70.0%
```

## Decisions made in this build (worth reviewing)

1. **KuzuDB, not Neo4j.** Embedded, no server, runs in CI and offline — best fit for
   "local-first, small team." Revisit if you want Neo4j's GraphRAG/visualization tooling
   or OpenSanctions' native Cypher import (Phase 4).
2. **Reconstructed ontology.** `turkish-markets-kg-ontology.md` was missing from the
   folder, so the schema here was rebuilt from the node/edge vocabulary in the
   architecture doc. If you have the original, diff it against this — names/enums may differ.
3. **`Portfolio` node added** `[design choice]`. The architecture's questions assume
   "my holdings" but no Portfolio node was enumerated. Added with a `HOLDS` edge.
4. **Fixtures are illustrative, not filings-grade.** Stake percentages are approximate and
   some real chains run through intermediate entities (Enerji Yatırımları, Koç Finansal
   Hizmetler) that are collapsed here. They exist to prove the graph and queries work —
   replace with live KAP extraction before trusting any number.

## Archived: corporate-debt subsystem

A working, tested debt/refinancing layer (MKK "Menkul Kıymetler Listesi"
ingestion, nominal/issuance pricing, blast-radius analytics) was built earlier
but served none of the three target pillars. The 2026-06-18 cleanup retired it:
the code, tests, reference data and raw MKK export are archived intact at
`archive/debt-subsystem-2026-06-18.zip`. The debt-specific `Security`/`ISSUES`
schema columns are left in `ddl.py` as **dormant** (unpopulated) to avoid a
risky migration, ready if a "credit-shock" event type revives the subsystem. The
`backfill_gleif.py --stage debt|nominal|issuance|spv|stubs` stages were removed;
surviving stages are `lei, isin, bist, classify, l2, subsidiary, both, all`.

## Outcome — the three pillars (all tested, all NO-GO)

Each pillar was built and run through the same honest venue-feasible / Deflated-Sharpe / PBO gate
(the M4 judge), not a vibe. The verdicts:

1. **Asset correlation** (M3/M5) — a genuine *frictionless* residual edge, but **too thin to
   survive 10bps + borrow** in the venue-feasible book → **NO-GO** (ADR-0004). 92 survivor edges
   landed in L2 `residual_corr`; the cost model was not weakened to manufacture a pass.
2. **Geopolitical-event impact** (M6) — **structurally dead**: negative even frictionless; the
   apparent significance was an overlap / multiple-testing artifact (0 cells survive FDR), not
   rescuable by residual returns, salience filtering, or LLM per-event sign → **NO-GO** (ADR-0005).
   Its §240 channel-stress **risk** output survives as a real deliverable (now the M8.1 risk spine).
3. **Supply-chain / linkage** (M7) — tier-1 firm-level KAP new-business too sparse (~10–15 tradeable
   listed-to-listed edges/yr), tier-2 intra-group is the already-priced null, tier-3 sector-IO has
   no out-of-sample predictability → **NO-GO** (ADR-0006).

**Net: tradeable cross-sectional alpha on BIST residuals is exhausted at n≈500.** The substrate +
risk spine are the durable deliverable; the honest-evaluation protocol that killed three plausible
signals cheaply (before any capital, without weakening an invariant) is itself reusable IP.

**Declined advanced layers:** OpenSanctions PEP/sanction enrichment; GraphRAG NL-over-graph (useful
as an explanation skin, not pursued); GNN overlays (overfit-prone at n≈500, and the alpha case is
already settled).

## Keeping the data current

Today the pipeline is **reproducible but manually triggered** — not a self-updating service. You run
the ingestion scripts (`scripts/ingest_*.py`) and they update the local L1/L2 caches; signal/risk
code then reads only the cache, never the network (§4). The architecture is built *for* safe
automation — **idempotent primary-key writes** (re-running is a no-op), **bitemporal
`knowledge_date`** (new data lands without rewriting history), a **resumable** GDELT backfill,
**fail-loud-never-fabricate**, and the **M8.3 drift/health monitors** as safety rails. What is *not*
yet wired for hands-off operation: a scheduler (cron/launchd/CI), a daily *incremental* ("since last
`knowledge_date`") mode, and **new-listing / IPO onboarding** — a newly-listed name needs its identity
bridge (ticker↔ISIN↔kap_oid↔LEI), price history, universe-class, and a factor/residual refit before
it is tradeable in the substrate. There is an IPO-calendar source available (Matriks), but it is not
yet turned into an automatic onboarding adapter.

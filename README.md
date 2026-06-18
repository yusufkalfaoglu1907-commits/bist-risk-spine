# Turkish Equities Knowledge Graph (tmkg)

Property-graph research substrate over BİST entities — ownership, control, governance,
events, regulation, and macro sensitivity. See `turkish-markets-kg-architecture.md` for
the full design and `turkish-markets-kg-ontology.md` for the schema.

**Status: Phase 1 (identity + ownership core) built and smoke-tested. LIVE KAP
acquisition working — 729 listed companies + disclosure metadata ingest from
www.kap.org.tr. LIVE GLEIF back-fill working — attaches the canonical LEI join
key + legal_form by name match (≈87% auto-match), and the equity ISIN by
instrument-class selection. Both stages are confidence-tiered: only high-
confidence results are written; ambiguous cases are logged for review, never
guessed. SECTOR classification loaded — KAP's two-level Sektörler taxonomy (16
main / 57 sub) attached to the graph: 606/729 companies linked to their leaf
sub-sector, main sector one hop up via `SUBSECTOR_OF`.**

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

## Corporate-debt instruments (debt stage)

The MKK "Menkul Kıymetler Listesi" used for the equity ticker→ISIN map is a
superset: it also registers the issuers' debt. The debt stage turns the graph
from "who owns whom" into "who owes what, due when".

Same provenance-first stance as the equity side — extract once into a committed,
dated reference file, then load from it (no live scrape):

```bash
# 1) extract debt from the MKK export -> data/reference/mkk_debt.json
#    (TRS bonds, TRF financing bills, TRD sukuk, XS Eurobonds by default)
PYTHONPATH=src python scripts/import_mkk_debt.py mkk_list.xlsx \
    --isin-col "ISIN Kodu" --desc-col "Kıymet Açıklama" --issuer-col "MKKÇ Adı"

# 2) attach debt Securities to their issuer Company nodes
PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --stage debt
```

What it does, and what it refuses to guess:

- **ISIN** validated by ISO 6166 shape + check digit — for TR *and* XS codes
  (`is_valid_isin_any`); malformed codes are quarantined, never written.
- **Instrument class → `Security.type`** is a deterministic lookup on the ISIN
  class char: `TRS`→`BOND`, `TRF`→`FINANCING_BILL`, `TRD`→`SUKUK`, `XS`→`EUROBOND`.
- **Maturity** is *inferred* from the description's embedded `DDMMYYYY` run and
  carries a confidence + method (`ddmmyyyy-single` 0.9; `ddmmyyyy-multi` 0.5 when
  issue/coupon dates are also present — latest taken, flagged for review). Never
  asserted as a structured fact; low-confidence parses are logged.
- **Issuer match** reuses the GLEIF brand-token coverage matcher to map each
  issuer short name (`MKKÇ Adı`, e.g. "AK FAKTORİNG") to an existing Company.
  Default scope is **listed-only**: issuers not already in the graph (private
  SPVs, unlisted banks) are logged to `mkk_debt_report.json`, not turned into
  fuzzy-matched new entities. `--create-missing-issuers` opts into the fuller map.
- **Edge provenance**: `(Company)-[:ISSUES {instrument_class, source,
  extraction_method, confidence}]->(Security)`.

> The committed `data/reference/mkk_debt.json` is what the loader reads; re-run
> `import_mkk_debt.py` only when the MKK list is refreshed.

## Not yet built (later phases, per architecture §6)

- **GLEIF Level-2 parent back-fill.** Level-1 (LEI + legal_form) and equity ISINs
  are done; the "who owns whom" relationship records that back-fill cross-border
  parents (`CONTROLS`/`SUBSIDIARY_OF`) are not yet wired.
- **Authoritative ticker→ISIN map.** GLEIF resolves the equity ISIN confidently
  for most names but refuses on ambiguous multi-share-class issuers (see
  `gleif_isin_report.json`). A BİST/MKK reference feed would close that gap.
- **Phase 2:** DuckDB time-series (EVDS macro + BİST OHLCV), ISIN/LEI join, `SENSITIVE_TO`.
- **Phase 3:** LLM extraction of `Event`s from KAP docs; TCMB/mevzuat → `Regulation`/`SUBJECT_TO`.
- **Phase 4:** OpenSanctions PEP/sanction enrichment.
- **Phase 5:** GraphRAG NL → Cypher interface.
```

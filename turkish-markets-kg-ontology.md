# Turkish Equities Knowledge Graph — Ontology (v0.1, reconstructed)

**Companion to:** `turkish-markets-kg-architecture.md` (the system this schema lives in).
**Status:** This file was reconstructed from the node/edge vocabulary enumerated in the architecture doc. The original `turkish-markets-kg-ontology.md` was not present in the project folder when the build started, so treat this as a v0.1 working schema, not the canonical original. Where the architecture is silent, choices are marked `[design choice]`.

**Scope (post 2026-06-18 pre-pillar cleanup):** the populated graph is an **equities ownership/identity core** — `Company`, `Person`, `Security` (equity only), `Sector`, `Portfolio` and the ownership/control edges below. The corporate-debt/refinancing subsystem is **archived** (`archive/debt-subsystem-2026-06-18.zip`): no debt `Security` nodes, no debt `ISSUES` edges, and no fabricated `EXTERNAL_STUB` placeholders are present. The debt-specific `Security`/`ISSUES` columns remain in `ddl.py` as **dormant** (unpopulated). Three pillars are planned on top of this core: (1) asset correlations, (2) geopolitical-event impact, (3) supply-chain dependencies.

---

## 0. Cross-cutting design rules

**Identity spine.** Every real-world entity gets an internal `uuid` (the primary key inside the graph). External identifiers — GLEIF `lei`, `isin`, KAP `oid` — are *attributes/join keys*, never the primary key. This keeps the graph stable when an external id is missing, wrong, or later corrected.

**Provenance on every asserted fact.** Any node or edge that did not come from a deterministic structured field carries three properties:

| property | meaning |
|---|---|
| `source` | URL or identifier of the originating disclosure/dataset |
| `extraction_method` | `structured` \| `llm_extracted` \| `manual` |
| `confidence` | float 0–1 (1.0 for structured/manual; model score for `llm_extracted`) |

This lets you separate filings-grade truth from model inference and re-run extraction with a better model later without polluting facts you trust.

**Temporal stamping.** Ownership and governance change over time. Stake/board/control edges carry `as_of` (and `valid_to` where known) rather than overwriting. `[design choice]` v0.1 keeps the latest assertion plus `as_of`; full bitemporal history is deferred.

**Enum naming follows FIBO as a dictionary** (not loaded into the store): `Security.type`, `Event.type`, index concepts.

---

## 1. Node types

### Company
The central entity — a legal entity, listed or not (subsidiaries and parents may be unlisted).

| property | type | source | notes |
|---|---|---|---|
| `uuid` | STRING (PK) | internal | generated |
| `lei` | STRING | GLEIF | canonical cross-source join key |
| `isin` | STRING | MKK/BİST | for listed equity line |
| `kap_oid` | STRING | KAP | 32-char hex OID, KAP's own PK |
| `ticker` | STRING | BİST | e.g. `KCHOL` |
| `name` | STRING | KAP/GLEIF | legal name |
| `legal_form` | STRING | GLEIF | ELF code |
| `jurisdiction` | STRING | GLEIF | registered country |
| `registration_authority` | STRING | GLEIF | |
| `is_listed` | BOOLEAN | derived | |
| `is_pep` | BOOLEAN | OpenSanctions | Phase 4 |
| `is_sanctioned` | BOOLEAN | OpenSanctions | Phase 4 |

### Person
A natural person — shareholder, director, or executive.

| property | type | source |
|---|---|---|
| `uuid` | STRING (PK) | internal |
| `name` | STRING | KAP |
| `is_pep` | BOOLEAN | OpenSanctions (Phase 4) |
| `is_sanctioned` | BOOLEAN | OpenSanctions (Phase 4) |

### Security
A tradeable instrument issued by a Company.

| property | type | notes |
|---|---|---|
| `uuid` | STRING (PK) | |
| `isin` | STRING | join key to time-series store |
| `ticker` | STRING | |
| `type` | STRING | FIBO Securities: `EQUITY` (populated) \| `PREF` \| `BOND` \| `WARRANT` (debt/other classes dormant — debt subsystem archived) |
| `currency` | STRING | |

### Disclosure
A KAP filing (metadata only; raw document stored on disk by `index`).

| property | type | notes |
|---|---|---|
| `index` | STRING (PK) | KAP disclosure index — uniquely identifies the filing |
| `subject` | STRING | filing subject |
| `disclosure_type` | STRING | KAP type |
| `date` | DATE | |
| `stock_codes` | STRING | |
| `has_attachment` | BOOLEAN | |
| `url` | STRING | source link |

### Event
A corporate action / material event extracted from a disclosure.

| property | type | notes |
|---|---|---|
| `uuid` | STRING (PK) | |
| `type` | STRING | FIBO Corporate Actions: `DIVIDEND` \| `SPLIT` \| `CAPITAL_INCREASE` \| `MERGER` \| `ACQUISITION` \| … |
| `date` | DATE | |
| `description` | STRING | |
| `source` / `extraction_method` / `confidence` | provenance | |

### Regulation
A regulatory instrument from the TCMB/mevzuat connector.

| property | type | notes |
|---|---|---|
| `uuid` | STRING (PK) | |
| `type` | STRING | `KANUN` \| `TEBLIG` \| `TCMB_KARAR` \| `YONETMELIK` \| … |
| `title` | STRING | |
| `ref` | STRING | official reference / number |
| `date` | DATE | |

### Sector
Classification grouping for companies (used by `SENSITIVE_TO`, regulatory blast-radius).
Populated from KAP's two-level "Sektörler" taxonomy: `level` 1 = main sector,
2 = sub-sector (leaf). Companies attach to the leaf; the main sector is one
`SUBSECTOR_OF` hop up.

| property | type | notes |
|---|---|---|
| `code` | STRING (PK) | slug of the name (ASCII-folded, upper-snake) |
| `name` | STRING | |
| `level` | INT64 | 1 = main sector, 2 = sub-sector (leaf) |
| `parent_code` | STRING | parent sector code (null for main sectors) |

### MacroSeries
Metadata for an EVDS macro series; observations live in the time-series store.

| property | type |
|---|---|
| `evds_code` | STRING (PK) |
| `name` | STRING |
| `frequency` | STRING |
| `unit` | STRING |

### Portfolio / `[design choice]`
The architecture's example questions reference "my holdings," but no Portfolio node is enumerated. Added here so the Phase-1 exit query is answerable.

| property | type |
|---|---|
| `uuid` | STRING (PK) |
| `name` | STRING |

---

## 2. Edge (relationship) types

Provenance columns (`source`, `extraction_method`, `confidence`) apply to every edge derived from KAP/LLM. Structured edges set `extraction_method='structured'`, `confidence=1.0`.

| edge | from → to | key properties | source |
|---|---|---|---|
| `HOLDS_STAKE` | Person\|Company → Company | `pct`, `as_of`, provenance | KAP shareholding |
| `CONTROLS` | Person\|Company → Company | `basis`, `as_of`, provenance | KAP / GLEIF L2 |
| `SUBSIDIARY_OF` | Company → Company | `as_of`, provenance | KAP bağlı ortaklık / GLEIF L2 |
| `BOARD_MEMBER_OF` | Person → Company | `role`, `since`, provenance | KAP yönetim kurulu |
| `EXECUTIVE_OF` | Person → Company | `title`, `since`, provenance | KAP |
| `ISSUES` | Company → Security | — | BİST/MKK |
| `HAS_DISCLOSURE` | Company → Disclosure | — | KAP |
| `ABOUT` | Event → Company | provenance | LLM |
| `FROM_DISCLOSURE` | Event → Disclosure | provenance | LLM |
| `SUBJECT_TO` | Company\|Sector → Regulation | provenance | mevzuat |
| `SENSITIVE_TO` | Company\|Sector → MacroSeries | `beta`, `direction` | analytics (Phase 2) |
| `IN_SECTOR` | Company → Sector | — | KAP (links to leaf sub-sector) |
| `SUBSECTOR_OF` | Sector → Sector | — | KAP (sub-sector → main sector) |
| `ASSOCIATE_OF` | Person → Person\|Company | provenance | OpenSanctions (Phase 4) |
| `HOLDS` `[design choice]` | Portfolio → Security | `weight`, `qty` | user |

---

## 3. Phase-1 subset (what this build populates)

Nodes: **Company, Person, Security, Sector, Portfolio**.
Edges: **HOLDS_STAKE, CONTROLS, SUBSIDIARY_OF, BOARD_MEMBER_OF, EXECUTIVE_OF, ISSUES, IN_SECTOR, SUBSECTOR_OF, HOLDS**.

Phase-1 exit test (from architecture §6): *"What is my aggregated exposure to the Koç group across my holdings, weighted by stake?"* — answerable from `Portfolio -HOLDS-> Security <-ISSUES- Company`, then walking `CONTROLS`/`HOLDS_STAKE`/`SUBSIDIARY_OF` up to the controlling group.

Disclosure, Event, Regulation, MacroSeries and their edges are defined above but populated in later phases.

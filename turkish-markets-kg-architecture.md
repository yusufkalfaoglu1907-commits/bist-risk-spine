# Turkish Equities Knowledge Graph — Integration Architecture (v0.1)

**Companion to:** `turkish-markets-kg-ontology.md` (the schema this architecture populates).
**Goal:** research & analysis over BİST entities — exposure mapping, contagion, ownership/control, regulatory and macro sensitivity.
**Stance:** open-source projects (FIBO, GLEIF, OpenSanctions) are *borrowed scaffolding*. The differentiated value is the Turkish layer (KAP, EVDS, MKK, TCMB/mevzuat) built on the property-graph ontology. Local-first; LLM via API at the extraction/query boundary only.

---

## 1. The one-paragraph version

Run a **property graph** (Neo4j Community or KuzuDB) as the operational store. Give every entity an internal UUID and attach the **GLEIF LEI** as the canonical external join key. Fill ownership, control, and board structure primarily from **KAP** (the Turkish disclosure platform), using GLEIF Level-2 only to back-fill cross-border parents. Borrow **FIBO** purely as a naming reference so your node/edge vocabulary stays standards-aligned. Optionally enrich shareholders/directors with **OpenSanctions** PEP/sanction flags (commercial-license caveat). Keep all numbers — prices, volumes, EVDS macro series — in a **separate time-series DB** (DuckDB → TimescaleDB), joined to the graph by ISIN/LEI. Wire your existing **TCMB/mevzuat connector** in as `Regulation` nodes. An **LLM (API)** does entity/relationship extraction from KAP filings and news, and translates your questions into graph + time-series queries (GraphRAG).

---

## 2. Layered architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  L6  INTERFACE / REASONING                                         │
│      LLM (API): KAP & news extraction · NL → Cypher · GraphRAG     │
├──────────────────────────────────────────────────────────────────┤
│  L5  ANALYTICS                                                     │
│      exposure rollups · contagion paths · interlock detection ·   │
│      macro-sensitivity joins · regulatory blast-radius            │
├───────────────────────────────┬──────────────────────────────────┤
│  L4  GRAPH STORE (property)    │  L4'  TIME-SERIES STORE           │
│      Neo4j / KuzuDB            │       DuckDB → TimescaleDB        │
│      identity · ownership ·    │       OHLCV · indicators ·        │
│      control · governance ·    │       EVDS observations           │
│      events · regulation       │   ── joined by ISIN / LEI ──      │
├───────────────────────────────┴──────────────────────────────────┤
│  L3  ENRICHMENT      OpenSanctions (PEP/sanctions) ⚠NC             │
├──────────────────────────────────────────────────────────────────┤
│  L2  OWNERSHIP/CONTROL/GOVERNANCE   ★ KAP (primary) + GLEIF L2     │
├──────────────────────────────────────────────────────────────────┤
│  L1  IDENTITY SPINE    internal UUID + GLEIF LEI + MKK/ISIN/KAP id │
├──────────────────────────────────────────────────────────────────┤
│  L0  ONTOLOGY REFERENCE    FIBO (selective vocabulary alignment)   │
└──────────────────────────────────────────────────────────────────┘
       Regulatory feed: TCMB / mevzuat MCP  →  Regulation nodes (L4)
```

The ★ layer (L2, KAP) is where your analytical value concentrates. Everything below it is plumbing you can borrow; everything above it is the questions you get to answer.

---

## 3. What each source actually contributes to the schema

Mapped onto the v0.1 ontology nodes/edges. "Build" = you ingest/construct it; "Borrow" = off-the-shelf.

### FIBO — *borrow as vocabulary only (do not implement wholesale)*
- Aligns your enums/labels to an industry standard so future interop is cheap.
- Use its **Securities** module to name `Security.type` (`EQUITY`/`PREF`/`BOND`/`WARRANT`), its **Corporate Actions & Events** module to name `Event.type` (`DIVIDEND`/`SPLIT`/`CAPITAL_INCREASE`…), and **Indices & Indicators** for index concepts.
- **Do not** load FIBO OWL into your store. It buys formal semantics you don't need yet and adds heavy query overhead. Treat it as a dictionary you consult, not a layer you run.
- License: MIT ✅.

### GLEIF — *the identity spine (join key), partial ownership back-fill*
- Adds to `Company`: `lei`, `legal_form` (ELF code), `registration_authority`, registered jurisdiction.
- Level-2 "who owns whom" → `CONTROLS` / `SUBSIDIARY_OF` edges **for entities that report** — good for international parents of BİST companies.
- **Limitation that matters:** Level-2 coverage of Turkish private holding structures is patchy. Use it to confirm/extend, never as the sole ownership source.
- Refresh: LEI daily, RDF weekly. License: CC0 ✅. Ingest via REST API (JSON) — no need to adopt RDF.

### KAP — *the core: ownership, control, governance, disclosures* ★
- Primary feed for `Company`, `Person`, `HOLDS_STAKE`, `CONTROLS`, `BOARD_MEMBER_OF`, `EXECUTIVE_OF`, `Disclosure`, `Event`.
- This is the data that makes "true group exposure" answerable. It is public, authoritative, and Turkish-specific — no off-the-shelf KG replaces it.
- Ingestion is partly structured (filing metadata) and partly free text → the LLM extracts `Event` and relationship edges, every one stamped `extraction_method=llm_extracted` + `confidence`.

### OpenSanctions — *risk enrichment* ✅ (personal use)
- Adds risk flags onto existing `Person`/`Company`: `is_pep`, `is_sanctioned`, `sanction_program`, plus `ASSOCIATE_OF` edges from its FollowTheMoney model.
- Relevant slice for you: surfacing when a *controlling shareholder or director* of a holding is a PEP or sanctioned — meaningful in Turkish markets.
- **Import is easy** — it exports **Cypher** directly for Neo4j/Memgraph; no RDF pipeline needed.
- **License:** data is CC-BY-NC. For **personal, non-commercial** research this is fine — use it freely. (Only revisit if this ever becomes a commercial product.)

### EVDS — *macro time-series (separate store)*
- `MacroSeries` metadata nodes in the graph (`evds_code`, `frequency`, `unit`); observations go to DuckDB/TimescaleDB.
- Powers `SENSITIVE_TO` analysis: graph picks the entities, time-series supplies the numbers. Free REST API ✅.

### Matriks / BİST — *price time-series (separate store)*
- OHLCV, indicators, index composition → time-series DB, joined by ISIN.
- **License caveat:** live/historical feeds are licensed; check redistribution/storage terms ⚠.

### TCMB / mevzuat MCP (already connected) — *regulatory layer*
- `Regulation` nodes (`KANUN`/`TEBLIG`/`TCMB_KARAR`…) + `SUBJECT_TO` edges to `Company`/`Sector`.
- Enables "which listed names does this new decision touch?" — a question prices can never answer.

---

## 4. Why a property graph, not a triple-store

The borrowed projects are RDF-native, which tempts you toward a triple-store. Resist it for a *research* graph:

| | Property graph (Neo4j/Kuzu) | RDF triple-store |
|---|---|---|
| Path/exposure/contagion queries | native, fast, readable | verbose, slower |
| GraphRAG / LLM-over-graph | well-supported | workable, heavier |
| Ingesting GLEIF/OpenSanctions | GLEIF via API→map; OpenSanctions exports Cypher natively | native but you inherit OWL weight |
| Formal reasoning / inference | limited | strong (you don't need this yet) |
| Iteration speed for a small team | high | lower |

**Decision:** property graph as the store, RDF only at the import boundary, FIBO as a paper standard. Revisit a triple-store only if formal OWL reasoning becomes a goal.

---

## 5. Data flow

```
KAP filings ─┐
news feeds  ─┼─► LLM extraction (API) ─► candidate nodes/edges
             │      (confidence + source stamped)        │
GLEIF API ───┼─► identity/LEI + L2 mapping ──────────────┤
OpenSanctions├─► Cypher export (PEP/sanction) ───────────┼─► PROPERTY GRAPH
TCMB/mevzuat ┘─► Regulation nodes ───────────────────────┘    (Neo4j/Kuzu)
                                                                  ▲
EVDS API ────► observations ─┐                                    │ ISIN/LEI join
Matriks/BİST ► OHLCV ────────┴─► TIME-SERIES DB (DuckDB/Timescale)┘
```

Reads (analytics, L5) and the LLM interface (L6) sit on top and query both stores.

---

## 6. Phased build order

**Phase 1 — Identity + ownership core (the proof).**
Stand up the property graph. Load BİST `Company`/`Security` with internal UUID + LEI + ISIN. Ingest KAP ownership, control, and board data. *Exit test:* correctly answer "what is my aggregated exposure to the Koç group across my holdings?" If Phase 1 doesn't earn its keep, nothing later does.

**Phase 2 — Time-series + macro join.**
DuckDB with EVDS series and BİST prices. Build the ISIN/LEI join. Add `SENSITIVE_TO` edges. *Exit test:* "show sectors sensitive to USD/TRY and their recent price behaviour."

**Phase 3 — Events + regulation.**
LLM extraction of `Event`s from KAP; wire the TCMB/mevzuat connector to `Regulation`/`SUBJECT_TO`. *Exit test:* "which listed names are affected by last week's capital-increase filings / this new SPK tebliğ?"

**Phase 4 — Risk enrichment.**
OpenSanctions PEP/sanction flags on owners/directors. Import its Cypher export and match to existing `Person`/`Company` nodes. (Free for your personal use.)

**Phase 5 — GraphRAG interface.**
Natural-language → graph+time-series queries; entity-influence ranking over the event graph.

---

## 7. Licensing summary (personal / non-commercial use)

You confirmed this is a **personal, non-commercial** system. That removes the constraints that would have blocked a commercial build — OpenSanctions, FinDKG, and StockKG are all usable for research now.

| Source | Code | Data | Personal use |
|---|---|---|---|
| FIBO | MIT | open standard | ✅ free |
| GLEIF | MIT | CC0 | ✅ free |
| KAP | — | public disclosure | ✅ (respect terms of use / rate limits) |
| EVDS | — | public, API key | ✅ free |
| OpenSanctions | MIT | CC-BY-NC | ✅ free (non-commercial) |
| FinDKG | GPL-3.0 | non-commercial research | ✅ usable for research |
| StockKG (KG-CTF) | unclear | unclear | ⚠ verify license before reuse; no BİST coverage anyway |
| Matriks / BİST live feed | — | proprietary | ⚠ licensed — your account terms govern storage/redistribution |

The only remaining caveat is **Matriks/BİST feed terms** (you're handling that later) and not hammering KAP's endpoints (see §11). If this ever turns commercial, re-check OpenSanctions and FinDKG.

---

## 8. Benefits — what this system gives you

1. **Exposure you can't see in a portfolio table.** Aggregates economic exposure across opaque conglomerate structures — your real concentration, not the per-ticker illusion.
2. **Contagion / cluster risk early.** Find names linked by common controlling owners or interlocking boards *before* a shock propagates through them.
3. **Hidden-correlation discovery.** Surfaces structural links between holdings that look independent on a price chart.
4. **Regulatory blast-radius mapping.** Turns a new TCMB/SPK/BDDK rule into a concrete list of affected companies and sectors — leveraging a connector you already have.
5. **Event propagation across the group graph.** One disclosure → every entity it structurally touches.
6. **Macro-to-equity bridge.** Connects EVDS macro regimes to the specific sectors/names sensitive to them, numbers and structure in one query.
7. **Provenance-graded knowledge.** Every fact carries source + confidence, so you can separate filings-grade truth from model inference — and re-run extraction safely.
8. **Compounding asset.** Unlike a one-off screen, the graph accumulates: each filing ingested makes every future query richer.

## 9. Concrete questions it answers

*Ownership & exposure*
- What is my total economic exposure to the Koç / Sabancı group across all holdings, weighted by stake?
- Two tickers I hold look unrelated — do they share a controlling shareholder or interlocking directors?
- If I add position X, how much does my single-controller concentration rise?

*Contagion & risk*
- Bank Y is in trouble — which non-bank listed names share its controlling group or board members?
- Which companies sit within two ownership hops of a distressed entity?

*Regulation & events*
- This new SPK tebliğ / TCMB kararı — which listed companies and sectors are subject to it?
- Show every company affected by capital-increase or M&A disclosures filed this week, grouped by controlling group.
- Who joined/left boards across my universe last quarter, and where do those people also sit?

*Macro*
- Which sectors are most sensitive to USD/TRY or the policy rate, and how are those names trading now?
- When CPI prints above threshold, which parts of my universe have historically moved?

*Compliance (Phase 4, if licensed)*
- Does any controlling shareholder or director in my universe appear on a sanctions list or as a PEP?

---

## 10. What it deliberately does NOT do

- It is not a price/backtest engine — that lives in the time-series layer and a separate compute step.
- It is not a trade-execution or signal-firing system — it's a *research substrate*.
- It is not a full FIBO/RDF semantic platform — that's heavier than a research team needs.
- It does not replace KAP/EVDS as systems of record — it links and reasons over them.

---

## 11. KAP data acquisition — how we actually pull it

**The honest starting point:** KAP (`kap.org.tr`) has **no official, documented public API**. The site is an Angular single-page app that talks to internal JSON endpoints under `https://www.kap.org.tr/tr/api/...`. Those endpoints are reachable and return clean JSON, but they're undocumented and can change — so we wrap them behind a thin adapter rather than scattering raw URLs through the code. Avoid Selenium/headless-browser scraping; it's slower and more fragile than calling the JSON endpoints directly.

### Recommended path: use a maintained wrapper, don't reinvent

`kap-client` (MIT, actively maintained — v1.1.0, May 2026; deps only `httpx` + `pydantic`) already wraps the real endpoints with retries, back-off, and typed models. Use it as the acquisition layer; fall back to raw endpoints only for things it doesn't cover.

```
pip install kap-client
```

```python
from kap_client import Kap

with Kap() as kap:
    companies = kap.fetch_companies()                 # full member list (cached)
    co = kap.find_company("KCHOL")                    # ticker -> Company(oid, name, ticker)
    disc = kap.fetch_disclosures(co.oid, "2024-01-01", "2024-12-31")
    for d in disc:
        if d.has_attachment:
            for a in kap.fetch_attachments(d.index):  # direct PDF/file download URLs
                ...                                    # /tr/api/file/download/{objId}
```

### The endpoints that matter (under the hood)

| Need | Mechanism |
|---|---|
| Company / fund master list | member-list endpoint → each company gets a **32-char hex OID** (the stable internal key) |
| Disclosures by company + date range | disclosure-query endpoint → `index`, `subject`, `disclosure_type`, `stock_codes`, `has_attachment`, `url` |
| Per-fund filter (no year limit) | `GET /tr/api/disclosure/filter/...` |
| Attachment download | `GET /tr/api/file/download/{objId}` |

**Key for our schema:** the KAP **OID** is the natural value for `Company.kap_member_id` in the ontology, and the `index` uniquely identifies each `Disclosure` node. Join KAP→GLEIF→prices through ticker/ISIN, but keep the OID as KAP's own primary key.

### The part nobody mentions: the data you want is *inside documents*, not in clean fields

This is the most important caveat. The API gives you disclosure **metadata** (who filed, when, what subject, attachment links). It does **not** hand you ownership percentages, board rosters, or subsidiary lists as structured fields. Those live in the disclosure **content** — KAP's HTML templates and PDF attachments. So the real pipeline is:

```
1. fetch_companies()                       → seed Company nodes (oid, name, ticker)
2. fetch_disclosures(oid, range)           → Disclosure nodes (metadata)
3. filter by subject to the KG-relevant types (see below)
4. fetch_attachments() / fetch document    → raw HTML/PDF
5. LLM extraction (API)                    → structured edges:
                                             HOLDS_STAKE, CONTROLS, BOARD_MEMBER_OF, Event
6. stamp every edge: source=disclosure URL, extraction_method=llm_extracted, confidence
7. upsert into the property graph
```

Steps 1–4 are deterministic plumbing (kap-client handles them). Step 5 is where the LLM earns its place and where your `confidence`/provenance design (ontology §0) pays off.

### Disclosure subjects worth targeting first

For the ownership/control core (Phase 1), prioritise these filing types:
- **Ortaklık yapısı / pay sahipliği** (shareholding structure, >5% holders) → `HOLDS_STAKE`, `CONTROLS`
- **Yönetim kurulu** changes (board appointments/resignations) → `BOARD_MEMBER_OF`
- **Bağlı ortaklık / iştirak** (subsidiaries & participations) → `SUBSIDIARY_OF`
- **Özel durum açıklaması** (material event disclosures) → `Event`
- **Finansal raporlar** (financial reports) → financial attributes / time-series side

### Operational rules

- **Cache the member list and OIDs locally** — fetch once, refresh weekly. Don't re-pull on every run.
- **Respect rate limits.** The endpoints return HTTP 429 under load; honour back-off (kap-client does) and run ingestion as a scheduled nightly batch, not a tight loop.
- **Store raw documents.** Keep every fetched HTML/PDF on disk keyed by `index`, so you can re-run LLM extraction with a better model later without re-hitting KAP.
- **Date-range quirk:** some endpoints require start/end within the same calendar year — loop year by year for history.
- **Treat the endpoints as unstable.** Pin `kap-client`, isolate it behind your own `kap_adapter` module, and add a smoke test so you notice fast if KAP changes its API.

> Net: KAP is a *disclosure feed you parse*, not an ownership database you query. The acquisition is easy; the value comes from the LLM extraction step that turns filings into graph edges.

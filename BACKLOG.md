# Backlog / planned work

Deferred items with enough detail to pick up cold. Each notes its **trigger** —
the stage at which it should be raised.

---

## GLEIF Level-2 control edges — ✅ DONE 2026-06-08

Built `CONTROLS` / `SUBSIDIARY_OF` from GLEIF Level-2 consolidation parents.
`adapter.fetch_parents()` (direct + ultimate parent, 404 = no-parent, cached in
`gleif_parents.json`) → `loaders/gleif_l2_backfill.py` → `backfill_gleif.py
--stage l2`. In-universe-only by default (`--create-missing-parents` opts into
external parent nodes), confidence 0.95, source `GLEIF-L2`, idempotent MERGE.
**Result: 101 CONTROLS + 89 SUBSIDIARY_OF** (89 direct, 12 ultimate-only); top
controllers SAHOL 11 / İŞ BANK 10 / KOÇ 9 / DOHOL 4. 502 no-parent, 74+36
external parents logged-not-invented, 0 errors. Report:
`data/cache/gleif_l2_report.json`. 5 offline tests in `test_gleif_l2.py`.
**Prereq that had to be fixed first:** the persisted graph was missing the whole
L1 enrichment (0 LEI/ISIN) — restored via `--stage bist` (583 ISIN, offline) +
live `--stage lei` (671 LEI) before L2 could key off LEIs. **Possible follow-up:**
multi-hop blast-radius queries over CONTROLS (e.g. group-level contagion from a
single subsidiary's debt wall).

---

## Sector classification (KAP Sektörler) — ✅ DONE 2026-06-07

Loaded KAP's two-level sector taxonomy onto the live graph. `Sektörler.xlsx`
(KAP Sektörler listing) → `scripts/import_sectors.py` → committed
`data/reference/sectors.json` → `scripts/backfill_sectors.py` → graph.
73 sectors (16 main, 57 sub); 606/729 companies linked to their leaf sub-sector,
main sector one `SUBSECTOR_OF` hop up; 123 unmatched (debt-only/funds) logged in
`data/cache/sector_backfill_report.json`, never guessed. Adapter + 7 offline
tests green. Schema gained `Sector.level`, `Sector.parent_code`, and the
`SUBSECTOR_OF` rel (additive migrations). **Refresh** = re-export KAP Sektörler →
re-run the two scripts. **Possible follow-up:** wire `SUBJECT_TO`/`SENSITIVE_TO`
to use the new sector roll-up for Phase-2/3 blast-radius queries.

---

## Corporate-debt & sukuk instruments from the MKK securities list

**Status:** ✅ MACHINERY BUILT 2026-06-07 — `mkk_debt_adapter` + `debt_backfill`
loader + `scripts/import_mkk_debt.py` + `backfill_gleif.py --stage debt` + 16
offline tests (all green) + end-to-end verified on synthetic data. Issuer-scope
decision MADE: **listed-only by default** (unmatched issuers logged, not created;
`--create-missing-issuers` opts into the fuller map). See README "Corporate-debt
instruments".
**DATA PENDING (the only remaining step):** the raw MKK "Menkul Kıymetler
Listesi" export is no longer present (`uploads/` clears between sessions; only the
parsed *equity* `bist_isin.json` was committed, not the raw rows). To populate the
graph, RE-SUPPLY the MKK xlsx, then `scripts/import_mkk_debt.py …` → `--stage debt`.
Original spec retained below.
**Trigger — raise this when:** starting **Phase 2** (Security modeling /
time-series), or any time we next add/extend `Security` nodes or work on
leverage / refinancing / contagion analysis. Surface it then.

### Why it's worth doing
The MKK "Menkul Kıymetler Listesi" already in hand
(`uploads/` → parsed by `scripts/import_bist_isin.py`) is a superset: beyond the
1,720 equity lines it holds the issuers' **debt**. Modeling it turns the graph
from "who owns whom" into "who owes what, due when" — the substrate for
refinancing-wall and contagion analysis named in the project's purpose.

### Scope (what to include / exclude)
Verified counts from the 24,997-row file (2026-06-07):

Include (high signal):
- `TRS` corporate bonds — 301
- `TRF` financing bills (finansman bonosu) — 538
- `TRD` lease certificates / sukuk (kira sertifikası) — 621
- `XS…` Eurobonds (FX-debt contagion) — ~840 (XS1/XS2/XS3)
- → **216 distinct corporate-debt issuers** (TRS/TRF/TRD)

Exclude (noise for this project):
- `TRX` ELÜS commodity/warehouse receipts — 7,925 (agricultural, not corp finance)
- `NLB` / `GB0` / other foreign structured certs — ~3,000
- `TRW` warrants — 6,245 (only if/when modeling derivatives exposure; low priority)

### Issuer-scope decision (was the open question)
Two options — pick at build time:
- **Listed issuers only** — attach debt `Security` nodes to companies already in
  the graph; skip private/SPV issuers. Clean, smaller, high-signal. *(leaning)*
- **All debt issuers** — also create issuer nodes for non-listed banks /
  factoring firms / lease-cert SPVs (e.g. "DK VARLIK KİRALAMA"). Fuller contagion
  map but expands the entity universe + adds fuzzy-match risk.

### Modeling notes (so it's not re-derived)
- New `Security.type` values: `BOND` / `FINANCING_BILL` / `SUKUK` / `EUROBOND`
  (+ `WARRANT` if ever included). Edge: `(Issuer)-[:ISSUES]->(Security)`.
- **Maturity date is parseable from the description** free-text, e.g.
  "…ÖZEL SEKTÖR TAHVİLİ **25052027**…" → 2027-05-25 (DDMMYYYY). Some rows embed
  the ISIN again in the text. Inferred → **confidence-tag** like the GLEIF fields;
  log low-confidence parses to an audit report, never guess silently.
- Issuer match: the file's `MKKÇ Adı` is a SHORT name ("AK FAKTORİNG",
  "ULUSAL FAKTORING") — reuse the GLEIF/brand-token matching approach
  (`gleif_adapter`), same diacritic/legal-suffix caveats. Many issuers are NOT
  listed equities, so matching must tolerate "no Company node" and (per scope)
  either skip or create an issuer node.
- Reuse the import plumbing: `scripts/import_bist_isin.py` already reads this
  xlsx robustly and validates ISO 6166 check digits — generalize it (a
  `--classes` filter) rather than writing a new parser.
- ISIN check-digit validation already exists in `bist_isin_adapter.py`
  (`is_valid_isin`) — reuse it; ~50 rows in this file have malformed check digits
  and must stay excluded.

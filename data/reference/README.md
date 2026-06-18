# BİST/MKK ticker→ISIN reference

`bist_isin.json` is the **authoritative** ticker→ISIN map used to close the
residual ISIN gap that the GLEIF back-fill refuses to guess
(`ambiguous-multi-equity` and `no-equity-class` — see
`src/tmkg/adapters/gleif_adapter.py`).

## Why it is a committed file, not a live scrape

Verified 2026-06-06: there is **no public, automatable bulk ticker→ISIN feed**
from the obvious BİST/MKK surfaces.

| Source | Result |
|---|---|
| KAP company export (`/tr/api/company/generic/pdf/IGS/A/...`) | ticker, title, city, auditor — **no ISIN** |
| KAP company summary pages | ISIN not present in server-rendered HTML |
| MKK ISIN registry (`mkk.com.tr`) | login-gated; no bulk page |
| İş Yatırım `Data.aspx` endpoints | **401 Unauthorized**; company card shows no ISIN |
| Borsa İstanbul site | equity/companies data pages error / no clean file |

A provenance-first project therefore treats this as a **dated reference list**:
versioned on disk, carrying its `source` and `fetched_iso`, validated on load,
and never silently re-derived.

## Schema

```json
{
  "source":      "<where the data came from>",
  "fetched_iso": "<YYYY-MM-DD>",
  "method":      "seed-verified | official-export | chrome-rendered",
  "schema_version": 1,
  "complete":    false,
  "mappings":    { "TICKER": "ISIN", ... }
}
```

Every ISIN is validated on load: Turkish shape (`^TR[A-Z][0-9A-Z]{8}[0-9]$`)
**and** the ISO 6166 check digit. Invalid codes are quarantined (never written
to the graph) and surfaced via `BistIsinAdapter.rejected`.

## Current state

`complete: true`. Imported from the **MKK "Menkul Kıymetler Listesi"**
(Merkezi Kayıt İstanbul) on 2026-06-07:

- **1,014** tickers in `mappings` (unambiguous listed-equity ISIN).
- **150** tickers in `ambiguous` (genuine multi-group splits with no privileged
  marker, e.g. PRMS A/B/C/D) — preserved as candidate lists, NOT served as
  lookups, awaiting which-group-trades info.
- ~50 rows in the source with malformed ISO 6166 check digits were rejected
  (confirmed malformed against `python-stdnum`), never written.

Disambiguation of common-vs-privileged: where a ticker had several equity ISINs,
the line whose description contains **İMTİYAZLI** (privileged/founder shares,
not the publicly-traded common line) is dropped; if exactly one common line
remains it is taken (e.g. `TUPRS → TRATUPRS91E8`, not the İMTİYAZLI `TRETPRS…`).
All four GLEIF-verified anchors resolve correctly.

## How to populate / refresh

1. **Official export (preferred).** Export the securities list from the MKK ISIN
   registry (or a Borsa İstanbul equity list) — CSV/XLSX with a ticker column,
   an ISIN column, and ideally a description column — then:

   ```bash
   PYTHONPATH=src python scripts/import_bist_isin.py path/to/export.xlsx \
       --ticker-col "Borsa Kodu" --isin-col "ISIN Kodu" --desc-col "Kıymet Açıklama"
   ```

   The importer reads .xlsx directly (style-independent), keeps only TRA/TRE
   equity ISINs, validates every code (shape + ISO 6166 check digit), resolves
   common-vs-privileged via `--desc-col`, writes `bist_isin.json` with
   provenance, and reports rejected/ambiguous rows.

2. **Rendered route.** Drive a logged-in source that displays ISIN per ticker
   via Claude in Chrome and write the same JSON.

Then run the back-fill stage:

```bash
PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --stage bist
```

---

# KAP sector classification reference

`sectors.json` is the **authoritative** company→sector classification. KAP's
member-list endpoint carries no sector field, so the live graph seeds Company
identity with **no** `Sector` nodes or `IN_SECTOR` edges; this file fills that
gap from KAP's "Sektörler" listing.

## Structure — a two-level taxonomy

KAP classifies each company into a **main sector** (level 1, e.g. `İMALAT`) and a
**sub-sector** (level 2, the leaf, e.g. `KİMYA İLAÇ PETROL…`). A sub-sector's
member set is always a subset of its parent's. The importer reconstructs both
levels purely from that containment property (no hard-coded names) and writes:

- `sectors`: every node as `{code, name, level, parent}` (`parent` is null for
  main sectors). `code` is an ASCII-folded upper-snake slug of the name; name
  collisions between a main and sub sector (e.g. `ULAŞTIRMA VE DEPOLAMA` appears
  at both levels) are suffixed, the main sector keeping the base slug.
- `memberships`: `TICKER → leaf code`. A company links to its **leaf**; the main
  sector is one `SUBSECTOR_OF` hop up. Legacy/secondary codes from multi-code
  cells (`GARAN, TGB`) are included so any ticker variant the graph holds resolves.

## Current state

`complete: true`. Imported from **`Sektörler.xlsx`** (KAP Sektörler listing) on
2026-06-07:

- **73 sectors** (16 main, 57 sub).
- **630** ticker→leaf mappings; **606 / 729** live companies link (the 123
  unmatched are debt-only issuers, funds, and names absent from the equities
  taxonomy — left unlinked, never guessed; see
  `data/cache/sector_backfill_report.json`).

## How to populate / refresh

1. Export KAP's Sektörler listing to .xlsx (the page layout flattened to a grid:
   a sector-name row, a `Sıra | Kod | Şirket Unvanı` band, then member rows).
2. Regenerate the reference file:

   ```bash
   PYTHONPATH=src python scripts/import_sectors.py "Sektörler.xlsx" \
       --source "KAP Sektörler listing (kap.org.tr) export"
   ```

3. Apply to the graph:

   ```bash
   PYTHONPATH=src python scripts/backfill_sectors.py --db ./data/tmkg.kuzu
   ```

`SectorAdapter.smoke_check()` validates tree integrity (parent refs resolve,
memberships point at leaves) plus a few ticker→main anchors; `tests/test_sectors.py`
guards the parse/load/round-trip.

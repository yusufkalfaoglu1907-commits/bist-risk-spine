# Finance KG — Cleanup Plan (pre-pillar reset)

**Author:** prepared 2026-06-18 for execution by Claude Code
**Goal:** Strip the project down to a clean, trustworthy *equities ownership/identity* core, then build three pillars on top: (1) asset correlations, (2) geopolitical-event impact, (3) supply-chain dependencies. The debt/refinancing-wall subsystem is **off-mission** and is being retired (archived, not destroyed). Fabricated and stale data is being purged.

> **Why this exists:** A 2026-06-18 audit found the debt layer was the most elaborate part of the repo yet served none of the three stated goals, and the live graph contained fabricated placeholder nodes (`EXTERNAL_STUB`) plus ~1,950 debt instruments that add noise to an equities-focused graph. This plan removes that surface area before new work starts.

---

## 0. NON-NEGOTIABLE SAFETY STEP (do this first)

There is **no git repository** in this folder. Every deletion is irreversible without a backup. Before touching anything:

```bash
cd "<repo root>"
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p ../FinanceKG-backups
# Full snapshot EXCLUDING the heavy, reproducible .venv
tar --exclude='.venv' --exclude='**/__pycache__' -czf "../FinanceKG-backups/finance-kg-pre-cleanup-$TS.tar.gz" .
ls -lh "../FinanceKG-backups/finance-kg-pre-cleanup-$TS.tar.gz"
```

Confirm the tarball exists and is >40MB (it contains the 45MB DB) before proceeding. **Also initialize git now** so future changes are recoverable:

```bash
git init && printf ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\ndata/*.kuzu*\n" > .gitignore
git add -A && git commit -m "Snapshot before pre-pillar cleanup"
```

---

## 1. KEEP — the on-mission core (do not touch)

**Source (`src/tmkg/`):**
- adapters: `kap_adapter.py`, `gleif_adapter.py`, `bist_isin_adapter.py`, `sector_adapter.py`, `kap_subsidiary_adapter.py`, `tabular.py`
- loaders: `identity.py`, `kap_ingest.py`, `gleif_backfill.py`, `gleif_l2_backfill.py`, `bist_isin_backfill.py`, `sector_backfill.py`, `kap_subsidiary_backfill.py`, `ownership.py`
- analytics: `exposure.py`
- schema: `ddl.py`, `integrity.py`; plus `graph/`, `config.py`

**Scripts:** `ingest_kap.py`, `backfill_gleif.py` (after trimming — see §4), `import_sectors.py`, `backfill_sectors.py`, `import_bist_isin.py`, `build_phase1.py`, `discover_kap_subsidiaries.py`

**Tests:** `test_phase1.py`, `test_gleif.py`, `test_gleif_l2.py`, `test_bist_isin.py`, `test_sectors.py`, `test_kap_subsidiary.py`, `test_integrity.py`, `test_kap_live.py`

**Data kept:** `data/reference/{bist_isin.json, sectors.json, kap_subsidiary.json, isin_disambiguation.json, README.md}`; `data/cache/{kap_members.json, gleif_lookups.json, gleif_parents.json, gleif_backfill_report.json, gleif_l2_report.json, bist_isin_report.json, sector_backfill_report.json, kap_subsidiary_report.json}`

**Docs kept:** `README.md` (update after cleanup), `turkish-markets-kg-architecture.md`, `turkish-markets-kg-ontology.md`

---

## 2. ARCHIVE — the debt subsystem (move out, don't delete)

This is working, tested machinery that may return later as a "credit-shock" event type. Move it into a zip, then remove from the active tree.

```bash
mkdir -p archive
```

Move these into `archive/debt-subsystem/` (preserving the `src/tmkg/...` sub-paths), then zip and remove the loose copy:

- adapters: `mkk_debt_adapter.py`, `kap_issuance_adapter.py`, `kap_nominal_adapter.py`
- loaders: `debt_backfill.py`, `nominal_backfill.py`, `kap_issuance_backfill.py`, `external_stub_backfill.py`, `spv_parent_backfill.py`
- analytics: `blast_radius.py`, `outstanding.py`
- scripts: `import_mkk_debt.py`, `extract_kap_nominals.py`, `refresh_nominals.py`, `discover_kap_issuances.py`, `discover_kap_fx_issuances.py`, `blast_radius.py`
- tests: `test_mkk_debt.py`, `test_outstanding.py`, `test_blast_radius.py`, `test_kap_nominal.py`, `test_kap_issuance.py`, `test_kap_fx_issuance.py`, `test_external_stub.py`, `test_spv_parent.py`
- data/reference: `mkk_debt.json`, `kap_nominal.json`, `kap_issuance.json`, `kap_fx_issuance.json`
- data/cache: `mkk_debt_report.json`, `spv_parent_report.json`, `external_stub_report.json`
- raw source: `Menkul Kıymetler Listesi  Merkezi Kayıt İstanbul.xlsx` (8.4 MB)

```bash
# after moving the files under archive/debt-subsystem/
( cd archive && zip -r debt-subsystem-2026-06-18.zip debt-subsystem && rm -rf debt-subsystem )
```

Keep `archive/*.zip` in the folder (or move to `../FinanceKG-backups/` if you want the working folder even leaner).

---

## 3. DELETE — junk and stale process docs

**Junk (safe to remove outright):**
```bash
rm -f data/ovtest.txt data/testwrite
rm -rf .pytest_cache pytest-cache-files-* 
find . -path ./.venv -prune -o -name '__pycache__' -type d -print -exec rm -rf {} +
```

**Stale audit/process docs** — superseded by this plan. Move to `archive/docs/` (history, not deletion):
- `AUDIT-2026-06-11.md`, `FIXPLAN.md`, `audit-fix-plan.md`, `BACKLOG.md`

```bash
mkdir -p archive/docs && mv AUDIT-2026-06-11.md FIXPLAN.md audit-fix-plan.md BACKLOG.md archive/docs/
```

**`Sektörler.xlsx`** (raw sector source, already ingested into `sectors.json`): move to `archive/` — not needed in the active tree.

---

## 4. CODE — trim `backfill_gleif.py` and re-validate schema

`scripts/backfill_gleif.py` imports the archived debt loaders and exposes stages `debt, nominal, issuance, spv, stubs`. After §2 those imports break.

- Remove the `debt, nominal, issuance, spv, stubs` entries from the `--stage` `choices=(...)`.
- Delete their handler branches and the now-dangling imports of the archived loaders.
- Keep stages: `lei, isin, bist, classify, l2, subsidiary, both, all` (redefine `all` to exclude debt stages).
- Grep the whole `src/` + `scripts/` tree for any remaining import of an archived module and fix/remove:
  ```bash
  grep -rn "mkk_debt\|kap_issuance\|kap_nominal\|debt_backfill\|nominal_backfill\|external_stub\|spv_parent\|blast_radius\|outstanding" src scripts | grep -v archive
  ```
- **Schema (`ddl.py`):** the debt-specific columns on `Security` (`nominal`, `maturity_date`, `is_amortizing`, `issue_date`, …) and on `ISSUES` (`instrument_class`, …) are harmless when unpopulated. **Leave them** for now to avoid a risky migration; note them as dormant. (Equity `Security` nodes and the `ISSUES` table are still used.)

---

## 5. GRAPH — rebuild clean instead of surgical deletion

Rather than running `DELETE` queries against the live 45MB DB (error-prone, and the mount has the known Kuzu lock gotcha), **rebuild a fresh graph from on-mission stages only**. This deterministically yields a graph with **zero debt instruments and zero external stubs**.

> **Kuzu mount gotcha:** you cannot open/build the DB directly on this synced folder from a sandbox (stale `.shadow`/`.wal` can't be cleared). Build at `/tmp/clean.kuzu`, then copy the main file back with `cat /tmp/clean.kuzu > data/tmkg.kuzu` (do not try to `rm` the sidecars).

Recipe (run with `PYTHONPATH=src`, against `/tmp/clean.kuzu`):

1. `ingest_kap.py --seed` — 729 companies + equity securities (offline, from `kap_members.json`)
2. `backfill_gleif.py --stage classify` — `listing_status` tagging
3. `import_sectors.py` then `backfill_sectors.py` (or the sector stage) — sector tree + `IN_SECTOR`
4. `backfill_gleif.py --stage bist` — authoritative ISINs (offline)
5. `backfill_gleif.py --stage lei` then `--stage isin` — GLEIF identity (network; `gleif_lookups.json` cache covers most)
6. `backfill_gleif.py --stage l2` — GLEIF control edges (network/`gleif_parents.json` cache)
7. `backfill_gleif.py --stage subsidiary` — KAP ownership edges (offline from `kap_subsidiary.json`)

**Explicitly DO NOT run:** `debt`, `nominal`, `issuance`, `spv`, `stubs`.

Then `cat /tmp/clean.kuzu > data/tmkg.kuzu`.

**Decision on `spv`/`stubs`:** dropped. The `spv` stage produced 10 control edges but all target debt-issuer SPVs; the `stubs` stage fabricated the 55 `EXTERNAL_STUB` placeholder nodes (noise). Both are excluded from the clean graph. Real foreign/holding parents created by the `l2` and `subsidiary` stages (`EXTERNAL_PARENT`: Çalık, Akfen, Al Baraka Group, Carrier Global, …) are **retained** — they are valuable for the geopolitical pillar.

---

## 6. VERIFY — acceptance checks (must pass before declaring done)

Run a read-only audit on the rebuilt graph and assert:

| Check | Expected |
|---|---|
| `Company` count | ~729 + real external parents (no fabricated stubs) |
| `Company` where `listing_status='EXTERNAL_STUB'` | **0** |
| `Security` count | ~729 (equity only; **no XS/TRF/TRD/TRS**) |
| `ISSUES` where `instrument_class IN ['XS','TRF','TRD','TRS']` | **0** |
| `CONTROLS` count | ~212 (GLEIF-L2 + KAP), all real entities |
| Equity-traded coverage | ISIN ~100%, sector ~100%, LEI ~90% |
| Top controllers | Sabancı, Koç, İş Bankası, OYAK, QIA, BBVA still present |

Then:
```bash
PYTHONPATH=src python -m pytest -q   # full suite green AFTER archived tests removed
grep -rn "<archived module names>" src scripts | grep -v archive   # must be empty
```

Update `README.md` and `turkish-markets-kg-ontology.md` to describe the equities-focused scope and the three pillars; note the debt subsystem is archived at `archive/debt-subsystem-2026-06-18.zip`.

---

## 7. OUT OF SCOPE (the next phase, not this cleanup)

After cleanup, the build order for the three pillars (rationale in the audit) is:
1. **Price time-series** — keystone for correlations *and* event studies; lowest data risk (BİST market-data MCP available).
2. **Geopolitical-event impact** — `Event` + `SENSITIVE_TO`, measurable once returns exist.
3. **Supply-chain dependencies** — hardest data problem (no clean TR feed; needs LLM extraction from KAP filings or curation); do last.

Do **not** start pillar work until §6 passes.

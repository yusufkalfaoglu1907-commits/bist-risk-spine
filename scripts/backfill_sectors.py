#!/usr/bin/env python3
"""Back-fill the KAP sector taxonomy onto the live graph.

Reads the committed sector reference (`data/reference/sectors.json`, produced by
`scripts/import_sectors.py`) and writes Sector nodes + SUBSECTOR_OF hierarchy +
IN_SECTOR edges from existing Company nodes to their leaf sub-sector. Idempotent.

Usage:
    # apply to the live DB
    PYTHONPATH=src python scripts/backfill_sectors.py --db ./data/tmkg.kuzu

    # dry-run summary only (no graph writes, no report)
    PYTHONPATH=src python scripts/backfill_sectors.py --db ./data/tmkg.kuzu --dry-run
"""
from __future__ import annotations

import argparse

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.sector_adapter import SectorAdapter
from tmkg.loaders.sector_backfill import backfill


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="Kuzu db path (default from config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report reference + match coverage without writing")
    args = ap.parse_args()

    adapter = SectorAdapter().load(strict=True)
    info = adapter.smoke_check()
    print(f"reference: {info['sectors']} sectors ({info['main']} main, "
          f"{info['sub']} sub), {info['memberships']} ticker mappings")
    print(f"  source: {info['source']} ({info['fetched_iso']})")

    conn = connect(args.db)
    apply_schema(conn)

    if args.dry_run:
        res = conn.execute(
            "MATCH (c:Company) WHERE c.ticker IS NOT NULL AND c.ticker <> '' "
            "RETURN c.ticker")
        matched = unmatched = 0
        while res.has_next():
            if adapter.lookup(res.get_next()[0]).found:
                matched += 1
            else:
                unmatched += 1
        print(f"\n[dry-run] companies that would be linked: {matched}")
        print(f"[dry-run] companies with no sector match:  {unmatched}")
        return

    report = backfill(conn, adapter)
    cov = report["coverage"]
    print("\n=== Sector back-fill ===")
    print(f"  Sector nodes:      {report['sector_nodes']}")
    print(f"  SUBSECTOR_OF edges: {report['subsector_edges']}")
    print(f"  Companies linked (KAP): {report['companies_linked']} / "
          f"{report['companies_total']}")
    print(f"  Companies unmatched: {report['companies_unmatched']} "
          f"(left unlinked, see report)")
    print(f"  Sub-sectors populated: {report['leaves_populated']}")
    print(f"  Inherited over CONTROLS (F8): {report['inherited_sectors']}")
    print("  Coverage:")
    print(f"    company-weighted:    {cov['company_weighted_coverage']:.1%} "
          f"({cov['companies_sectored']}/{cov['companies_total']}, "
          f"+{cov['companies_sectored_inherited']} inherited)")
    print(f"    instrument-weighted: {cov['instrument_weighted_coverage']:.1%} "
          f"({cov['instruments_sectored']}/{cov['instruments_total']})")
    print(f"  -> {report.get('report_path')}")


if __name__ == "__main__":
    main()

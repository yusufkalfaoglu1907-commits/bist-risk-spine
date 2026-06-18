#!/usr/bin/env python3
"""Live KAP ingest into the property graph.

Examples:
    # seed all listed (IGS) companies + securities, no disclosures
    PYTHONPATH=src python scripts/ingest_kap.py --db ./data/tmkg.kuzu --seed

    # seed, then pull 2025 disclosures for a few tickers
    PYTHONPATH=src python scripts/ingest_kap.py --db ./data/tmkg.kuzu --seed \
        --tickers KCHOL,TUPRS,FROTO --start 2025-01-01 --end 2025-12-31

Requires network access to www.kap.org.tr.
"""
from __future__ import annotations

import argparse

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_adapter import KapAdapter, ALL_MEMBER_TYPES
from tmkg.loaders import kap_ingest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--seed", action="store_true", help="seed Company/Security from KAP member list")
    ap.add_argument("--all-types", action="store_true", help="include non-IGS member types")
    ap.add_argument("--include-unlisted", action="store_true")
    ap.add_argument("--tickers", default="", help="comma list, e.g. KCHOL,TUPRS")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--cache-raw", action="store_true", help="also store raw disclosure HTML")
    ap.add_argument("--refresh-members", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    apply_schema(conn)

    member_types = ALL_MEMBER_TYPES if args.all_types else ("IGS",)

    with KapAdapter() as adapter:
        if args.seed:
            stats = seed = kap_ingest.seed_companies_from_kap(
                conn, adapter, member_types=member_types,
                listed_only=not args.include_unlisted, refresh=args.refresh_members,
            )
            print(f"Seeded: {stats}")

        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if tickers and args.start and args.end:
            members = adapter.fetch_members(member_types=member_types)
            total = 0
            for t in tickers:
                try:
                    m = adapter.find(t, members)
                except KeyError:
                    print(f"  ! {t}: not found in member list")
                    continue
                n = kap_ingest.ingest_disclosures(
                    conn, adapter, m, args.start, args.end, cache_raw=args.cache_raw
                )
                total += n
                print(f"  {t} ({m.name}): {n} disclosures")
            print(f"Total disclosures ingested: {total}")

    # quick summary
    for label, q in [
        ("Company", "MATCH (c:Company) RETURN count(c)"),
        ("Security", "MATCH (s:Security) RETURN count(s)"),
        ("Disclosure", "MATCH (d:Disclosure) RETURN count(d)"),
    ]:
        print(f"  graph {label}: {conn.execute(q).get_next()[0]}")


if __name__ == "__main__":
    main()

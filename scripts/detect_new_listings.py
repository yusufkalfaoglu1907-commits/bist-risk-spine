"""Onboarding step 3a — detect names listed upstream (cached KAP member list) but not yet in the
substrate. Writes data/cache/new_listings_report.json (§4). Pure local read — no network, no mutation.

    PYTHONPATH=src python scripts/detect_new_listings.py
"""
from __future__ import annotations

from tmkg.graph.connection import connect
from tmkg.ingest.new_listings import detect_new_listings


def main() -> int:
    con = connect()
    rep = detect_new_listings(con, write_report=True)

    print("\n=== NEW-LISTING DETECTION (diff key: kap_oid) ===")
    print(f"  upstream : {rep['upstream_source']}")
    print(f"  graph    : {rep['n_known_in_graph']} known · upstream {rep['n_upstream_listed']} listed")
    print(f"  IN SYNC  : {rep['in_sync']}")
    if rep["new_listings"]:
        print(f"\n  NEW to onboard ({rep['n_new']}):")
        for n in rep["new_listings"][:50]:
            print(f"    {(n.get('ticker') or '?'):10} {(n.get('name') or '')[:48]}  [{n['kap_oid']}]")
    if rep["retired_candidates"]:
        print(f"\n  retired candidates (kept, survivorship — flag only, {rep['n_retired_candidates']}):")
        print(f"    {', '.join(r['ticker'] or r['kap_oid'] for r in rep['retired_candidates'][:30])}")
    if rep["ticker_changes"]:
        print(f"\n  ticker changes to reconcile (id-bridge, {rep['n_ticker_changes']}):")
        for t in rep["ticker_changes"][:30]:
            print(f"    {t['graph_ticker']} -> {t['upstream_ticker']}  [{t['kap_oid']}]")
    print("\nReport: data/cache/new_listings_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

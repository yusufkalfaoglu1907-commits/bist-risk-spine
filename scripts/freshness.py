"""M9.2 — data-freshness scan. For every listed name, report whether its L2 price/return data is
current / stale / missing vs a freshness budget. Read-only (L1 + bulk L2; no network). The daily
incremental runner uses this to target only the stale names (pulling each one's incremental window).

    PYTHONPATH=src python scripts/freshness.py [MAX_AGE_DAYS]
"""
from __future__ import annotations

import sys
from datetime import date

from tmkg.graph.connection import connect
from tmkg.l2.store import L2Store
from tmkg.ingest.incremental import freshness_report


def main(argv: list[str]) -> int:
    max_age = int(argv[1]) if len(argv) > 1 else 7
    con = connect()
    store = L2Store()
    store.bootstrap_schema()
    r = freshness_report(con, store, as_of=date.today(), max_age_days=max_age)

    print(f"\n=== DATA FRESHNESS (budget {max_age}d) ===")
    print(f"  current={r['n_current']}  stale={r['n_stale']}  missing={r['n_missing']}  (of {r['n_symbols']})")
    if r["stale"]:
        print("  stale (oldest first):")
        for s in r["stale"][:15]:
            print(f"    {s['symbol']:8} last={s['last_bar']}  ({s['age_days']}d)")
    return 1 if (r["n_stale"] or r["n_missing"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Onboarding queue (M9.3c) — every listed graph name with an incomplete quant onboarding, and
(optionally, --check-market) whether each pending-price name is carried by the market-data vendor yet
(vendor-lag vs ready). Pure local read; --check-market adds Matriks symbolSearch calls. No mutation.

    PYTHONPATH=src python scripts/onboarding_queue.py [--check-market] [--limit N]
"""
from __future__ import annotations

import sys

from tmkg.graph.connection import connect
from tmkg.l2.store import L2Store
from tmkg.ingest.onboarding_queue import market_data_status, onboarding_queue


def main(argv: list[str]) -> int:
    check_market = "--check-market" in argv
    limit = 20
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    con = connect()
    store = L2Store()
    store.bootstrap_schema()
    q = onboarding_queue(con, store)
    print(f"\n=== ONBOARDING QUEUE — {len(q)} listed names with an incomplete quant row ===")
    print("  (NB: scoped to all is_listed graph nodes — includes non-equity instruments like warrants;")
    print("   the actionable equity targets are a subset. Equity-scope filtering is a follow-up.)")

    adapter = None
    if check_market:
        from tmkg.ingest.matriks import MatriksAdapter
        adapter = MatriksAdapter()

    for e in q[:limit]:
        line = f"  {(e['ticker'] or '?'):8} next={e['next_step']:15} ({len(e['pending'])} pending)"
        if check_market and e["ticker"] and "universe_prices" in e["pending"]:
            md = market_data_status(adapter, e["ticker"])
            line += f"  market_data={md['market_data']}"
        print(line)
    if len(q) > limit:
        print(f"  ... and {len(q) - limit} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

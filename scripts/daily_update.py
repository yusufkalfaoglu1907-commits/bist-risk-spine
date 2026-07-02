"""M9.2 — daily incremental update. Refreshes only STALE names, each over its incremental window
(efficient top-up, not a full re-pull). DRY-RUN by default (plan only, no network/mutation); pass
--execute to pull. Does NOT refit factors (separate regime-aware step).

    PYTHONPATH=src python scripts/daily_update.py [--execute] [--max-age N] [--limit N]
"""
from __future__ import annotations

import sys
from datetime import date

from tmkg.graph.connection import connect
from tmkg.l2.store import L2Store
from tmkg.ingest.incremental import run_daily_update


def main(argv: list[str]) -> int:
    execute = "--execute" in argv
    max_age = int(argv[argv.index("--max-age") + 1]) if "--max-age" in argv else 7
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None

    con = connect()
    store = L2Store()
    store.bootstrap_schema()
    adapter = None
    if execute:
        from tmkg.ingest.matriks import MatriksAdapter
        adapter = MatriksAdapter()

    r = run_daily_update(con, store, adapter, as_of=date.today(), max_age_days=max_age,
                         dry_run=not execute, limit=limit)
    f = r["freshness"]
    print(f"\n=== DAILY UPDATE ({r['mode']}) ===")
    print(f"  freshness: current={f['n_current']} stale={f['n_stale']} missing={f['n_missing']}")
    print(f"  to pull  : {r['n_to_pull']}")
    for item in r["plan"][:15]:
        print(f"    {item['symbol']:8} {item['start']}..{item['end']}  ({item['age_days']}d stale)")
    for e in r["executed"]:
        print(f"    [{'ok' if e['ok'] else 'FAIL'}] {e['symbol']} "
              + (f"{e.get('n_bars')} bars" if e["ok"] else e.get("error", "")))
    if not execute and r["n_to_pull"]:
        print("\n  (dry-run — re-run with --execute to pull)")

    # Honest exit code: a run that fails most names must NOT report success (so the
    # refresh_all chain / scheduler surfaces it). Non-zero when any pull failed.
    n_failed = r.get("n_failed", 0)
    if n_failed:
        print(f"\n  {n_failed}/{r['n_to_pull']} pulls FAILED "
              f"({r.get('n_ok', 0)} ok) — exiting non-zero")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

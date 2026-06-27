"""M9.1 — the keep-current heartbeat. Read-only: detects new listings, checks the three health
monitors, and computes the onboarding queue, then prints a consolidated status and exits nonzero if
anything needs attention. Safe to schedule. No mutation, no onboarding.

    PYTHONPATH=src python scripts/keep_current.py

Schedule it (examples):
    # cron — daily 07:30 (after the market-data ingest jobs)
    30 7 * * *  cd /path/to/Finance\\ KG && PYTHONPATH=src .venv/bin/python scripts/keep_current.py >> data/cache/keep_current.log 2>&1

    # macOS launchd — a StartCalendarInterval plist calling the same command.

A nonzero exit = attention needed (new listing / monitor failure / pending onboarding); wire it to an
alert. The data this reads is refreshed by the ingest jobs (KAP member cache, adapter smoke runs); the
scheduler should run those first, then this as the gate.
"""
from __future__ import annotations

from tmkg.graph.connection import connect
from tmkg.l2.store import L2Store
from tmkg.ingest.keep_current import run_keep_current


def main() -> int:
    con = connect()
    store = L2Store()
    store.bootstrap_schema()
    rep = run_keep_current(con, store, write_report=True)
    v = rep["verdict"]

    print("\n=== KEEP-CURRENT HEARTBEAT ===")
    print(f"  health monitors : idbridge={v['monitors']['idbridge']} "
          f"smoke_drift={v['monitors']['smoke_drift']} registry={v['monitors']['registry']}")
    print(f"  new listings    : {v['n_new_listings']}  {rep['new_listings']['tickers'] or ''}")
    print(f"  onboarding queue: {v['onboarding_queue_len']}")
    print(f"\n  STATUS: {'ATTENTION' if v['attention'] else 'OK — current'}")
    for r in v["reasons"]:
        print(f"    - {r}")
    print("\nReport: data/cache/keep_current_report.json")
    return 1 if v["attention"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

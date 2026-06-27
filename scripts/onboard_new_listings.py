"""Onboarding step 3b — bring detected new listings to a tradeable substrate row.

DRY-RUN by default: plans + verifies completion, no network, no mutation. Pass --execute to actually
run the (idempotent, fail-loud) onboarding chain — this DOES hit the network (KAP/GLEIF/Matriks) and
mutate the canonical graph + L2, so it is opt-in.

    PYTHONPATH=src python scripts/onboard_new_listings.py            # dry-run plan
    PYTHONPATH=src python scripts/onboard_new_listings.py --execute  # run the chain (mutates!)
"""
from __future__ import annotations

import sys
from datetime import date

from tmkg.graph.connection import connect
from tmkg.l2.store import L2Store
from tmkg.ingest.onboarding import run_onboarding


def main(argv: list[str]) -> int:
    execute = "--execute" in argv
    con = connect()
    store = L2Store()
    store.bootstrap_schema()
    rep = run_onboarding(con, store, as_of=date.today(), execute=execute, report_dir="data/cache")

    print(f"\n=== NEW-LISTING ONBOARDING ({rep['mode']}) ===")
    print(f"  new entities: {rep['n_entities']}")
    for e in rep["entities"]:
        head = f"{(e.get('ticker') or '?')} ({e.get('name') or ''})  [{e['kap_oid']}]"
        if e["complete"]:
            print(f"\n  ✓ {head} — already fully onboarded")
            continue
        print(f"\n  ▸ {head}")
        for s in e["remaining_steps"]:
            print(f"      - {s['step']:16} → {s['writes']}")
        if "execution" in e:
            for r in e["execution"]:
                print(f"      [{'ok' if r['ok'] else 'FAIL'}] {r['step']} (rc={r['returncode']})")
            print(f"      complete after run: {e.get('complete_after')}  pending: {e.get('pending_after')}")
    if not execute and any(not e["complete"] for e in rep["entities"]):
        print("\n  (dry-run — re-run with --execute to onboard; it mutates the graph + L2)")
    print("\nReport: data/cache/onboarding_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

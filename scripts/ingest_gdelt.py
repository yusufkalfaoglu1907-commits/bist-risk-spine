"""M6 — Turkey GKG event backfill into L2 ``events`` + ``event_targets`` (BUILD_PLAN.md M6).

The one network-touching M6 driver (§4 ingestion layer). Crawls the GDELT raw GKG feed
day-by-day over ``[start, end]``, lands the Turkey-filtered, typed events + their inferred-tier
``TARGETS`` prior seed, and writes a §4 audit report. Day-granular so it is **resumable**
(``--skip-existing`` skips days already landed) and **resilient** (a per-day source failure is
logged and the crawl continues; a burst of 15 consecutive failures aborts per §8). Writes are
PK-idempotent, so a re-run never duplicates.

    PYTHONPATH=src python scripts/ingest_gdelt.py 2025-01-01 2025-03-31 [--skip-existing]

NB GKG files are ~5 MB × 96/day, so a quarter is tens of GB / a few hours — run in the
background. The M6 exit gate (``scripts/run_m6_gate.py``) reads what this lands.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import tmkg.config  # noqa: F401  -- load_dotenv() before adapters read env
from tmkg.ingest.audit import write_run_report
from tmkg.ingest.gdelt import GdeltAdapter
from tmkg.ingest.pipeline import ingest_gdelt_events
from tmkg.l2.store import L2Store
from tmkg.pit.errors import SourceUnreachable

_BURST_ABORT = 15  # consecutive day-level source failures -> §8 hard stop


def _day_has_events(store: L2Store, d: date) -> bool:
    con = store.connect()
    try:
        return con.execute(
            "SELECT count(*) FROM events WHERE knowledge_date = ?", [d]
        ).fetchone()[0] > 0
    finally:
        con.close()


def main(start: date, end: date, *, skip_existing: bool, db_path: str | None) -> int:
    store = L2Store(db_path=db_path) if db_path else L2Store()
    store.bootstrap_schema()  # idempotent — adds events/event_targets if the store predates M6
    adapter = GdeltAdapter(timeout=90.0)

    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    tot = {"events": 0, "targets": 0, "untyped_skipped": 0,
           "days_done": 0, "days_skipped": 0, "days_failed": 0}
    consec_fail = 0
    print(f"GDELT Turkey GKG backfill {start}..{end}  ({len(days)} days, full 15-min cadence)")

    for i, d in enumerate(days, 1):
        if skip_existing and _day_has_events(store, d):
            tot["days_skipped"] += 1
            print(f"[{i}/{len(days)}] {d}  SKIP (already landed)")
            continue
        try:
            rep = ingest_gdelt_events(adapter, store, start=d, end=d)
            tot["events"] += rep["n_events"]
            tot["targets"] += rep["n_targets"]
            tot["untyped_skipped"] += rep["skipped"].get("untyped", 0)
            tot["days_done"] += 1
            consec_fail = 0
            print(f"[{i}/{len(days)}] {d}  +{rep['n_events']} events  "
                  f"(+{rep['n_targets']} targets)  [running: {tot['events']} events]")
        except SourceUnreachable as e:
            consec_fail += 1
            tot["days_failed"] += 1
            print(f"[{i}/{len(days)}] {d}  FAILED ({consec_fail} in a row): {e}", file=sys.stderr)
            if consec_fail >= _BURST_ABORT:
                print(f"§8 HARD STOP: {_BURST_ABORT} consecutive day failures — aborting.",
                      file=sys.stderr)
                tot["aborted_at"] = str(d)
                break

    report = {"source": "gdelt", "window": f"{start}..{end}", **tot}
    write_run_report("gdelt_backfill", report)
    print(f"\nDONE: {tot['events']} events / {tot['targets']} targets landed · "
          f"{tot['days_done']} days · {tot['days_skipped']} skipped · {tot['days_failed']} failed")
    return 1 if tot.get("aborted_at") else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=date.fromisoformat)
    ap.add_argument("end", type=date.fromisoformat)
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip days that already have events landed (resumable backfill)")
    ap.add_argument("--db-path", default=None, help="L2 DuckDB path (default: project L2)")
    a = ap.parse_args()
    raise SystemExit(main(a.start, a.end, skip_existing=a.skip_existing, db_path=a.db_path))

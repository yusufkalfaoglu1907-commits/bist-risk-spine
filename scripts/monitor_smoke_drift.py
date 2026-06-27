"""M8.3 — data-source drift monitor. Aggregates the per-adapter <source>_smoke_report.json outcomes
into one health surface and writes data/cache/smoke_drift_report.json (§4). Pure local read — it does
NOT re-run the network smoke checks (that is `make smoke`); it reports what those runs recorded.

    PYTHONPATH=src python scripts/monitor_smoke_drift.py [MAX_AGE_DAYS]
"""
from __future__ import annotations

import sys

from tmkg.monitor.smoke_drift import write_smoke_drift_report


def main(argv: list[str]) -> int:
    max_age = float(argv[1]) if len(argv) > 1 else 30.0
    report, path = write_smoke_drift_report(max_age_days=max_age)

    print("\n=== DATA-SOURCE DRIFT (recorded smoke outcomes) ===")
    for r in report["sources"]:
        age = f"{r['age_days']}d" if r.get("age_days") is not None else "-"
        extra = ""
        if r["status"] == "drift":
            extra = f"  DRIFT: {r['drift']}"
        print(f"  {r['source']:14} {r['status']:10} (age {age}){extra}")
    c = report["counts"]
    print(f"\n  counts: ok={c['ok']} drift={c['drift']} missing={c['missing']} "
          f"stale={c['stale']} unreadable={c['unreadable']}")
    if report["stale_sources"]:
        print(f"  stale (>{report['max_age_days']}d, warn only): {report['stale_sources']}")
    print(f"\n  RESULT: {'PASS' if report['passes'] else 'FAIL'}")
    for f in report["failures"]:
        print(f"    - {f}")
    print(f"\nReport: {path}")
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

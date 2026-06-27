"""M8.3 — id-bridge health monitor. Sweeps the whole ticker-bearing universe and writes
data/cache/idbridge_health_report.json (§4). Prints PASS/FAIL on coverage floors + collisions +
round-trips. Pure read over the L1 graph — no network, no mutation.

    PYTHONPATH=src python scripts/monitor_idbridge.py
"""
from __future__ import annotations

import sys

from tmkg.graph.connection import connect
from tmkg.monitor.idbridge_health import write_idbridge_health_report


def main() -> int:
    con = connect()
    report, path = write_idbridge_health_report(con)

    print("\n=== ID-BRIDGE HEALTH (the §5 single point of failure) ===")
    print(f"  ticker names : {report['n_companies_ticker']}")
    for leg, cov in report["coverage"].items():
        floor = report["coverage_floors"].get(leg)
        flag = "" if floor is None or cov >= floor else f"  ⚠ < floor {floor}"
        print(f"  coverage {leg:8}: {cov:.3f}{flag}")
    cc = report["collision_counts"]
    print(f"  collisions   : isin={cc['isin']} kap_oid={cc['kap_oid']} lei={cc['lei']}")
    rt = report["round_trip"]
    print(f"  round-trips  : ok={rt['ok']} broken={rt['broken_count']} ambiguous={rt['ambiguous_count']}")
    print(f"\n  RESULT: {'PASS' if report['passes'] else 'FAIL'}")
    for f in report["failures"]:
        print(f"    - {f}")
    print(f"\nReport: {path}")
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

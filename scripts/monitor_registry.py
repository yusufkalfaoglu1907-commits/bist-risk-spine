"""M8.3 — signal_registry hygiene monitor. Sweeps the L2 verdict ledger for incoherent rows
(promoted-but-fails-gate, missing n_trials haircut, out-of-range DSR/PBO) and writes
data/cache/registry_hygiene_report.json (§4). Reads via PITAccess (§5); no network, no mutation.

    PYTHONPATH=src python scripts/monitor_registry.py [AS_OF]
"""
from __future__ import annotations

import sys
from datetime import date

from tmkg.l2.store import L2Store
from tmkg.monitor.registry_hygiene import write_registry_hygiene_report


def main(argv: list[str]) -> int:
    as_of = date.fromisoformat(argv[1]) if len(argv) > 1 else date.today()
    store = L2Store()
    store.bootstrap_schema()
    report, path = write_registry_hygiene_report(store, as_of=as_of)

    print(f"\n=== SIGNAL_REGISTRY HYGIENE @ {as_of} ===")
    print(f"  rows {report['n_rows']} · signals {report['n_signals']} · promoted {report['promoted_count']}")
    for v in report["latest_verdicts"]:
        tag = "PROMOTED" if v["promoted"] else "NO-GO"
        ver = f"  ({v['n_versions']} versions)" if v["n_versions"] > 1 else ""
        print(f"    {v['signal_id']:22} {tag:9} @ {v['knowledge_date']}{ver}")
    if report["multi_version_signals"]:
        print(f"  multi-version: {report['multi_version_signals']}")
    print(f"\n  RESULT: {'PASS' if report['passes'] else 'FAIL'}")
    for f in report["failures"]:
        print(f"    - {f}")
    print(f"\nReport: {path}")
    return 0 if report["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

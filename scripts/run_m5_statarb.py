"""M5 — residual stat-arb through the M4 judge (BUILD_PLAN.md M5).

The first *real* signal. Reads the landed neutralized residuals + total returns (PIT-honest, no
network), builds the peer-relative residual-reversion candidate from the M3 surviving edges,
runs the honest grid through the purged walk-forward / three-book / DSR-PBO promotion gate, and
lands the artifacts:

  - the filtered residual-corr snapshot -> L2 ``residual_corr`` (M3 survivors; never the dense matrix);
  - the verdict (promote OR reject) -> L2 ``signal_registry`` (a rejection is recorded as durably
    as a promotion — "we tried it and it failed");
  - a JSON audit report -> data/cache/m5_statarb_report.json.

This is the M5 exit gate, a project-level result (§8): the script PRINTS the verdict; it does
NOT auto-advance to M6.

    PYTHONPATH=src python scripts/run_m5_statarb.py [AS_OF] [TOP_BY_COVERAGE]
"""
from __future__ import annotations

import sys
from datetime import date

import tmkg.config  # noqa: F401  -- load_dotenv() before adapters read env
import duckdb

from tmkg.graph.connection import connect as graph_connect
from tmkg.ingest.universe import graph_sector_resolver
from tmkg.l2.store import L2Store
from tmkg.signals.run_statarb import run_m5_statarb


def _residual_symbols(store: L2Store, as_of: date) -> list[str]:
    con = store.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM residuals WHERE knowledge_date <= ? ORDER BY symbol",
            [as_of]).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def main(as_of: date, top_by_coverage: int) -> int:
    store = L2Store()
    store.bootstrap_schema()

    syms = _residual_symbols(store, as_of)
    if not syms:
        print("NO residuals landed — run scripts/run_m3_gate.py first.")
        return 1

    resolve = graph_sector_resolver(graph_connect())
    sectors = {s: resolve(s) for s in syms}
    sectors = {k: v for k, v in sectors.items() if v is not None}
    print(f"sector map: {len(sectors)} names placed into {len(set(sectors.values()))} sectors")

    print(f"M5 residual stat-arb (top_by_coverage={top_by_coverage}) ...")
    rep = run_m5_statarb(
        store, as_of=as_of, sectors=sectors, top_by_coverage=top_by_coverage,
        panel_min_obs=120, write_l2=True, report_dir="data/cache")

    g = rep["exit_gate"]
    v = rep["verdict"]
    b = rep["books"]
    inp = rep["inputs"]
    decision = "GO (PROMOTED)" if g["promoted"] else "NO-GO (not real in venue-feasible)"
    print("\n" + "=" * 68)
    print(f"  M5 RESIDUAL STAT-ARB  [exit gate]  ->  {decision}")
    print("=" * 68)
    print(f"  universe: {inp['n_names']} liquid names, {inp['n_oos_test_dates']} OOS test dates, "
          f"n_trials={inp['n_trials_grid']}")
    print(f"  net Sharpe  research / venue / stress = "
          f"{b['research']['net_sharpe']:.3f} / {b['venue_feasible']['net_sharpe']:.3f} / "
          f"{b['stress']['net_sharpe']:.3f}")
    print(f"  candidate net={v['candidate_net_sharpe']:.3f}  vs best baseline "
          f"{v['best_baseline']}={v['best_baseline_net_sharpe']:.3f}")
    print(f"  DSR={v['dsr']['dsr']:.3f} (passes={v['dsr']['passes']})  PBO={v['pbo']['pbo']:.3f}")
    print(f"  exit-gate checks: {g}")
    print(f"  short_eligible available: {inp['short_eligible_available']} "
          f"(empty -> stress book is the binding short test)")
    print(f"  report -> data/cache/m5_statarb_report.json")
    print("=" * 68)
    if not g["promoted"]:
        print("  Verdict logged to signal_registry. Per the M5 exit gate, a signal that survives")
        print("  only in frictionless research is NOT real: log and move on (next pillar = M6).")
    return 0


if __name__ == "__main__":
    as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    raise SystemExit(main(as_of, top))

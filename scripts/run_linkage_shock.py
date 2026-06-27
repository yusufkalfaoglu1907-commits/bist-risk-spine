"""M8.2 — propagate an idiosyncratic name shock through the ownership/control graph and write
data/cache/m8_linkage_shock_report.json (§4). RISK tool: structural look-through exposure (who is
mechanically exposed via stakes / control), NOT a linked-firm co-move prediction (that was NO-GO,
ADR-0006). Reads the L1 Kuzu graph; no network, no registry write.

    PYTHONPATH=src python scripts/run_linkage_shock.py TICKER:SHOCK [TICKER:SHOCK ...]
    # e.g. PYTHONPATH=src python scripts/run_linkage_shock.py ARCLK:-0.20 TTo-FROTO style
    #   ARCLK:-0.20  =  a 20% idiosyncratic drop in ARCLK
"""
from __future__ import annotations

import sys

from tmkg.graph.connection import connect
from tmkg.risk.run_linkage import run_linkage_shock


def _parse(args: list[str]) -> dict[str, float]:
    shocks: dict[str, float] = {}
    for a in args:
        if ":" not in a:
            raise SystemExit(f"bad shock arg {a!r}; use TICKER:SHOCK e.g. ARCLK:-0.20")
        tk, val = a.rsplit(":", 1)
        shocks[tk.upper()] = float(val)
    return shocks


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    shocks = _parse(argv[1:])
    con = connect()
    rep = run_linkage_shock(con, shocks=shocks, report_dir="data/cache")

    own = rep["ownership"]
    print("\n=== M8.2 LINKAGE SHOCK PROPAGATION (structural look-through, not alpha) ===")
    print(f"  shocks         : {rep['shocks']}")
    print(f"  ownership edges: {own['n_edges']}   holders exposed: {own['n_holders_exposed']}")
    if own["unmapped_origins"]:
        print(f"  unmapped origin: {own['unmapped_origins']} (in no ownership edge — surfaced)")
    print("  worst-exposed holders (look-through):")
    for h, v in own["worst_exposed"].items():
        print(f"      {h:10} {v:+.4f}")
    for origin, b in rep["control_blast_radius"].items():
        print(f"  CONTROLS blast-radius of {origin}: {b['n_in_blast_radius']} names "
              f"(up={list(b['controllers'])[:6]} down={list(b['controlled'])[:6]})")
    print(f"\n  caveat: {own['caveat']}")
    print("\nReport: data/cache/m8_linkage_shock_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

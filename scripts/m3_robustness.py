"""M3 gate robustness sweep — sector granularity × rolling-window length.

The wide-universe gate sat right on the absolute-persistence bar (median Jaccard 0.096 vs 0.10)
while chance-adjusted lift cleared by 6.5×. This sweep brackets that borderline read WITHOUT
touching any threshold (§8): does the verdict's single failing check move under defensible
re-parameterizations, or is it stable?

Two axes, both over the SAME landed residuals (pure read+compute, no refit, no network):
  · sector granularity — LEAF (level-2, 57 blocks, the gate default) vs PARENT (level-1, 16
    coarser blocks; consolidates the long tail of ≤4-name leaf sectors that contribute few/no
    within-sector pairs);
  · rolling-window length — 90 / 120 / 150 trading days (non-overlapping; fewer→more pairs).

Writes data/cache/m3_robustness_report.json. Prints lift + Jaccard + decision per cell.

    PYTHONPATH=src python scripts/m3_robustness.py [AS_OF]
"""
from __future__ import annotations

import json
import sys
from datetime import date

import tmkg.config  # noqa: F401
from tmkg import config
from tmkg.graph.connection import connect as graph_connect
from tmkg.ingest.universe import graph_sector_resolver
from tmkg.l2.store import L2Store
from tmkg.signals.gate import m3_residual_survival_gate

WINDOWS = [90, 120, 150]
MIN_OBS = 60


def _leaf_to_parent(con) -> dict[str, str]:
    """leaf sector NAME -> level-1 ancestor NAME (level-1 names map to themselves)."""
    rows, by_code = [], {}
    r = con.execute("MATCH (s:Sector) RETURN s.code, s.level, s.parent_code, s.name")
    while r.has_next():
        rows.append(r.get_next())
    by_code = {c: (lvl, parent, name) for c, lvl, parent, name in rows}
    out = {}
    for code, (lvl, parent, name) in by_code.items():
        if lvl == 1 or parent is None:
            out[name] = name
        else:
            out[name] = by_code.get(parent, (None, None, name))[2]
    return out


def main(as_of: date) -> int:
    store = L2Store()
    con = graph_connect()
    leaf_resolve = graph_sector_resolver(con)
    leaf_to_parent = _leaf_to_parent(con)

    syms = [x[0] for x in store.connect().execute(
        "SELECT DISTINCT symbol FROM residuals").fetchall()]
    leaf_map = {s: leaf_resolve(s) for s in syms}
    leaf_map = {k: v for k, v in leaf_map.items() if v}
    parent_map = {s: leaf_to_parent.get(v, v) for s, v in leaf_map.items()}

    granularities = {
        "leaf_L2": (leaf_map, len(set(leaf_map.values()))),
        "parent_L1": (parent_map, len(set(parent_map.values()))),
    }

    cells = []
    print(f"{'granularity':12} {'nblocks':>7} {'window':>6} {'pairs':>5} "
          f"{'med_lift':>9} {'med_jacc':>9} {'med_edges':>9}  decision")
    for gname, (smap, nblk) in granularities.items():
        for w in WINDOWS:
            rep = m3_residual_survival_gate(
                store, as_of=as_of, sectors=smap,
                window=w, min_obs=MIN_OBS, panel_min_obs=MIN_OBS,
            )
            s, d = rep["summary"], rep["decision"]
            cell = {
                "granularity": gname, "n_blocks": nblk, "window": w,
                "n_window_pairs": s["n_window_pairs"],
                "median_lift": s["median_lift"], "median_jaccard": s["median_jaccard"],
                "median_n_edges": s["median_n_edges"],
                "decision": d["decision"], "failed_checks": d["failed_checks"],
            }
            cells.append(cell)
            print(f"{gname:12} {nblk:>7} {w:>6} {s['n_window_pairs']:>5} "
                  f"{s['median_lift']:>9.2f} {s['median_jaccard']:>9.4f} "
                  f"{s['median_n_edges']:>9.1f}  {d['decision']} {d['failed_checks']}")

    out = config.REPO_ROOT / "data" / "cache" / "m3_robustness_report.json"
    out.write_text(json.dumps(
        {"as_of": str(as_of), "min_obs": MIN_OBS, "windows": WINDOWS, "cells": cells},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    raise SystemExit(main(as_of))

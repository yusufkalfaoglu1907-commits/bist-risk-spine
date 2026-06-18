#!/usr/bin/env python3
"""Phase-1 build: create schema, load identity spine + ownership from fixtures,
then run the Koç-group exposure exit query.

Usage:
    PYTHONPATH=src python scripts/build_phase1.py
    PYTHONPATH=src python scripts/build_phase1.py --db /tmp/tmkg.kuzu
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders import identity, ownership
from tmkg.analytics.exposure import group_exposure, total_group_weight


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="Kuzu db path (default from config)")
    ap.add_argument("--fresh", action="store_true", help="Delete existing db first")
    args = ap.parse_args()

    if args.fresh and args.db and Path(args.db).exists():
        shutil.rmtree(args.db)

    conn = connect(args.db)
    apply_schema(conn)

    n_co = identity.load_companies(conn)
    n_pe = identity.load_people(conn)
    n_se = identity.load_securities(conn)
    n_sc = identity.load_sectors(conn)
    n_pf = identity.load_portfolio(conn)
    edge_counts = ownership.load_all(conn)

    print("=== Loaded ===")
    print(f"  Companies:  {n_co}")
    print(f"  People:     {n_pe}")
    print(f"  Securities: {n_se}")
    print(f"  Sectors:    {n_sc}")
    print(f"  Portfolio holdings: {n_pf}")
    for k, v in edge_counts.items():
        print(f"  {k}: {v}")

    print("\n=== Exit test: exposure to Koç group (root=co-kchol) ===")
    rows = group_exposure(conn, "pf-main", "co-kchol")
    for r in rows:
        flag = "KOÇ" if r["in_group"] else "   "
        hops = "" if r["control_hops"] is None else f"  ({r['control_hops']} hop)"
        print(f"  [{flag}] {r['ticker']:<6} w={r['weight']:.2f}  {r['name']}{hops}")
    print(f"\n  >>> Aggregated Koç-group portfolio weight: "
          f"{total_group_weight(rows) * 100:.1f}%")


if __name__ == "__main__":
    main()

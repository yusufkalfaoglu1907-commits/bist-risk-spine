#!/usr/bin/env python3
"""Discover NEW debt instruments from KAP issuance bulletins — no MKK export.

Walks debt issuers' KAP feeds (resumable, time-budgeted), parses each
"Borçlanma Araçlarının / Kira Sertifikalarının İşlem Görmeye Başlaması" bulletin
into full instrument records (ISIN, nominal, maturity, issuer ticker — see
kap_issuance_adapter), accumulates them into data/reference/kap_issuance.json,
and (unless --no-load) materialises them onto the graph via
backfill_from_issuances (ticker-matched, idempotent, creates missing Securities).

This is what makes the weekly refresh self-sustaining: new issuance enters the
graph straight from KAP, with amount + maturity, with no manual MKK step.

    PYTHONPATH=src python scripts/discover_kap_issuances.py --db ./data/tmkg.kuzu \
        --issuers KOCFN,ARCLK --years 2025,2026 --budget 60
    PYTHONPATH=src python scripts/discover_kap_issuances.py --db ./data/tmkg.kuzu \
        --from-graph --budget 120
"""
from __future__ import annotations

import argparse
import time

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_adapter import KapAdapter
from tmkg.adapters.kap_issuance_adapter import (
    extract_issuances, load_issuance_reference, write_issuance_reference,
    DEFAULT_ISSUANCE_REFERENCE_PATH,
)
from tmkg.loaders.kap_issuance_backfill import backfill_from_issuances


def harvest(issuers, from_graph, years, budget, indices=None) -> list[dict]:
    t0 = time.time()
    have = {r["isin"]: r for r in load_issuance_reference()}
    with KapAdapter() as adapter:
        members = adapter.fetch_members()
        by_ticker = {m.primary_ticker: m for m in members if m.primary_ticker}
        idxs = list(indices or [])
        if not idxs:
            if from_graph:
                targets = [m for m in members if m.mkk_oid]
            else:
                targets = [by_ticker[t] for t in issuers if t in by_ticker and by_ticker[t].mkk_oid]
            for m in targets:
                if time.time() - t0 > budget:
                    break
                for y in years:
                    try:
                        ds = adapter.fetch_disclosures(m.mkk_oid, f"{y}-01-01", f"{y}-12-31")
                    except Exception:
                        continue
                    for d in ds:
                        i = getattr(d, "index", None) or getattr(d, "disclosureIndex", None)
                        if i:
                            idxs.append(int(i))
        seen = set()
        for idx in idxs:
            if time.time() - t0 > budget:
                break
            if idx in seen:
                continue
            seen.add(idx)
            try:
                html = adapter.fetch_detail_html(idx)
            except Exception:
                continue
            for rec in extract_issuances(html, source=f"KAP:{idx}")["records"]:
                have[rec["isin"]] = rec
    write_issuance_reference(list(have.values()))
    return list(have.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--issuers", default="")
    ap.add_argument("--from-graph", action="store_true")
    ap.add_argument("--indices", default="")
    ap.add_argument("--years", default="2025,2026")
    ap.add_argument("--budget", type=float, default=90.0)
    ap.add_argument("--no-load", action="store_true", help="harvest only, don't write the graph")
    ap.add_argument("--create-missing-issuers", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    issuers = [t.strip().upper() for t in args.issuers.split(",") if t.strip()]
    indices = [int(x) for x in args.indices.split(",") if x.strip()]
    years = [int(y) for y in args.years.split(",") if y.strip()]

    records = harvest(issuers, args.from_graph, years, args.budget, indices or None)
    print(f"harvested {len(records)} issuance records -> {DEFAULT_ISSUANCE_REFERENCE_PATH}")

    if not args.no_load:
        conn = connect(args.db)
        apply_schema(conn)
        stats = backfill_from_issuances(
            conn, records, create_missing_issuers=args.create_missing_issuers,
            report_path=args.report)
        print("load:", {k: stats[k] for k in
                        ("records_in", "written", "new_instruments",
                         "unmatched_issuer", "out_of_scope_class")})


if __name__ == "__main__":
    main()

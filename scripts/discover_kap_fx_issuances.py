#!/usr/bin/env python3
"""Price XS eurobonds from KAP issue certificates — FX issue size + currency.

Eurobonds (XS…) never appear in the domestic TL listing bulletin the
`discover_kap_issuances` path walks. Their issue size and currency live in the
SPK issue-certificate disclosures — subject "İhraç Belgesi" / "Tertip İhraç
Belgesi" — as a labelled `DÖVİZ: … NOMİNAL TUTAR: …` run carrying the XS ISIN
(see kap_issuance_adapter.extract_fx_issuances). This script walks the XS
issuers' feeds (resumable, time-budgeted), extracts those records into
data/reference/kap_fx_issuance.json, and (unless --no-load) prices the matching
XS Securities ISIN-exact via backfill_fx_issuances.

Hard rule (audit-fix-plan 3.1/3.2): the figure is the ISSUE size, an UPPER BOUND
per currency — it lands with basis 'fx-issue-size-upper-bound', never folded into
a confident or single-currency total, never USDTRY-converted.

    # walk a couple of issuers, harvest only (no graph write)
    PYTHONPATH=src python scripts/discover_kap_fx_issuances.py \
        --issuers AKBNK,GARAN --years 2023,2024,2025 --budget 90 --no-load

    # walk every XS issuer in the graph and price a (tmp-copy) graph
    PYTHONPATH=src python scripts/discover_kap_fx_issuances.py \
        --db ./data/tmkg.kuzu --from-graph --budget 300
"""
from __future__ import annotations

import argparse
import re
import time

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_adapter import KapAdapter
from tmkg.adapters.kap_issuance_adapter import (
    extract_fx_issuances, load_fx_issuance_reference, write_fx_issuance_reference,
    DEFAULT_FX_ISSUANCE_REFERENCE_PATH,
)
from tmkg.loaders.kap_issuance_backfill import backfill_fx_issuances

# Issue-certificate disclosure subjects (Turkish). Both the umbrella certificate
# and the per-tranche one carry the structured DÖVİZ/NOMİNAL TUTAR field run.
_CERT_SUBJECT = re.compile(r"İhraç Belgesi", re.I)


def _xs_issuer_oids(adapter, conn) -> list[str]:
    """mkk_oids of companies that issue XS paper in the graph, matched to KAP
    members by ticker (None-ticker external stubs are skipped — no feed to walk)."""
    res = conn.execute(
        "MATCH (c:Company)-[i:ISSUES]->(:Security) WHERE i.instrument_class='XS' "
        "AND c.ticker IS NOT NULL RETURN DISTINCT c.ticker")
    tickers = set()
    while res.has_next():
        tickers.add(res.get_next()[0])
    by_ticker = {m.primary_ticker: m for m in adapter.fetch_members() if m.primary_ticker}
    return [by_ticker[t].mkk_oid for t in tickers
            if t in by_ticker and by_ticker[t].mkk_oid]


def harvest(adapter, oids, years, budget) -> list[dict]:
    t0 = time.time()
    have = {r["isin"]: r for r in load_fx_issuance_reference()}
    # 1) collect issue-certificate disclosure indices across the issuers/years
    indices: list[int] = []
    for oid in oids:
        if time.time() - t0 > budget:
            break
        for y in years:
            try:
                ds = adapter.fetch_disclosures(oid, f"{y}-01-01", f"{y}-12-31")
            except Exception:
                continue
            for d in ds:
                if _CERT_SUBJECT.search(d.subject or ""):
                    indices.append(int(d.index))
    # 2) fetch + extract each certificate (dedup by index)
    seen: set[int] = set()
    for idx in indices:
        if time.time() - t0 > budget:
            print(f"[budget] stopping after {idx}")
            break
        if idx in seen:
            continue
        seen.add(idx)
        try:
            html = adapter.fetch_detail_html(idx)
        except Exception:
            continue
        for rec in extract_fx_issuances(html, source=f"KAP:{idx}")["records"]:
            have[rec["isin"]] = rec
    write_fx_issuance_reference(list(have.values()))
    print(f"scanned {len(seen)} certificates in {time.time()-t0:.0f}s | "
          f"reference now {len(have)} XS ISINs -> {DEFAULT_FX_ISSUANCE_REFERENCE_PATH}")
    return list(have.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--issuers", default="", help="comma-separated tickers")
    ap.add_argument("--from-graph", action="store_true",
                    help="walk every XS issuer in the graph (needs --db)")
    ap.add_argument("--years", default="2023,2024,2025,2026")
    ap.add_argument("--budget", type=float, default=120.0)
    ap.add_argument("--no-load", action="store_true", help="harvest only, don't write the graph")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    conn = connect(args.db) if (args.db or args.from_graph) else None
    if conn is not None:
        apply_schema(conn)

    with KapAdapter() as adapter:
        if args.from_graph:
            if conn is None:
                ap.error("--from-graph requires --db")
            oids = _xs_issuer_oids(adapter, conn)
        else:
            by_ticker = {m.primary_ticker: m for m in adapter.fetch_members() if m.primary_ticker}
            tickers = [t.strip().upper() for t in args.issuers.split(",") if t.strip()]
            oids = [by_ticker[t].mkk_oid for t in tickers
                    if t in by_ticker and by_ticker[t].mkk_oid]
        records = harvest(adapter, oids, years, args.budget)

    if not args.no_load:
        if conn is None:
            ap.error("loading requires --db (or pass --no-load)")
        stats = backfill_fx_issuances(conn, records, report_path=args.report)
        print("load:", {k: stats[k] for k in ("records_in", "priced", "unmatched_isin")})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Discover CONTROLS / ownership edges from KAP general-information forms.

Walks listed companies' KAP feeds (resumable, time-budgeted), finds each
company's most recent "Şirket Genel Bilgi Formu", parses its
"Bağlı Ortaklıklar, Finansal Duran Varlıklar ile Finansal Yatırımlar" table into
parent->child relations (see kap_subsidiary_adapter), accumulates them into
data/reference/kap_subsidiary.json, and (unless --no-load) writes the resulting
CONTROLS / SUBSIDIARY_OF / HOLDS_STAKE edges via backfill_subsidiaries.

This closes the contagion-graph gap GLEIF L2 left open: each company's OWN
consolidation declaration links it to the entities it controls.

    PYTHONPATH=src python scripts/discover_kap_subsidiaries.py --db ./data/tmkg.kuzu \
        --issuers KCHOL,SAHOL --years 2025,2026 --budget 60
    PYTHONPATH=src python scripts/discover_kap_subsidiaries.py --db ./data/tmkg.kuzu \
        --from-graph --budget 300
"""
from __future__ import annotations

import argparse
import time

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_adapter import KapAdapter
from tmkg.adapters.kap_subsidiary_adapter import (
    extract_subsidiaries, load_subsidiary_reference, write_subsidiary_reference,
    DEFAULT_SUBSIDIARY_REFERENCE_PATH,
)
from tmkg.loaders.kap_subsidiary_backfill import backfill_subsidiaries

_FORM_SUBJECT = "Şirket Genel Bilgi Formu"
# The general-info form is sectioned: any single disclosure may update only ONE
# section (board, capital, subsidiaries…), so the LATEST form often lacks the
# related-party table. Scan recent forms newest-first until one carries it, up to
# this many attempts per company (bounds the network cost).
_MAX_FORMS_PER_COMPANY = 5


def harvest(issuers, from_graph, years, budget) -> list[dict]:
    """Return the accumulated flat relations list (merged with prior reference)."""
    t0 = time.time()
    have = {(r.get("parent_ticker"), r.get("child_name")): r
            for r in load_subsidiary_reference()}
    with KapAdapter() as adapter:
        members = adapter.fetch_members()
        by_ticker = {m.primary_ticker: m for m in members if m.primary_ticker}
        if from_graph:
            targets = [m for m in members if m.mkk_oid and m.primary_ticker]
        else:
            targets = [by_ticker[t] for t in issuers
                       if t in by_ticker and by_ticker[t].mkk_oid]
        for m in targets:
            if time.time() - t0 > budget:
                break
            # gather this company's general-info forms, newest first
            forms = []  # (publish_datetime_str, index)
            for y in years:
                try:
                    ds = adapter.fetch_disclosures(m.mkk_oid, f"{y}-01-01", f"{y}-12-31")
                except Exception:
                    continue
                for d in ds:
                    if (d.subject or "").strip() != _FORM_SUBJECT:
                        continue
                    idx = getattr(d, "index", None)
                    if idx:
                        forms.append((str(getattr(d, "publish_datetime", "")), int(idx)))
            forms.sort(reverse=True)
            # fetch newest-first until one carries the related-party section
            for pub, idx in forms[:_MAX_FORMS_PER_COMPANY]:
                if time.time() - t0 > budget:
                    break
                try:
                    html = adapter.fetch_detail_html(idx)
                except Exception:
                    continue
                recs = extract_subsidiaries(html, source=f"KAP:{idx}")["records"]
                if not recs:
                    continue
                as_of = pub[:10] if pub else None
                for rec in recs:
                    rec = {**rec, "parent_ticker": m.primary_ticker,
                           "parent_name": m.name, "as_of": as_of}
                    have[(m.primary_ticker, rec["child_name"])] = rec
                break  # found the section; stop scanning older forms
    write_subsidiary_reference(list(have.values()))
    return list(have.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--issuers", default="")
    ap.add_argument("--from-graph", action="store_true")
    ap.add_argument("--years", default="2025,2026")
    ap.add_argument("--budget", type=float, default=120.0)
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--no-load", action="store_true", help="harvest only")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    issuers = [t.strip().upper() for t in args.issuers.split(",") if t.strip()]
    years = [int(y) for y in args.years.split(",") if y.strip()]

    relations = harvest(issuers, args.from_graph, years, args.budget)
    print(f"harvested {len(relations)} relations -> {DEFAULT_SUBSIDIARY_REFERENCE_PATH}")

    if not args.no_load:
        conn = connect(args.db)
        apply_schema(conn)
        stats = backfill_subsidiaries(conn, relations, threshold=args.threshold,
                                      report_path=args.report)
        print("load:", {k: stats[k] for k in
                        ("relations_in", "matched", "controls_new",
                         "controls_corroborated", "holds_stake_new",
                         "unmatched_child", "unmatched_parent", "self_links_skipped")})


if __name__ == "__main__":
    main()

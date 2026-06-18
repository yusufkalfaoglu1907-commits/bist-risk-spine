#!/usr/bin/env python3
"""GLEIF Level-1 LEI back-fill for the Company nodes seeded from KAP.

Attaches the canonical join key (LEI) + legal_form + jurisdiction +
registration_authority to listed companies by matching on name. Writes only
confident matches to the graph; logs every attempt to an audit report.

Examples:
    # LEI + ISIN back-fill for the first 25 listed companies (a quick proof)
    PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --limit 25

    # full LEI + ISIN back-fill for every listed company still missing them
    PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu

    # only the ISIN stage (LEIs already present)
    PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --stage isin

    # close the residual gap from the authoritative BİST/MKK ticker->ISIN map
    # (data/reference/bist_isin.json) — fills what GLEIF refused to guess
    PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu --stage bist

    # re-score ALL listed companies (not just missing) at a stricter threshold
    PYTHONPATH=src python scripts/backfill_gleif.py --db ./data/tmkg.kuzu \
        --stage lei --all --threshold 0.7

Requires network access to api.gleif.org (public, CC0, no key) for the lei/isin
stages. The bist stage is fully offline — it reads the committed reference file.
"""
from __future__ import annotations

import argparse

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.schema.integrity import check_no_controls_cycles, ControlsCycleError
from tmkg.adapters.gleif_adapter import GleifAdapter
from tmkg.adapters.bist_isin_adapter import BistIsinAdapter
from tmkg.loaders.gleif_backfill import backfill_leis, backfill_isins
from tmkg.loaders.gleif_l2_backfill import backfill_l2_parents
from tmkg.loaders.bist_isin_backfill import (
    backfill_isins_from_bist, classify_listing_status,
)
from tmkg.adapters.mkk_debt_adapter import MkkDebtReference
from tmkg.loaders.debt_backfill import backfill_debt
from tmkg.adapters.kap_nominal_adapter import KapNominalReference
from tmkg.loaders.nominal_backfill import backfill_nominals
from tmkg.adapters.kap_issuance_adapter import load_issuance_reference
from tmkg.loaders.kap_issuance_backfill import backfill_from_issuances
from tmkg.adapters.kap_subsidiary_adapter import load_subsidiary_reference
from tmkg.loaders.kap_subsidiary_backfill import backfill_subsidiaries
from tmkg.loaders.spv_parent_backfill import (
    backfill_spv_parents, demote_jv_suspect_edges,
)
from tmkg.loaders.external_stub_backfill import backfill_external_stubs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--stage",
                    choices=("lei", "isin", "bist", "classify", "debt", "l2",
                             "nominal", "issuance", "subsidiary", "spv", "stubs",
                             "both", "all"),
                    default="both",
                    help="which back-fill to run. 'both'=lei+isin (GLEIF); "
                         "'bist'=authoritative ticker->ISIN gap-fill; "
                         "'classify'=tag Company.listing_status; "
                         "'debt'=attach MKK debt instruments to issuers; "
                         "'l2'=GLEIF Level-2 CONTROLS/SUBSIDIARY_OF parent edges; "
                         "'nominal'=attach KAP issued-nominal amounts to Securities by ISIN; "
                         "'all'=lei+isin+bist+classify+debt+l2+nominal. isin/l2 need LEIs first; "
                         "nominal needs debt Securities first.")
    ap.add_argument("--limit", type=int, default=None, help="cap number of companies")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="min coverage score to write an LEI (default 0.6)")
    ap.add_argument("--all", action="store_true",
                    help="re-process all companies, not just those missing the field")
    ap.add_argument("--include-unlisted", action="store_true")
    ap.add_argument("--report", default=None, help="path for the LEI audit report JSON")
    ap.add_argument("--isin-report", default=None, help="path for the ISIN audit report JSON")
    ap.add_argument("--bist-report", default=None, help="path for the BİST ISIN audit report JSON")
    ap.add_argument("--reference", default=None, help="path to the BİST ticker->ISIN reference JSON")
    ap.add_argument("--debt-reference", default=None, help="path to the MKK debt reference JSON")
    ap.add_argument("--debt-report", default=None, help="path for the debt audit report JSON")
    ap.add_argument("--create-missing-issuers", action="store_true",
                    help="debt stage: create issuer nodes for unmatched (unlisted) debt issuers "
                         "instead of logging+skipping them")
    ap.add_argument("--l2-report", default=None, help="path for the Level-2 parent audit report JSON")
    ap.add_argument("--create-missing-parents", action="store_true",
                    help="l2 stage: materialise external (out-of-universe) parent Company nodes "
                         "instead of logging+skipping them")
    ap.add_argument("--nominal-reference", default=None, help="path to the KAP nominal reference JSON")
    ap.add_argument("--nominal-report", default=None, help="path for the nominal audit report JSON")
    args = ap.parse_args()

    conn = connect(args.db)
    apply_schema(conn)

    needs_gleif = args.stage in ("lei", "isin", "l2", "both", "all")
    if needs_gleif:
        with GleifAdapter() as adapter:
            if args.stage in ("lei", "both", "all"):
                stats = backfill_leis(
                    conn, adapter,
                    threshold=args.threshold, limit=args.limit,
                    only_missing=not args.all, listed_only=not args.include_unlisted,
                    report_path=args.report,
                )
                print("GLEIF LEI back-fill:")
                for k in ("targets", "leis_written", "below_threshold",
                          "no_candidates", "http_errors"):
                    print(f"  {k:16} {stats[k]}")
                print(f"  report           {stats['report']}")

            if args.stage in ("isin", "both", "all"):
                istats = backfill_isins(
                    conn, adapter, limit=args.limit, only_missing=not args.all,
                    report_path=args.isin_report,
                )
                print("GLEIF ISIN back-fill:")
                for k in ("targets", "isins_written", "needs_review", "http_errors"):
                    print(f"  {k:16} {istats[k]}")
                print(f"  report           {istats['report']}")

            if args.stage in ("l2", "all"):
                l2 = backfill_l2_parents(
                    conn, adapter, limit=args.limit, only_missing=not args.all,
                    create_missing_parents=args.create_missing_parents,
                    report_path=args.l2_report,
                )
                print("GLEIF Level-2 parent edges (CONTROLS / SUBSIDIARY_OF):")
                for k in ("targets", "direct_in_universe", "direct_external",
                          "ultimate_in_universe", "ultimate_external",
                          "no_parent", "external_parents_created",
                          "external_reconciled", "edges_written", "http_errors"):
                    print(f"  {k:24} {l2[k]}")
                print(f"  report                   {l2['report']}")

    if args.stage in ("bist", "all"):
        bist = BistIsinAdapter(reference_path=args.reference)
        bist.load()
        bstats = backfill_isins_from_bist(
            conn, bist, limit=args.limit, only_missing=not args.all,
            listed_only=not args.include_unlisted, report_path=args.bist_report,
        )
        print("BİST/MKK ISIN back-fill (authoritative ticker->ISIN):")
        print(f"  reference        {len(bist)} tickers · {bist.source!r}")
        for k in ("targets", "isins_written", "disambiguated", "conflicts",
                  "not_in_reference", "invalid_or_nonequity"):
            print(f"  {k:18} {bstats[k]}")
        print(f"  report           {bstats['report']}")

    if args.stage in ("classify", "all"):
        bist = BistIsinAdapter(reference_path=args.reference)
        cstats = classify_listing_status(conn, bist)
        print("Company listing_status classification:")
        for k in ("total", "EQUITY_TRADED", "NON_EQUITY_ISSUER"):
            print(f"  {k:18} {cstats[k]}")

    if args.stage in ("debt", "all"):
        debt_ref = MkkDebtReference(reference_path=args.debt_reference)
        debt_ref.load()
        if len(debt_ref) == 0:
            print("MKK debt back-fill: debt reference is empty or missing — "
                  "run scripts/import_mkk_debt.py on the MKK export first.")
        else:
            dstats = backfill_debt(
                conn, debt_ref, threshold=args.threshold, limit=args.limit,
                create_missing_issuers=args.create_missing_issuers,
                report_path=args.debt_report,
            )
            print("MKK debt back-fill (issuer -> debt Securities):")
            print(f"  reference        {len(debt_ref)} instruments · {debt_ref.source!r}")
            for k in ("issuers_total", "issuers_matched",
                      "issuers_attached_to_stub", "issuers_unmatched",
                      "issuers_sovereign_excluded", "securities_written",
                      "securities_unmatched", "securities_sovereign_excluded",
                      "edges_written", "in_scope_reference", "unmatched_rate",
                      "low_confidence_maturities", "quarantined_isins"):
                print(f"  {k:28} {dstats[k]}")
            print(f"  report           {dstats['report']}")

    if args.stage in ("nominal", "all"):
        nom_ref = KapNominalReference(reference_path=args.nominal_reference)
        nom_ref.load()
        if len(nom_ref.all()) == 0:
            print("KAP nominal back-fill: nominal reference is empty or missing — "
                  "run scripts/extract_kap_nominals.py to harvest issuance amounts first.")
        else:
            nstats = backfill_nominals(
                conn, reference=nom_ref, report_path=args.nominal_report,
            )
            print("KAP nominal back-fill (issued amount -> Security by ISIN):")
            for k in ("reference_records", "matched", "absent_from_graph",
                      "rejected_on_load"):
                print(f"  {k:20} {nstats[k]}")

    if args.stage in ("issuance", "all"):
        records = load_issuance_reference()
        if not records:
            print("KAP issuance back-fill: issuance reference empty or missing — "
                  "run scripts/discover_kap_issuances.py to harvest first.")
        else:
            istats = backfill_from_issuances(conn, records)
            print("KAP issuance back-fill (create/price Securities from bulletins):")
            for k in ("records_in", "written", "new_instruments",
                      "unmatched_issuer", "out_of_scope_class"):
                print(f"  {k:20} {istats[k]}")

    if args.stage in ("subsidiary", "all"):
        relations = load_subsidiary_reference()
        if not relations:
            print("KAP subsidiary back-fill: reference empty or missing — "
                  "run scripts/discover_kap_subsidiaries.py to harvest first.")
        else:
            sstats = backfill_subsidiaries(conn, relations, threshold=args.threshold)
            print("KAP subsidiary back-fill (CONTROLS / SUBSIDIARY_OF / HOLDS_STAKE):")
            for k in ("relations_in", "matched", "controls_new",
                      "controls_corroborated", "holds_stake_new",
                      "unmatched_child", "unmatched_parent", "self_links_skipped"):
                print(f"  {k:24} {sstats[k]}")
            print(f"  report                 {sstats['report']}")

    if args.stage in ("spv", "all"):
        spvstats = backfill_spv_parents(conn)
        print("SPV->parent inference (CONTROLS from naming convention):")
        for k in ("spv_candidates", "controls_new", "controls_corroborated",
                  "ambiguous", "no_in_graph_parent", "no_brand"):
            print(f"  {k:22} {spvstats[k]}")
        print(f"  report                 {spvstats['report']}")
        jv = demote_jv_suspect_edges(conn)
        print(f"  jv_suspects_demoted    {jv['jv_suspects_demoted']}")

    if args.stage == "stubs":
        ststats = backfill_external_stubs(conn)
        print("External stub parents (EXTERNAL_STUB — bounded universe widening, F3):")
        for k in ("spv_no_parent_rows", "stubs_created", "controls_new",
                  "controls_corroborated", "spv_not_in_graph"):
            print(f"  {k:24} {ststats[k]}")
        print(f"  report                   {ststats['report']}")

    # Coverage is reported over the IN-UNIVERSE companies only (the curated
    # 729). EXTERNAL_STUB / EXTERNAL_PARENT nodes are control anchors, not part of
    # the listed-coverage denominator — counting them would silently move the bar.
    in_universe = ("(c.listing_status IS NULL "
                   "OR NOT c.listing_status STARTS WITH 'EXTERNAL')")
    total = conn.execute(
        f"MATCH (c:Company) WHERE {in_universe} RETURN count(c)").get_next()[0]
    with_lei = conn.execute(
        f"MATCH (c:Company) WHERE {in_universe} "
        "AND c.lei IS NOT NULL AND c.lei <> '' RETURN count(c)"
    ).get_next()[0]
    with_isin = conn.execute(
        f"MATCH (c:Company) WHERE {in_universe} "
        "AND c.isin IS NOT NULL AND c.isin <> '' RETURN count(c)"
    ).get_next()[0]
    n_stub = conn.execute(
        "MATCH (c:Company) WHERE c.listing_status STARTS WITH 'EXTERNAL' "
        "RETURN count(c)").get_next()[0]
    print(f"  graph coverage   {with_lei}/{total} carry an LEI · "
          f"{with_isin}/{total} carry an ISIN  (+{n_stub} external stubs, excluded)")

    # Post-load integrity (F6): the CONTROLS graph must be a DAG. Fail loud so a
    # cycle cannot reach an analyst as a confident-looking group total. Runs on
    # every --stage so any loader that admits one is caught at the source.
    try:
        rep = check_no_controls_cycles(conn)
        print(f"  integrity        CONTROLS cycles: {rep['controls_cycles']} (OK, DAG)")
    except ControlsCycleError as exc:
        raise SystemExit(f"INTEGRITY FAILURE — {exc}")


if __name__ == "__main__":
    main()

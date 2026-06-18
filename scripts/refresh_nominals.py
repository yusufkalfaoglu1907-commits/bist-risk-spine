#!/usr/bin/env python3
"""One-shot refresh: harvest new KAP issuance nominals, then load them.

This is the hands-off entry point for keeping debt magnitudes current. It runs
two existing steps back-to-back:

  1. extract_kap_nominals (--from-graph): walk debt issuers' KAP feeds, harvest
     newly-disclosed issuance amounts into data/reference/kap_nominal.json
     (resumable, idempotent, confidence-gated — only unambiguous single-ISIN
     pairs).
  2. backfill_nominals (--stage nominal): MERGE the reference onto Securities by
     ISIN.

What this DOES keep current without you touching anything:
  • nominal coverage of instruments already in the graph (grows each run);
  • *outstanding* amounts — those are computed as-of query time from
    (nominal, maturity, is_amortizing), so matured paper rolls off automatically
    with no refresh at all (see analytics/outstanding.py).

What this does NOT do (honest limit):
  • discover brand-new INSTRUMENTS (new ISINs) — those still enter via the MKK
    debt reference (scripts/import_mkk_debt.py on a fresh export). Harvesting new
    instruments straight from KAP issuance disclosures is the next step to remove
    that dependency.

Designed to be cron/scheduler-friendly:
    PYTHONPATH=src python scripts/refresh_nominals.py --db ./data/tmkg.kuzu --budget 120
"""
from __future__ import annotations

import argparse
import datetime as _dt

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_adapter import KapAdapter
from tmkg.adapters.kap_nominal_adapter import (
    KapNominalReference, extract_nominals, write_nominal_reference,
)
from tmkg.loaders.nominal_backfill import backfill_nominals
from tmkg import config

REF_PATH = config.REPO_ROOT / "data" / "reference" / "kap_nominal.json"


def harvest(years, budget: float) -> int:
    """Walk every debt issuer's KAP feed within the time budget, accumulating
    confident (ISIN -> nominal) pairs into the reference. Returns new-ISIN count."""
    import time
    t0 = time.time()
    have = {r.isin: r for r in KapNominalReference(REF_PATH).all()}
    new = 0
    with KapAdapter() as adapter:
        targets = [m for m in adapter.fetch_members() if m.mkk_oid]
        for m in targets:
            if time.time() - t0 > budget:
                break
            for y in years:
                try:
                    ds = adapter.fetch_disclosures(m.mkk_oid, f"{y}-01-01", f"{y}-12-31")
                except Exception:
                    continue
                for d in ds:
                    if time.time() - t0 > budget:
                        break
                    idx = getattr(d, "index", None) or getattr(d, "disclosureIndex", None)
                    if not idx:
                        continue
                    try:
                        html = adapter.fetch_detail_html(int(idx))
                    except Exception:
                        continue
                    for rec in extract_nominals(html, source=f"KAP:{idx}")["records"]:
                        if rec.isin not in have:
                            new += 1
                        have[rec.isin] = rec
    write_nominal_reference(list(have.values()), REF_PATH,
                            source="KAP issuance disclosures (refresh)", complete=False)
    return new


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--years", default=None, help="comma-separated; default = this year + last")
    ap.add_argument("--budget", type=float, default=120.0, help="harvest seconds")
    ap.add_argument("--skip-harvest", action="store_true",
                    help="only load the existing reference (no KAP fetch)")
    args = ap.parse_args()

    this_year = _dt.date.today().year
    years = ([int(y) for y in args.years.split(",")] if args.years
             else [this_year - 1, this_year])

    if not args.skip_harvest:
        added = harvest(years, args.budget)
        print(f"harvest: +{added} new ISINs into the reference")

    conn = connect(args.db)
    apply_schema(conn)
    stats = backfill_nominals(conn, reference_path=REF_PATH)
    print("load:", {k: stats[k] for k in
                    ("reference_records", "matched", "absent_from_graph")})


if __name__ == "__main__":
    main()

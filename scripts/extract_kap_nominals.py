#!/usr/bin/env python3
"""Harvest issued-nominal amounts from KAP disclosure detail pages.

Walks the disclosure feeds of debt issuers (or an explicit list of disclosure
indices), fetches each detail page, runs the confidence-gated extractor
(`kap_nominal_adapter.extract_nominals` — only unambiguous single-amount-per-ISIN
pairs), and accumulates verified (ISIN -> nominal) records into the committed
reference file ``data/reference/kap_nominal.json``.

Resumable + time-budgeted (KAP is slow and the sandbox caps tool calls): it
loads any existing reference, processes until the budget elapses, then writes
back. Re-run until coverage stops growing. Loading the reference into the graph
is a separate, fast, offline step: ``backfill_gleif.py --stage nominal``.

Examples:
    # walk KOCFN's + YKBNK's disclosure feeds for 2025-2026, 40s budget
    PYTHONPATH=src python scripts/extract_kap_nominals.py \
        --issuers KOCFN,YKBNK,ARCLK --years 2025,2026 --budget 40

    # harvest specific bulletin disclosure indices directly
    PYTHONPATH=src python scripts/extract_kap_nominals.py --indices 1529430,1527784

    # walk every matched debt issuer from the KAP member cache
    PYTHONPATH=src python scripts/extract_kap_nominals.py --from-graph --budget 40
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from tmkg import config
from tmkg.adapters.kap_adapter import KapAdapter
from tmkg.adapters.kap_nominal_adapter import (
    KapNominalReference, NominalRecord, extract_nominals, write_nominal_reference,
)

REF_PATH = config.REPO_ROOT / "data" / "reference" / "kap_nominal.json"


def _load_existing() -> dict[str, NominalRecord]:
    ref = KapNominalReference(REF_PATH)
    return {r.isin: r for r in ref.all()}


def _disclosure_indices_for(adapter: KapAdapter, mkk_oid: str, years) -> list[int]:
    idx: list[int] = []
    for y in years:
        try:
            ds = adapter.fetch_disclosures(mkk_oid, f"{y}-01-01", f"{y}-12-31")
        except Exception:
            continue
        for d in ds:
            i = getattr(d, "index", None) or getattr(d, "disclosureIndex", None)
            if i:
                idx.append(int(i))
    return idx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issuers", default=None, help="comma-separated tickers")
    ap.add_argument("--from-graph", action="store_true",
                    help="walk every issuer in the KAP member cache")
    ap.add_argument("--indices", default=None,
                    help="comma-separated disclosure indices (skip feed walk)")
    ap.add_argument("--years", default="2025,2026")
    ap.add_argument("--budget", type=float, default=40.0, help="seconds")
    ap.add_argument("--out", default=str(REF_PATH))
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    t0 = time.time()
    have = _load_existing()
    seen_idx: set[int] = set()
    new_records = 0

    with KapAdapter() as adapter:
        members = adapter.fetch_members()
        by_ticker = {m.primary_ticker: m for m in members if m.primary_ticker}

        # build the work list of disclosure indices
        indices: list[int] = []
        if args.indices:
            indices = [int(x) for x in args.indices.split(",") if x.strip()]
        else:
            if args.from_graph:
                targets = [m for m in members if m.mkk_oid]
            else:
                tickers = [t.strip().upper() for t in (args.issuers or "").split(",") if t.strip()]
                targets = [by_ticker[t] for t in tickers if t in by_ticker and by_ticker[t].mkk_oid]
            for m in targets:
                if time.time() - t0 > args.budget:
                    break
                indices.extend(_disclosure_indices_for(adapter, m.mkk_oid, years))

        for idx in indices:
            if time.time() - t0 > args.budget:
                print(f"[budget] stopping after {idx}")
                break
            if idx in seen_idx:
                continue
            seen_idx.add(idx)
            try:
                html = adapter.fetch_detail_html(idx)
            except Exception:
                continue
            out = extract_nominals(html, source=f"KAP:{idx}", as_of=None)
            for rec in out["records"]:
                prev = have.get(rec.isin)
                if prev is None or rec.confidence >= prev.confidence:
                    if prev is None:
                        new_records += 1
                    have[rec.isin] = rec

    write_nominal_reference(list(have.values()), args.out,
                            source="KAP issuance disclosures (detail extract)",
                            complete=False)
    print(f"processed {len(seen_idx)} disclosures in {time.time()-t0:.0f}s | "
          f"reference now {len(have)} ISINs (+{new_records} new) -> {args.out}")


if __name__ == "__main__":
    main()

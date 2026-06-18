#!/usr/bin/env python3
"""Extract corporate-debt instruments from the MKK export into the debt reference.

Reads the SAME "Menkul Kıymetler Listesi" used for the equity ticker→ISIN map,
keeps the debt classes (TRS bonds, TRF financing bills, TRD sukuk, and XS
Eurobonds by default), validates every ISIN (ISO 6166 shape + check digit —
TR *and* XS), infers each instrument's maturity date from its description
(confidence-tagged), and writes `data/reference/mkk_debt.json` with provenance.
Malformed ISINs are reported and EXCLUDED — never written — like the equity side.

The committed reference file (not the raw export) is what the graph loader reads,
so this only needs to run when the MKK list is refreshed.

Examples:
    # default debt classes (TRS/TRF/TRD/XS)
    PYTHONPATH=src python scripts/import_mkk_debt.py mkk_list.xlsx \
        --isin-col "ISIN Kodu" --desc-col "Kıymet Açıklama" --issuer-col "MKKÇ Adı"

    # bonds + sukuk only, no Eurobonds
    PYTHONPATH=src python scripts/import_mkk_debt.py mkk_list.xlsx \
        --isin-col "ISIN Kodu" --desc-col "Kıymet Açıklama" --issuer-col "MKKÇ Adı" \
        --classes TRS TRD
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tmkg.adapters.tabular import read_rows
from tmkg.adapters.mkk_debt_adapter import (
    DEFAULT_DEBT_CLASSES, DEFAULT_DEBT_REFERENCE_PATH, extract_debt, write_reference,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="CSV/TSV/XLSX MKK export to import")
    ap.add_argument("--isin-col", required=True, help="column holding the ISIN")
    ap.add_argument("--desc-col", required=True,
                    help="column with the instrument description (maturity is parsed from it)")
    ap.add_argument("--issuer-col", required=True,
                    help="column with the issuer short name (MKKÇ Adı)")
    ap.add_argument("--classes", nargs="+", default=list(DEFAULT_DEBT_CLASSES),
                    help=f"debt classes to keep (default {' '.join(DEFAULT_DEBT_CLASSES)})")
    ap.add_argument("--source", default="MKK Menkul Kıymetler Listesi (mkk.com.tr) — debt",
                    help="provenance string for the reference file")
    ap.add_argument("--out", default=None,
                    help="output JSON (default data/reference/mkk_debt.json)")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"input not found: {in_path}")
    rows = read_rows(in_path)

    securities, rejected, skipped = extract_debt(
        rows, isin_col=args.isin_col, desc_col=args.desc_col,
        issuer_col=args.issuer_col, classes=tuple(args.classes),
    )
    out_path = write_reference(
        securities, args.out or DEFAULT_DEBT_REFERENCE_PATH,
        source=args.source, classes=tuple(args.classes),
    )

    by_class: dict[str, int] = {}
    issuers = set()
    with_maturity = 0
    for s in securities:
        by_class[s.instrument_class] = by_class.get(s.instrument_class, 0) + 1
        issuers.add(s.issuer_name)
        if s.maturity_date:
            with_maturity += 1

    print(f"rows read:                    {len(rows)}")
    print(f"debt instruments kept:        {len(securities)}  ({dict(sorted(by_class.items()))})")
    print(f"distinct issuers:             {len(issuers)}")
    print(f"with parsed maturity:         {with_maturity}/{len(securities)}")
    print(f"-> {out_path}")
    if rejected:
        print(f"REJECTED {len(rejected)} rows with a malformed ISIN (bad shape/check digit):")
        for r in rejected[:20]:
            print(f"  {r['isin']:14} {r.get('issuer_name','')}")
        if len(rejected) > 20:
            print(f"  ... and {len(rejected) - 20} more")


if __name__ == "__main__":
    main()

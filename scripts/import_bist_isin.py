#!/usr/bin/env python3
"""Import an authoritative BİST/MKK ticker→ISIN export into the reference file.

Takes a CSV/TSV/XLSX export (e.g. the MKK "Menkul Kıymetler Listesi" or a Borsa
İstanbul equity list) with a ticker column and an ISIN column, keeps the
listed-equity ISINs (Turkish class TRA/TRE), validates every code (Turkish shape
+ ISO 6166 check digit), and writes `data/reference/bist_isin.json` with
provenance. Anything that fails validation is reported and EXCLUDED — never
written — consistent with the project's "never persist a guessed/garbage ISIN"
rule.

Multi-share-class tickers (the MKK list registers every share line under the
SAME exchange code) are NOT collapsed by guessing. Resolution, in order:
  1. exactly one equity-class ISIN  -> written to `mappings`;
  2. several equity ISINs but, after dropping lines whose description matches an
     EXCLUDE pattern (default "İMTİYAZLI" — privileged/founder shares that are
     registered but not the publicly-traded common line), exactly one remains
     -> written to `mappings` (this resolves the common+privileged pairs that
     dominate the MKK list, e.g. TUPRS -> TRATUPRS91E8, not the İMTİYAZLI line);
  3. still several (genuine A/B/C/D group splits with no privileged marker)
     -> parked in an `ambiguous` block (candidates preserved) for review.
The exclude-based pick needs a description column (`--desc-col`); without one,
only rule 1 applies and everything else is parked ambiguous.

Examples:
    # MKK Menkul Kıymetler Listesi (Borsa Kodu / ISIN Kodu / Kıymet Açıklama)
    PYTHONPATH=src python scripts/import_bist_isin.py mkk_list.xlsx \
        --ticker-col "Borsa Kodu" --isin-col "ISIN Kodu" --desc-col "Kıymet Açıklama" \
        --source "MKK Menkul Kıymetler Listesi (mkk.com.tr)"

    # generic CSV
    PYTHONPATH=src python scripts/import_bist_isin.py equities.csv \
        --ticker-col Symbol --isin-col ISIN
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from tmkg.adapters.tabular import read_rows as _read_rows
from tmkg.adapters.bist_isin_adapter import (
    DEFAULT_REFERENCE_PATH, REFERENCE_SCHEMA_VERSION, is_valid_isin, is_equity_isin,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="CSV/TSV/XLSX export to import")
    ap.add_argument("--ticker-col", required=True, help="column holding the BİST ticker")
    ap.add_argument("--isin-col", required=True, help="column holding the ISIN")
    ap.add_argument("--desc-col", default=None,
                    help="column with the security description (enables exclude-based "
                         "disambiguation of common vs privileged lines)")
    ap.add_argument("--exclude-pattern", default="İMTİYAZL",
                    help="case/diacritic-insensitive substring marking a NON-traded "
                         "line to drop when disambiguating (default 'İMTİYAZL')")
    ap.add_argument("--source", default=None, help="provenance string for the reference file")
    ap.add_argument("--out", default=None, help="output JSON (default data/reference/bist_isin.json)")
    ap.add_argument("--keep-all-classes", action="store_true",
                    help="keep non-equity ISIN classes too (default: TRA/TRE equity only)")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"input not found: {in_path}")
    rows = _read_rows(in_path)

    def _fold(s: str) -> str:
        repl = {"İ": "I", "ı": "I", "Ş": "S", "ş": "S", "Ç": "C", "ç": "C",
                "Ğ": "G", "ğ": "G", "Ö": "O", "ö": "O", "Ü": "U", "ü": "U"}
        return "".join(repl.get(c, c) for c in s).upper()

    excl = _fold(args.exclude_pattern)

    # ticker -> {isin: description}
    by_ticker: dict[str, dict[str, str]] = defaultdict(dict)
    rejected: list[dict] = []
    foreign = 0
    seen_rows = 0
    for r in rows:
        tk = str(r.get(args.ticker_col, "") or "").strip().upper()
        isin = str(r.get(args.isin_col, "") or "").strip().upper()
        desc = str(r.get(args.desc_col, "") or "") if args.desc_col else ""
        if not tk or not isin:
            continue
        seen_rows += 1
        ok = is_valid_isin(isin) if args.keep_all_classes else is_equity_isin(isin)
        if not ok:
            if re.match(r"^[A-Z]{2}", isin) and not isin.startswith("TR"):
                foreign += 1            # valid foreign ISIN, just not a TR equity
            elif is_valid_isin(isin):
                foreign += 1            # valid TR non-equity (debt/warrant/right)
            else:
                rejected.append({"ticker": tk, "isin": isin})
            continue
        by_ticker[tk][isin] = desc

    mappings: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    resolved_by_exclude = 0
    for tk, isin_desc in by_ticker.items():
        isins = sorted(isin_desc)
        if len(isins) == 1:
            mappings[tk] = isins[0]
            continue
        # drop privileged/non-traded lines, then see if one common line remains
        common = [i for i in isins if excl not in _fold(isin_desc[i])]
        if len(common) == 1:
            mappings[tk] = common[0]
            resolved_by_exclude += 1
        else:
            ambiguous[tk] = isins

    out_path = Path(args.out or DEFAULT_REFERENCE_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "source": args.source or f"imported from {in_path.name}",
        "fetched_iso": date.today().isoformat(),
        "method": "official-export",
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "complete": True,
        "disambiguation": (f"single equity ISIN, or unique non-'{args.exclude_pattern}' "
                           f"common line") if args.desc_col else "single equity ISIN only",
        "mappings": dict(sorted(mappings.items())),
        "ambiguous": dict(sorted(ambiguous.items())),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"rows with ticker+ISIN seen:          {seen_rows}")
    print(f"  foreign / non-equity TR skipped:   {foreign}")
    print(f"ticker->ISIN written to 'mappings':  {len(mappings)} "
          f"({resolved_by_exclude} resolved by dropping '{args.exclude_pattern}' lines)")
    print(f"multi-line tickers parked 'ambiguous': {len(ambiguous)}")
    print(f"-> {out_path}")
    if rejected:
        print(f"REJECTED {len(rejected)} rows with a malformed ISIN (bad shape/check digit):")
        for r in rejected[:20]:
            print(f"  {r['ticker']:10} {r['isin']}")
        if len(rejected) > 20:
            print(f"  ... and {len(rejected) - 20} more")


if __name__ == "__main__":
    main()

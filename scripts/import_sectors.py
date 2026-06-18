#!/usr/bin/env python3
"""Import the KAP "Sektörler" classification export into the sector reference file.

WHAT THIS SOLVES
----------------
The live graph seeds Company nodes from KAP but carries NO sector classification
(KAP's member-list endpoint has no sector field; the offline fixtures only ship an
illustrative 6-sector sample). The authoritative classification lives in KAP's
"Sektörler" listing — a hierarchical, two-level taxonomy (16 main sectors, each
with sub-sectors) where every listed company sits in exactly one leaf sub-sector
and, by roll-up, its parent main sector.

Like the BİST/MKK ISIN list, that taxonomy is treated as a COMMITTED, DATED
reference file (`data/reference/sectors.json`) — versioned on disk, carrying its
source and fetch date, validated on load — never silently re-scraped.

THE EXPORT SHAPE (why this needs a dedicated parser, not tabular.read_rows)
--------------------------------------------------------------------------
The export is not a flat table. It is the KAP page layout flattened to a grid:

    <SECTOR NAME>                    (col A only)
    Sıra | Kod | Şirket Unvanı       (the band header)
    1    | AGROT | AGROTECH ...       (member rows; Kod may list several codes:
    2    | GARAN, TGB | ...           a company's primary + legacy/again codes)
    <blank>
    <NEXT SECTOR NAME>
    ...

Main sectors are followed by their sub-sectors, and a sub-sector's member set is
always a subset of its parent's. We reconstruct the two levels purely from that
containment property (no hard-coded sector names): a header whose member set is a
subset of the current main sector is a sub-sector (leaf); otherwise it opens a
new main sector. Empty sectors ("Kayıt Bulunamadı") are preserved as nodes.

Sector codes are slugs derived from the name (ASCII-folded Turkish, upper-snake);
collisions — a main sector and a sub-sector sharing a name, e.g. "ULAŞTIRMA VE
DEPOLAMA" — are disambiguated with a numeric suffix, the main sector keeping the
base slug (it is seen first).

Usage:
    PYTHONPATH=src python scripts/import_sectors.py "Sektörler.xlsx" \
        --source "KAP Sektörler listing (kap.org.tr) export"
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from tmkg.adapters.tabular import read_xlsx
from tmkg.adapters.sector_adapter import DEFAULT_REFERENCE_PATH, REFERENCE_SCHEMA_VERSION

_BAND_HEADER = ("Sıra", "Kod", "Şirket Unvanı")
_EMPTY_MARKER = "Kayıt Bulunamadı"
_FOLD = {"İ": "I", "ı": "I", "I": "I", "Ş": "S", "ş": "S", "Ç": "C", "ç": "C",
         "Ğ": "G", "ğ": "G", "Ö": "O", "ö": "O", "Ü": "U", "ü": "U", "â": "A"}


def fold(s: str) -> str:
    return "".join(_FOLD.get(c, c) for c in (s or "")).upper()


def slug(name: str) -> str:
    s = fold(name)
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")
    return s or "SECTOR"


def split_codes(cell: str) -> list[str]:
    """A 'Kod' cell may carry several codes (primary + legacy share lines)."""
    return [c.strip().upper() for c in re.split(r"[,/;]", cell or "") if c.strip()]


def parse_grid(grid: list[list]) -> list[dict]:
    """Return ordered raw sections: {name, codes:set}. Header bands and blank
    rows are skipped; empty ('Kayıt Bulunamadı') sectors are kept (codes empty)."""
    sections: list[dict] = []
    cur: dict | None = None
    for row in grid:
        a = (str(row[0]).strip() if len(row) > 0 and row[0] is not None else "")
        b = (str(row[1]).strip() if len(row) > 1 and row[1] is not None else "")
        c = (str(row[2]).strip() if len(row) > 2 and row[2] is not None else "")
        if not a and not b and not c:
            continue
        if a == _BAND_HEADER[0] and b == _BAND_HEADER[1]:
            continue
        if a and not b and not c:                      # sector-name header
            if a == _EMPTY_MARKER:
                continue
            cur = {"name": a, "codes": set()}
            sections.append(cur)
            continue
        if a == _EMPTY_MARKER:
            continue
        if cur is not None and b:                       # member row
            cur["codes"].update(split_codes(b))
    return sections


def build_tree(sections: list[dict]) -> list[dict]:
    """Assign level (1=main, 2=sub) and parent by member-set containment."""
    parent: dict | None = None
    for s in sections:
        if s["codes"] and parent is not None and s["codes"].issubset(parent["codes"]):
            s["level"], s["parent"] = 2, parent
        elif s["codes"]:
            s["level"], s["parent"], parent = 1, None, s
        else:                                           # empty sector
            if parent is not None:
                s["level"], s["parent"] = 2, parent
            else:
                s["level"], s["parent"] = 1, None
    return sections


def assign_codes(sections: list[dict]) -> None:
    """Stable slug PKs; disambiguate name collisions (main keeps base slug)."""
    used: set[str] = set()
    for s in sections:
        base = slug(s["name"])
        code, i = base, 1
        while code in used:
            i += 1
            code = f"{base}_{i}"
        used.add(code)
        s["code"] = code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="KAP Sektörler .xlsx export")
    ap.add_argument("--source", default=None, help="provenance string")
    ap.add_argument("--out", default=None,
                    help="output JSON (default data/reference/sectors.json)")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"input not found: {in_path}")

    sections = parse_grid(read_xlsx(in_path))
    build_tree(sections)
    assign_codes(sections)

    sectors = [{"code": s["code"], "name": s["name"], "level": s["level"],
                "parent": s["parent"]["code"] if s["parent"] else None}
               for s in sections]

    # memberships: every code (primary or legacy) -> its leaf (sub-sector) code.
    memberships: dict[str, str] = {}
    conflicts: dict[str, list[str]] = defaultdict(list)
    for s in sections:
        if s["level"] != 2:
            continue
        for code in s["codes"]:
            if code in memberships and memberships[code] != s["code"]:
                conflicts[code].append(s["code"])
            else:
                memberships[code] = s["code"]

    n_main = sum(1 for s in sectors if s["level"] == 1)
    n_sub = sum(1 for s in sectors if s["level"] == 2)

    out_path = Path(args.out or DEFAULT_REFERENCE_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "source": args.source or f"imported from {in_path.name}",
        "fetched_iso": date.today().isoformat(),
        "method": "official-export",
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "complete": True,
        "structure": "two-level KAP taxonomy: level 1 = main sector, level 2 = "
                     "sub-sector (leaf). Companies link to their leaf; the main "
                     "sector is one SUBSECTOR_OF hop up.",
        "sectors": sectors,
        "memberships": dict(sorted(memberships.items())),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"sectors: {len(sectors)}  ({n_main} main, {n_sub} sub)")
    print(f"ticker->leaf memberships: {len(memberships)}")
    if conflicts:
        print(f"WARNING: {len(conflicts)} codes mapped to multiple leaves: "
              f"{dict(list(conflicts.items())[:10])}")
    print(f"-> {out_path}")


if __name__ == "__main__":
    main()

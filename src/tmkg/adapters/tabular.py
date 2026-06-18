"""Minimal, dependency-light readers for the messy exports this project ingests.

openpyxl chokes on some real-world MKK / exchange .xlsx exports (malformed
stylesheet), so ``read_xlsx`` parses the sheet XML directly (zip + ElementTree)
and tolerates both shared-string and inline-string cells. ``read_rows``
dispatches on the file extension and returns a list of header→value dicts.

Used by ``scripts/import_bist_isin.py`` (equity ticker→ISIN) so there is
exactly one parser to trust and test for the MKK "Menkul Kıymetler Listesi".
"""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for c in letters:
        n = n * 26 + (ord(c) - 64)
    return n - 1


def read_xlsx(path: Path) -> list[list]:
    """Style-independent .xlsx reader returning a grid of rows (lists of cells)."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter()
                                      if t.tag.endswith("}t")))
        sheets = sorted(n for n in z.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        root = ET.fromstring(z.read(sheets[0]))
        out: list[list] = []
        for r in root.find("m:sheetData", _NS).findall("m:row", _NS):
            cells: dict[int, str] = {}
            for c in r.findall("m:c", _NS):
                idx = _col_index(c.get("r"))
                v = c.find("m:v", _NS)
                if v is not None:
                    cells[idx] = (shared[int(v.text)] if c.get("t") == "s" else v.text)
                else:
                    isv = c.find("m:is", _NS)
                    if isv is not None:
                        cells[idx] = "".join(t.text or "" for t in isv.iter()
                                             if t.tag.endswith("}t"))
            width = (max(cells) + 1) if cells else 0
            out.append([cells.get(i) for i in range(width)])
        return out


def read_rows(path: Path) -> list[dict]:
    """Read .xlsx/.xlsm/.csv/.tsv into a list of header→value dicts."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        grid = read_xlsx(path)
        if not grid:
            return []
        header = [str(c).strip() if c is not None else "" for c in grid[0]]
        return [dict(zip(header, r)) for r in grid[1:]]
    delim = "\t" if suffix in (".tsv", ".tab") else ","
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=delim))

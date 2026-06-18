"""Offline tests for KAP issuance discovery (extractor + loader).

The synthetic page reproduces the real bulletin's cell order — issuer/ticker/type
PRECEDE the ISIN — so the off-by-one association is exercised explicitly.

    PYTHONPATH=src python -m pytest tests/test_kap_issuance.py -v
"""
from __future__ import annotations

import datetime as _dt
import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_issuance_adapter import extract_issuances
from tmkg.loaders.kap_issuance_backfill import backfill_from_issuances

# Real KAP bulletin rows (real ISINs so ISO-6166 validation passes). Issuer NAME,
# TICKER, TYPE precede the ISIN; then nominal + 3 dates (issue, maturity, value).
# Two in-universe rows (KOCFN, KNTFA) + one SPV whose ticker isn't in the graph.
PAGE = (
    "Nominal Değer (TL) "
    "Koç Finansman A.Ş. KOCFN Tahvil TRSKCTF22726 620.000.000 01.01.2026 12.02.2027 03.01.2026 - "
    "Kent Finans Faktoring A.Ş. KNTFA Bono TRFKNTF62627 31.000.000 01.01.2026 24.06.2026 03.01.2026 - "
    "KT Sukuk Varlık Kiralama A.Ş. KTSVK Kira Sertifikası TRDKTSK52648 290.000.000 01.01.2026 22.05.2026 03.01.2026 - "
)


def _graph():
    conn = connect(Path(tempfile.mkdtemp()) / "iss.kuzu")
    apply_schema(conn)
    for u, t, n in [("c-kocfn", "KOCFN", "KOÇ FİNANSMAN A.Ş."),
                    ("c-kntfa", "KNTFA", "KENT FİNANS FAKTORİNG A.Ş.")]:
        conn.execute("CREATE (:Company {uuid:$u, ticker:$t, name:$n, is_listed:true})",
                     {"u": u, "t": t, "n": n})
    return conn


def test_extractor_aligns_issuer_before_isin():
    out = extract_issuances(PAGE, source="KAP:T")
    by_isin = {r["isin"]: r for r in out["records"]}
    assert set(by_isin) == {"TRSKCTF22726", "TRFKNTF62627", "TRDKTSK52648"}
    koc = by_isin["TRSKCTF22726"]
    assert koc["ticker"] == "KOCFN"               # NOT KNTFA — alignment correct
    assert koc["instrument_class"] == "TRS"
    assert koc["maturity_date"] == "2027-02-12"   # the latest date in the row
    assert koc["nominal"] == 620_000_000.0
    assert by_isin["TRFKNTF62627"]["ticker"] == "KNTFA"


def test_requires_tl_label():
    assert extract_issuances(PAGE.replace("Nominal Değer (TL)", ""), "x")["records"] == []


def test_loader_creates_priced_securities():
    conn = _graph()
    recs = extract_issuances(PAGE, source="KAP:T")["records"]
    stats = backfill_from_issuances(conn, recs)
    # KOCFN + KNTFA matched by ticker and created; KTSVK (SPV) not in graph
    assert stats["written"] == 2
    assert stats["new_instruments"] == 2
    assert stats["unmatched_issuer"] == 1
    # the created Security carries amount + maturity + bullet flag
    row = conn.execute(
        "MATCH (c:Company {ticker:'KOCFN'})-[i:ISSUES]->(s:Security {isin:'TRSKCTF22726'}) "
        "RETURN s.nominal, s.maturity_date, s.is_amortizing, i.instrument_class").get_next()
    assert row[0] == 620_000_000.0
    assert row[1] == _dt.date(2027, 2, 12)
    assert row[2] is False                          # TRS = bullet
    assert row[3] == "TRS"


def test_loader_is_idempotent_and_merges():
    conn = _graph()
    recs = extract_issuances(PAGE, source="KAP:T")["records"]
    backfill_from_issuances(conn, recs)
    backfill_from_issuances(conn, recs)             # second run
    n = conn.execute("MATCH (s:Security) WHERE s.isin='TRSKCTF22726' "
                     "RETURN count(s)").get_next()[0]
    assert n == 1                                   # MERGE, no duplicate
    edges = conn.execute("MATCH (:Company)-[i:ISSUES]->(:Security {isin:'TRSKCTF22726'}) "
                         "RETURN count(i)").get_next()[0]
    assert edges == 1

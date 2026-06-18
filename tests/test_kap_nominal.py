"""Offline tests for the KAP nominal extractor + loader.

Covers Turkish money parsing, the confidence gate (single-amount-per-ISIN only),
currency gating (TL label required), reference round-trip, and the loader's
match-only / idempotent behaviour.

    PYTHONPATH=src python -m pytest tests/test_kap_nominal.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_nominal_adapter import (
    parse_tr_amount, extract_nominals, write_nominal_reference,
    KapNominalReference, NominalRecord,
)
from tmkg.loaders.nominal_backfill import backfill_nominals


# --- Turkish amount parsing ------------------------------------------------

def test_parse_tr_amount_variants():
    assert parse_tr_amount("1.400.000.000") == 1_400_000_000.0
    assert parse_tr_amount("620.000.000") == 620_000_000.0
    assert parse_tr_amount("31.000.000,50") == 31_000_000.5
    # not a Turkish-grouped amount → reject (don't guess)
    assert parse_tr_amount("1000000") is None
    assert parse_tr_amount("abc") is None
    assert parse_tr_amount(None) is None


# --- extractor gate --------------------------------------------------------

def _page(rows: str) -> str:
    """Minimal stand-in for a KAP detail page: the TL label + ISIN<ws>amount
    cells in document order (tags get stripped by the extractor)."""
    return ("<div>Nominal Değer (TL)</div>" + rows)


def test_extract_single_issuance_emits():
    html = _page("<td>TRSKCTF22726</td> <td>620.000.000</td>")
    out = extract_nominals(html, source="KAP:1")
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r.isin == "TRSKCTF22726" and r.nominal == 620_000_000.0
    assert r.currency == "TRY"


def test_extract_multi_isin_bulletin_each_paired():
    html = _page(
        "TRSKCTF22726 620.000.000 "
        "TRFKNTF62627 31.000.000 "
        "TRFMNGF72611 130.000.000"
    )
    out = extract_nominals(html, source="KAP:2")
    got = {r.isin: r.nominal for r in out["records"]}
    assert got == {
        "TRSKCTF22726": 620_000_000.0,
        "TRFKNTF62627": 31_000_000.0,
        "TRFMNGF72611": 130_000_000.0,
    }


def test_extract_conflicting_amount_is_ambiguous_not_emitted():
    # same ISIN shows two different amounts → must NOT be emitted
    html = _page("TRSKCTF22726 620.000.000 something TRSKCTF22726 999.000.000")
    out = extract_nominals(html, source="KAP:3")
    assert out["records"] == []
    assert out["ambiguous"] and out["ambiguous"][0]["isin"] == "TRSKCTF22726"


def test_extract_requires_tl_label():
    # no 'Nominal Değer (TL)' label → not a TL nominal disclosure → nothing
    html = "<td>TRSKCTF22726</td> <td>620.000.000</td>"
    assert extract_nominals(html, source="KAP:4")["records"] == []


# --- reference round-trip --------------------------------------------------

def test_reference_roundtrip_and_validation():
    recs = [
        NominalRecord("TRSKCTF22726", 620_000_000.0, "TRY", "KAP:1", None, 0.9),
        NominalRecord("BADISIN", 1.0, "TRY", "KAP:1", None, 0.9),  # invalid ISIN
    ]
    p = Path(tempfile.mkdtemp()) / "kap_nominal.json"
    write_nominal_reference(recs, p, source="test")
    ref = KapNominalReference(p).load()
    kept = ref.all()
    assert [r.isin for r in kept] == ["TRSKCTF22726"]   # bad ISIN quarantined
    assert ref.rejected() and ref.rejected()[0]["isin"] == "BADISIN"


# --- loader (match-only, idempotent) --------------------------------------

def _graph_with_security(isin: str):
    conn = connect(Path(tempfile.mkdtemp()) / "nom.kuzu")
    apply_schema(conn)
    conn.execute("CREATE (:Company {uuid:'c1', ticker:'KOCFN', name:'X', is_listed:true})")
    conn.execute(
        "CREATE (:Security {uuid:'s1', isin:$i, type:'BOND', currency:'TRY', "
        "maturity_date:date('2026-09-01')})", {"i": isin})
    conn.execute("MATCH (c:Company {uuid:'c1'}),(s:Security {uuid:'s1'}) "
                 "CREATE (c)-[:ISSUES {instrument_class:'TRS'}]->(s)")
    return conn


def test_loader_matches_by_isin_and_is_idempotent():
    conn = _graph_with_security("TRSKCTF22726")
    p = Path(tempfile.mkdtemp()) / "kap_nominal.json"
    write_nominal_reference(
        [NominalRecord("TRSKCTF22726", 620_000_000.0, "TRY", "KAP:1", "2025-06-01", 0.9),
         NominalRecord("TRFMNGF72611", 5_000_000.0, "TRY", "KAP:1", None, 0.9)],  # valid ISIN, absent from graph
        p, source="test")
    s1 = backfill_nominals(conn, reference_path=p)
    assert s1["matched"] == 1 and s1["absent_from_graph"] == 1
    # value landed on the Security
    v = conn.execute("MATCH (s:Security {isin:'TRSKCTF22726'}) "
                     "RETURN s.nominal, s.nominal_currency, s.nominal_confidence").get_next()
    assert v[0] == 620_000_000.0 and v[1] == "TRY" and abs(v[2] - 0.9) < 1e-9
    # second run is a no-op on counts (idempotent)
    s2 = backfill_nominals(conn, reference_path=p)
    assert s2["matched"] == 1
    n = conn.execute("MATCH (s:Security) WHERE s.nominal IS NOT NULL "
                     "RETURN count(s)").get_next()[0]
    assert n == 1

"""Offline tests for SPV -> parent control inference.

Exercises the precision gates that make name-based inference safe:
  - a real bank SPV resolves to its bank ("Aktif Bank Sukuk Varlık Kiralama" ->
    "Aktif Yatırım Bankası");
  - the GEO-token trap does NOT fire ("Mercedes-Benz Finansman Türk" must not
    attach to "Albaraka Türk" via the shared "Türk");
  - an ambiguous brand (two "Tera" holdings) is logged, not written;
  - a non-SPV operating issuer is never given an inferred parent.

    PYTHONPATH=src python -m pytest tests/test_spv_parent.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders.spv_parent_backfill import backfill_spv_parents

_N = 0


def _co(conn, uuid, ticker, name):
    conn.execute("CREATE (:Company {uuid:$u, ticker:$t, name:$n, is_listed:true})",
                 {"u": uuid, "t": ticker, "n": name})


def _debt(conn, issuer_uuid, cls="TRD"):
    """Give an issuer one debt-class Security so it counts as a debt issuer."""
    global _N
    _N += 1
    sid = f"sec-{_N}"
    conn.execute("CREATE (:Security {uuid:$u, isin:$i, type:'bond'})",
                 {"u": sid, "i": f"TR{_N:010d}"})
    conn.execute("MATCH (c:Company {uuid:$c}), (s:Security {uuid:$s}) "
                 "CREATE (c)-[:ISSUES {instrument_class:$cls}]->(s)",
                 {"c": issuer_uuid, "s": sid, "cls": cls})


def _graph():
    conn = connect(Path(tempfile.mkdtemp()) / "spv.kuzu")
    apply_schema(conn)
    # parent banks / holdings
    _co(conn, "p-afb", "AFB", "AKTİF YATIRIM BANKASI A.Ş.")
    _co(conn, "p-albrk", "ALBRK", "ALBARAKA TÜRK KATILIM BANKASI A.Ş.")
    _co(conn, "p-tera1", "TERHOL", "TERA HOLDİNG A.Ş.")
    _co(conn, "p-tera2", "TEHOL", "TERA YATIRIM HOLDİNG A.Ş.")
    # SPV / finance-arm debt issuers
    _co(conn, "s-aktvk", "AKTVK", "AKTİF BANK SUKUK VARLIK KİRALAMA A.Ş.")
    _co(conn, "s-mbftr", "MBFTR", "MERCEDES-BENZ FİNANSMAN TÜRK A.Ş.")
    _co(conn, "s-tera", "TERFA", "TERA FİNANS FAKTORİNG A.Ş.")
    # non-SPV operating debt issuer
    _co(conn, "s-pgsus", "PGSUS", "PEGASUS HAVA TAŞIMACILIĞI A.Ş.")
    for u in ("s-aktvk", "s-mbftr", "s-tera", "s-pgsus"):
        _debt(conn, u)
    return conn


def test_resolves_bank_spv_to_its_bank():
    conn = _graph()
    stats = backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")
    assert stats["controls_new"] == 1
    row = conn.execute(
        "MATCH (:Company {ticker:'AFB'})-[r:CONTROLS]->(:Company {ticker:'AKTVK'}) "
        "RETURN r.source, r.confidence").get_next()
    assert row[0] == "spv-name-inference"
    assert row[1] == 0.70
    # mirrored SUBSIDIARY_OF
    n = conn.execute(
        "MATCH (:Company {ticker:'AKTVK'})-[:SUBSIDIARY_OF]->(:Company {ticker:'AFB'}) "
        "RETURN count(*)").get_next()[0]
    assert n == 1


def test_geo_token_does_not_bridge():
    conn = _graph()
    backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")
    # Mercedes-Benz Finansman Türk must NOT attach to Albaraka Türk via "Türk"
    n = conn.execute(
        "MATCH (:Company {ticker:'ALBRK'})-[:CONTROLS]->(:Company {ticker:'MBFTR'}) "
        "RETURN count(*)").get_next()[0]
    assert n == 0
    # and it has no inferred parent at all
    n2 = conn.execute(
        "MATCH (p:Company)-[:CONTROLS]->(:Company {ticker:'MBFTR'}) RETURN count(p)"
    ).get_next()[0]
    assert n2 == 0


def test_ambiguous_brand_is_not_written():
    conn = _graph()
    stats = backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")
    assert stats["ambiguous"] == 1
    n = conn.execute(
        "MATCH (p:Company)-[:CONTROLS]->(:Company {ticker:'TERFA'}) RETURN count(p)"
    ).get_next()[0]
    assert n == 0


def test_non_spv_operating_issuer_skipped():
    conn = _graph()
    backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")
    n = conn.execute(
        "MATCH (p:Company)-[:CONTROLS]->(:Company {ticker:'PGSUS'}) RETURN count(p)"
    ).get_next()[0]
    assert n == 0


def test_non_destructive_and_idempotent():
    conn = _graph()
    # pre-existing higher-grade edge on the same pair
    conn.execute(
        "MATCH (p:Company {ticker:'AFB'}), (c:Company {ticker:'AKTVK'}) "
        "CREATE (p)-[:CONTROLS {source:'GLEIF-L2', confidence:0.95}]->(c)")
    stats = backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")
    assert stats["controls_new"] == 0
    assert stats["controls_corroborated"] == 1
    row = conn.execute(
        "MATCH (:Company {ticker:'AFB'})-[r:CONTROLS]->(:Company {ticker:'AKTVK'}) "
        "RETURN r.source, r.confidence").get_next()
    assert row[0] == "GLEIF-L2"        # not overwritten
    assert row[1] == 0.95
    backfill_spv_parents(conn, report_path=Path(tempfile.mkdtemp()) / "spv.json")          # idempotent
    n = conn.execute(
        "MATCH (:Company {ticker:'AFB'})-[r:CONTROLS]->(:Company {ticker:'AKTVK'}) "
        "RETURN count(r)").get_next()[0]
    assert n == 1

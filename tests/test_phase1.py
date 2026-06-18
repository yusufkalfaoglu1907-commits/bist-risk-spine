"""Phase-1 smoke test. Runs fully offline on fixtures.

    PYTHONPATH=src python -m pytest tests/ -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders import identity, ownership
from tmkg.analytics.exposure import group_exposure, total_group_weight


def _build():
    tmp = Path(tempfile.mkdtemp()) / "tmkg.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    identity.load_companies(conn)
    identity.load_people(conn)
    identity.load_securities(conn)
    identity.load_sectors(conn)
    identity.load_portfolio(conn)
    ownership.load_all(conn)
    return conn


def test_nodes_loaded():
    conn = _build()
    res = conn.execute("MATCH (c:Company) RETURN count(c)")
    assert res.get_next()[0] == 8


def test_issues_edges():
    conn = _build()
    res = conn.execute("MATCH (:Company)-[r:ISSUES]->(:Security) RETURN count(r)")
    assert res.get_next()[0] == 8


def test_provenance_on_stakes():
    """Every HOLDS_STAKE edge must carry provenance (ontology §0)."""
    conn = _build()
    res = conn.execute(
        "MATCH ()-[r:HOLDS_STAKE]->() "
        "WHERE r.source IS NULL OR r.extraction_method IS NULL OR r.confidence IS NULL "
        "RETURN count(r)"
    )
    assert res.get_next()[0] == 0


def test_koc_group_exposure():
    """Exit test: TUPRS/FROTO/ARCLK/YKBNK are Koç; THYAO is not."""
    conn = _build()
    rows = group_exposure(conn, "pf-main", "co-kchol")
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["TUPRS"]["in_group"] is True
    assert by_ticker["FROTO"]["in_group"] is True
    assert by_ticker["ARCLK"]["in_group"] is True
    assert by_ticker["YKBNK"]["in_group"] is True
    assert by_ticker["THYAO"]["in_group"] is False
    # 0.25 + 0.20 + 0.15 + 0.10 = 0.70 of the portfolio is Koç-controlled
    assert abs(total_group_weight(rows) - 0.70) < 1e-6


def test_board_interlock_present():
    """Ali Koç sits on >1 board — basis for Phase-1 interlock queries later."""
    conn = _build()
    res = conn.execute(
        "MATCH (p:Person {uuid:'pe-alikoc'})-[:BOARD_MEMBER_OF]->(c:Company) "
        "RETURN count(c)"
    )
    assert res.get_next()[0] >= 2

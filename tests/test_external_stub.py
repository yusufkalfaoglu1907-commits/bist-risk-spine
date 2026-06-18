"""Offline tests for external stub-parent creation (Phase 2.1, F3).

Exercises the bounded universe widening: SPVs whose lead brand has NO in-graph
parent get a brand-keyed EXTERNAL_STUB parent + control edges, so the group
assembles — while staying excluded from the listed-coverage denominator and
never overwriting a higher-grade control edge.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders.external_stub_backfill import (
    backfill_external_stubs, STUB_STATUS, _stub_uuid,
)


def _co(conn, uuid, ticker, name, status="NON_EQUITY_ISSUER"):
    conn.execute(
        "CREATE (:Company {uuid:$u, ticker:$t, name:$n, is_listed:true, "
        "listing_status:$s})",
        {"u": uuid, "t": ticker, "n": name, "s": status})


def _reports_dir(no_parent_rows):
    d = Path(tempfile.mkdtemp())
    (d / "spv_parent_report.json").write_text(
        json.dumps({"no_in_graph_parent": no_parent_rows}, ensure_ascii=False),
        encoding="utf-8")
    return d


def _graph():
    conn = connect(Path(tempfile.mkdtemp()) / "stub.kuzu")
    apply_schema(conn)
    # two same-brand SPVs (DENIZ) + one solo (ZIRAAT)
    _co(conn, "s-dnfin", "DNFIN", "DENİZ FİNANSAL KİRALAMA A.Ş.")
    _co(conn, "s-denfa", "DENFA", "DENİZ FAKTORİNG A.Ş.")
    _co(conn, "s-zrtvk", "ZRTVK", "ZİRAAT VARLIK KİRALAMA A.Ş.")
    return conn


_ROWS = [
    {"spv": "DNFIN", "brand": "DENIZ", "name": "DENİZ FİNANSAL KİRALAMA A.Ş."},
    {"spv": "DENFA", "brand": "DENIZ", "name": "DENİZ FAKTORİNG A.Ş."},
    {"spv": "ZRTVK", "brand": "ZIRAAT", "name": "ZİRAAT VARLIK KİRALAMA A.Ş."},
]


def test_one_stub_per_brand_controls_all_siblings():
    conn = _graph()
    stats = backfill_external_stubs(conn, reports_dir=_reports_dir(_ROWS))
    # two brands -> two stubs; three control edges
    assert stats["stubs_created"] == 2
    assert stats["controls_new"] == 3
    # the DENIZ stub controls BOTH Deniz SPVs (group assembles)
    res = conn.execute(
        f"MATCH (p:Company {{uuid:$u}})-[:CONTROLS]->(c:Company) RETURN c.ticker",
        {"u": _stub_uuid("DENIZ")})
    controlled = set()
    while res.has_next():
        controlled.add(res.get_next()[0])
    assert controlled == {"DNFIN", "DENFA"}


def test_stub_is_external_and_unlisted():
    conn = _graph()
    backfill_external_stubs(conn, reports_dir=_reports_dir(_ROWS))
    row = conn.execute(
        f"MATCH (c:Company {{uuid:$u}}) RETURN c.listing_status, c.is_listed",
        {"u": _stub_uuid("DENIZ")}).get_next()
    assert row[0] == STUB_STATUS
    assert row[1] is False
    # SUBSIDIARY_OF is mirrored
    n = conn.execute(
        f"MATCH (:Company {{ticker:'DNFIN'}})-[:SUBSIDIARY_OF]->(c:Company {{uuid:$u}}) "
        "RETURN count(*)", {"u": _stub_uuid("DENIZ")}).get_next()[0]
    assert n == 1


def test_non_destructive_and_idempotent():
    conn = _graph()
    rd = _reports_dir(_ROWS)
    # a pre-existing higher-grade control edge onto a Deniz SPV must be preserved
    conn.execute(
        f"MATCH (c:Company {{ticker:'DNFIN'}}) "
        f"CREATE (:Company {{uuid:$u, name:'pre', listing_status:$st}})"
        f"-[:CONTROLS {{source:'GLEIF-L2', confidence:0.95}}]->(c)",
        {"u": _stub_uuid("DENIZ"), "st": STUB_STATUS})
    stats = backfill_external_stubs(conn, reports_dir=rd)
    # DNFIN edge already existed -> corroborated, not re-created/overwritten
    row = conn.execute(
        f"MATCH (:Company {{uuid:$u}})-[r:CONTROLS]->(:Company {{ticker:'DNFIN'}}) "
        "RETURN r.source, r.confidence", {"u": _stub_uuid("DENIZ")}).get_next()
    assert row[0] == "GLEIF-L2"
    assert row[1] == 0.95
    backfill_external_stubs(conn, reports_dir=rd)  # idempotent
    n = conn.execute(
        f"MATCH (:Company {{uuid:$u}})-[r:CONTROLS]->(:Company {{ticker:'DENFA'}}) "
        "RETURN count(r)", {"u": _stub_uuid("DENIZ")}).get_next()[0]
    assert n == 1


def test_missing_spv_logged_not_crashed():
    conn = _graph()
    rows = _ROWS + [{"spv": "NOPE", "brand": "GHOST", "name": "GHOST A.Ş."}]
    stats = backfill_external_stubs(conn, reports_dir=_reports_dir(rows))
    # the GHOST stub is created but its SPV isn't in the graph -> logged
    assert stats["spv_not_in_graph"] == 1

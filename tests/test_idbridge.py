"""Id-bridge round-trip + refuse-on-ambiguity (M0 T4).

The bridge is a single point of failure (CLAUDE.md §5): the round-trip test proves
ticker ↔ ISIN ↔ kap_oid ↔ LEI is consistent on the golden universe anchors, and
the ambiguity test proves the bridge REFUSES (and logs) rather than guess.
"""
from __future__ import annotations

import json
import pathlib

import kuzu
import pytest

from tmkg.graph.connection import connect
from tmkg.pit import IdBridge, IdentityAmbiguous

ANCHORS = ["EREGL", "ASELS", "KCHOL", "GARAN"]
GRAPH = pathlib.Path("data/tmkg.kuzu")


def _graph_or_skip():
    if not GRAPH.exists():
        pytest.skip("v1 graph data/tmkg.kuzu not present")
    try:
        return connect()
    except Exception as e:  # pragma: no cover - lock/permission edge
        pytest.skip(f"Kuzu graph unopenable: {e}")


def test_anchor_round_trips_consistent():
    con = _graph_or_skip()
    bridge = IdBridge(con)
    for tk in ANCHORS:
        rec = bridge.round_trip(tk)  # raises if any leg disagrees
        assert rec["ticker"] == tk
        assert rec["isin"] and rec["isin"].startswith("TR")
        assert rec["lei"] and len(rec["lei"]) == 20
        assert rec["kap_oid"]


def test_cross_leg_resolution():
    con = _graph_or_skip()
    bridge = IdBridge(con)
    rec = bridge.round_trip("EREGL")
    # resolving by ISIN (not ticker) yields the same company
    by_isin = bridge.resolve(rec["isin"], field="isin")
    assert by_isin is not None and by_isin["uuid"] == rec["uuid"]
    assert by_isin["ticker"] == "EREGL"


def test_unknown_identifier_returns_none():
    con = _graph_or_skip()
    assert IdBridge(con).resolve("NOSUCHTICKER", field="ticker") is None


def _temp_company_graph(tmp_path, rows):
    db = kuzu.Database(str(tmp_path / "g.kuzu"))
    con = kuzu.Connection(db)
    con.execute(
        "CREATE NODE TABLE Company(uuid STRING, ticker STRING, isin STRING, "
        "kap_oid STRING, lei STRING, name STRING, PRIMARY KEY(uuid))"
    )
    for r in rows:
        con.execute(
            "CREATE (:Company {uuid:$uuid, ticker:$ticker, isin:$isin, "
            "kap_oid:$kap_oid, lei:$lei, name:$name})",
            r,
        )
    return con


def test_ambiguous_isin_is_refused_and_logged(tmp_path, monkeypatch):
    # two distinct companies sharing one ISIN -> the bridge must refuse, not guess
    import tmkg.config as config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    con = _temp_company_graph(
        tmp_path,
        [
            {"uuid": "u1", "ticker": "AAA", "isin": "TRDUP00000", "kap_oid": "o1", "lei": "L1", "name": "A"},
            {"uuid": "u2", "ticker": "BBB", "isin": "TRDUP00000", "kap_oid": "o2", "lei": "L2", "name": "B"},
        ],
    )
    bridge = IdBridge(con)
    with pytest.raises(IdentityAmbiguous):
        bridge.resolve("TRDUP00000", field="isin")

    report = bridge.flush_report()
    logged = json.loads(report.read_text())
    assert logged["refused_count"] == 1
    assert set(logged["refused"][0]["candidates"]) == {"AAA", "BBB"}

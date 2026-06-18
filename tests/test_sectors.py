"""Sector taxonomy tests — adapter validation, loader, and live-reference checks.

    PYTHONPATH=src python -m pytest tests/test_sectors.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.sector_adapter import SectorAdapter, DEFAULT_REFERENCE_PATH
from tmkg.loaders.sector_backfill import (
    backfill, inherit_sectors, sector_coverage,
)

# A tiny, self-contained two-level taxonomy for offline unit tests.
_MINI = {
    "source": "unit-test", "fetched_iso": "2026-06-07", "schema_version": 1,
    "sectors": [
        {"code": "MFG", "name": "Manufacturing", "level": 1, "parent": None},
        {"code": "CHEM", "name": "Chemicals", "level": 2, "parent": "MFG"},
        {"code": "FIN", "name": "Financials", "level": 1, "parent": None},
        {"code": "BANK", "name": "Banks", "level": 2, "parent": "FIN"},
    ],
    "memberships": {"TUPRS": "CHEM", "ALT": "CHEM", "GARAN": "BANK"},
}


def _write_ref(blob: dict) -> Path:
    p = Path(tempfile.mkdtemp()) / "sectors.json"
    p.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    return p


# --- adapter ---------------------------------------------------------------

def test_adapter_lookup_and_rollup():
    a = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    lk = a.lookup("tuprs")                      # case-insensitive
    assert lk.found and lk.leaf == "CHEM" and lk.main == "MFG"
    assert a.lookup("GARAN").main == "FIN"
    assert a.lookup("UNKNOWN").found is False


def test_adapter_rejects_dangling_parent():
    bad = json.loads(json.dumps(_MINI))
    bad["sectors"][1]["parent"] = "NOPE"
    with pytest.raises(ValueError):
        SectorAdapter(_write_ref(bad)).load(strict=True)


def test_adapter_rejects_membership_to_nonleaf():
    bad = json.loads(json.dumps(_MINI))
    bad["memberships"]["TUPRS"] = "MFG"        # main sector, not a leaf
    with pytest.raises(ValueError):
        SectorAdapter(_write_ref(bad)).load(strict=True)


def test_missing_reference_is_empty_not_error():
    a = SectorAdapter(Path(tempfile.mkdtemp()) / "absent.json").load()
    assert len(a) == 0


# --- loader against a temp graph ------------------------------------------

def _graph_with_companies(tickers):
    conn = connect(Path(tempfile.mkdtemp()) / "tmkg.kuzu")
    apply_schema(conn)
    for i, t in enumerate(tickers):
        conn.execute("CREATE (c:Company {uuid:$u, ticker:$t})",
                     {"u": f"co-{i}", "t": t})
    return conn


def test_backfill_links_leaf_and_hierarchy():
    conn = _graph_with_companies(["TUPRS", "GARAN", "NOSEC"])
    adapter = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    rep = backfill(conn, adapter, write_report=False)

    assert rep["sector_nodes"] == 4
    assert rep["subsector_edges"] == 2
    assert rep["companies_linked"] == 2        # TUPRS, GARAN; NOSEC unmatched
    assert rep["companies_unmatched"] == 1

    # company -> leaf -> main resolves
    res = conn.execute(
        "MATCH (c:Company {ticker:'TUPRS'})-[:IN_SECTOR]->(l)-[:SUBSECTOR_OF]->(m) "
        "RETURN l.code, m.code")
    assert res.get_next() == ["CHEM", "MFG"]


def test_backfill_is_idempotent():
    conn = _graph_with_companies(["TUPRS", "GARAN"])
    adapter = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    backfill(conn, adapter, write_report=False)
    backfill(conn, adapter, write_report=False)
    n = conn.execute("MATCH (:Company)-[r:IN_SECTOR]->() RETURN count(r)").get_next()[0]
    assert n == 2                              # no duplicate edges


# --- sector inheritance over CONTROLS (F8) ---------------------------------

def _co(conn, uuid, ticker=None, name=None, status=None):
    conn.execute(
        "CREATE (c:Company {uuid:$u, ticker:$t, name:$n, listing_status:$s})",
        {"u": uuid, "t": ticker, "n": name, "s": status})


def _controls(conn, parent, child):
    conn.execute(
        "MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c}) "
        "MERGE (p)-[:CONTROLS]->(c)", {"p": parent, "c": child})


def test_inherit_sector_from_nearest_parent():
    conn = connect(Path(tempfile.mkdtemp()) / "tmkg.kuzu")
    apply_schema(conn)
    adapter = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    # GARAN is KAP-sectored (BANK); its unsectored SPV child + grandchild inherit.
    _co(conn, "co-garan", "GARAN", "GARANTİ")
    _co(conn, "co-spv", "GRNSPV", "GARANTİ FİNANSAL KİRALAMA",
        status="NON_EQUITY_ISSUER")
    _co(conn, "co-gc", "GRNGC", "GARANTİ FAKTORİNG", status="NON_EQUITY_ISSUER")
    backfill(conn, adapter, write_report=False)          # links GARAN -> BANK
    _controls(conn, "co-garan", "co-spv")
    _controls(conn, "co-spv", "co-gc")                   # two hops down

    rep = inherit_sectors(conn)
    assert rep["inherited"] == 2                          # spv + grandchild
    # both inherit GARAN's leaf, basis-stamped, traceable to GARAN (not chained)
    r = conn.execute(
        "MATCH (c:Company {ticker:'GRNGC'})-[e:IN_SECTOR]->(s:Sector) "
        "RETURN s.code, e.sector_basis")
    code, basis = r.get_next()
    assert code == "BANK" and basis == "inherited-from-parent"
    assert all(w["from_parent"] == "co-garan" for w in rep["inherited_edges"])


def test_inheritance_never_overwrites_kap_or_external():
    conn = connect(Path(tempfile.mkdtemp()) / "tmkg.kuzu")
    apply_schema(conn)
    adapter = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    _co(conn, "co-garan", "GARAN", "GARANTİ")
    _co(conn, "co-tuprs", "TUPRS", "TÜPRAŞ")              # KAP-sectored to CHEM
    _co(conn, "co-stub", name="DENIZ stub", status="EXTERNAL_STUB")
    backfill(conn, adapter, write_report=False)
    _controls(conn, "co-garan", "co-tuprs")              # bank "controls" TUPRS
    _controls(conn, "co-garan", "co-stub")               # and an external stub

    rep = inherit_sectors(conn)
    # TUPRS keeps its KAP sector (not overwritten to BANK); stub gets nothing
    r = conn.execute("MATCH (:Company {ticker:'TUPRS'})-[e:IN_SECTOR]->(s) "
                     "RETURN s.code, e.sector_basis")
    assert r.get_next() == ["CHEM", "kap-direct"]
    n = conn.execute("MATCH (:Company {uuid:'co-stub'})-[:IN_SECTOR]->() "
                     "RETURN count(*)").get_next()[0]
    assert n == 0
    assert rep["inherited"] == 0


def test_sector_coverage_company_and_instrument_weighted():
    conn = connect(Path(tempfile.mkdtemp()) / "tmkg.kuzu")
    apply_schema(conn)
    adapter = SectorAdapter(_write_ref(_MINI)).load(strict=True)
    # GARAN sectored; SPV unsectored but issues most of the debt -> the two
    # coverage figures diverge (the audit's "classified co's, unclassified paper").
    _co(conn, "co-garan", "GARAN", "GARANTİ")
    _co(conn, "co-spv", "GRNSPV", "GARANTİ SPV", status="NON_EQUITY_ISSUER")
    backfill(conn, adapter, write_report=False)
    for i, owner in enumerate(["co-garan"] + ["co-spv"] * 3):
        conn.execute("CREATE (s:Security {uuid:$u})", {"u": f"sec-{i}"})
        conn.execute("MATCH (c:Company {uuid:$o}), (s:Security {uuid:$u}) "
                     "MERGE (c)-[:ISSUES]->(s)", {"o": owner, "u": f"sec-{i}"})
    cov = sector_coverage(conn)
    assert cov["company_weighted_coverage"] == 0.5        # 1 of 2 companies
    assert cov["instrument_weighted_coverage"] == 0.25    # 1 of 4 instruments


# --- live reference file (auto-skips if not yet generated) -----------------

@pytest.mark.skipif(not DEFAULT_REFERENCE_PATH.exists(),
                    reason="sectors.json not generated yet")
def test_live_reference_integrity_and_anchors():
    info = SectorAdapter().smoke_check()
    assert info["main"] == 16 and info["sub"] == 57
    assert info["anchors_checked"]["GARAN"] == "MALI_KURULUSLAR"
    assert info["anchors_checked"]["TUPRS"] == "IMALAT"

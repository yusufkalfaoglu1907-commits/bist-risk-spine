"""CONTROLS-graph integrity: cycle detection + SCC-aware rooting (F6).

Offline. Builds tiny synthetic graphs in a temp Kuzu DB to exercise the
post-load cycle guard and the cycle-safe / SUBSIDIARY_OF-aware group rooting.

    PYTHONPATH=src python -m pytest tests/test_integrity.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.schema.integrity import (
    find_controls_cycles, check_no_controls_cycles, ControlsCycleError,
    strongly_connected_components,
)
from tmkg.analytics.blast_radius import resolve_group_root, group_members


def _db():
    conn = connect(Path(tempfile.mkdtemp()) / "integ.kuzu")
    apply_schema(conn)
    return conn


def _co(conn, u):
    conn.execute("CREATE (:Company {uuid:$u, ticker:$u, name:$u, is_listed:true})",
                 {"u": u})


def _controls(conn, a, b):
    conn.execute(
        """MATCH (x:Company {uuid:$a}), (y:Company {uuid:$b})
           CREATE (x)-[:CONTROLS {basis:'test', confidence:1.0,
                    source:'test', extraction_method:'structured'}]->(y)""",
        {"a": a, "b": b})


def _subsidiary_of(conn, child, parent):
    conn.execute(
        """MATCH (c:Company {uuid:$c}), (p:Company {uuid:$p})
           CREATE (c)-[:SUBSIDIARY_OF {source:'kap', extraction_method:'structured',
                    confidence:1.0}]->(p)""",
        {"c": child, "p": parent})


# --- Tarjan SCC primitive --------------------------------------------------

def test_scc_separates_cycle_from_dag_nodes():
    # a<->b<->c cycle, d dangling off a
    comps = strongly_connected_components(
        {"a", "b", "c", "d"},
        {"a": ["b"], "b": ["c"], "c": ["a"], "d": []},
    )
    sizes = sorted(len(c) for c in comps)
    assert sizes == [1, 3]                       # {d} and {a,b,c}
    cycle = next(c for c in comps if len(c) == 3)
    assert set(cycle) == {"a", "b", "c"}


# --- cycle detection -------------------------------------------------------

def test_clean_dag_has_no_cycles():
    conn = _db()
    for u in ("kchol", "ykb", "ykr"):
        _co(conn, u)
    _controls(conn, "kchol", "ykb")
    _controls(conn, "ykb", "ykr")
    assert find_controls_cycles(conn) == []
    rep = check_no_controls_cycles(conn)
    assert rep["controls_cycles"] == 0


def test_injected_cycle_is_detected_and_fails_loud():
    """The audit's KOCFN->KCHOL injection: KCHOL controls down to KOCFN, then an
    erroneous KOCFN->KCHOL edge closes a cycle. Must be caught, not rooted."""
    conn = _db()
    for u in ("kchol", "kocfin", "kocfn"):
        _co(conn, u)
    _controls(conn, "kchol", "kocfin")
    _controls(conn, "kocfin", "kocfn")
    _controls(conn, "kocfn", "kchol")        # the injected back-edge -> cycle
    cycles = find_controls_cycles(conn)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"kchol", "kocfin", "kocfn"}
    with pytest.raises(ControlsCycleError):
        check_no_controls_cycles(conn)
    # non-raising mode still reports it
    assert check_no_controls_cycles(conn, raise_on_fail=False)["controls_cycles"] == 1


def test_self_loop_reported_as_cycle():
    conn = _db()
    _co(conn, "x")
    _controls(conn, "x", "x")
    assert find_controls_cycles(conn) == [["x"]]


# --- SCC-aware / SUBSIDIARY_OF-aware rooting -------------------------------

def test_rooting_survives_a_cycle_and_still_assembles_group():
    """With the injected cycle, rooting must NOT collapse to the seed; it returns
    an apex inside the cycle and the group still fans out to every member."""
    conn = _db()
    for u in ("kchol", "kocfin", "kocfn", "leaf"):
        _co(conn, u)
    _controls(conn, "kchol", "kocfin")
    _controls(conn, "kocfin", "kocfn")
    _controls(conn, "kocfn", "kchol")        # cycle among the three
    _controls(conn, "kchol", "leaf")         # a normal child off the apex
    roots = resolve_group_root(conn, "leaf")
    # apex is the nearest member of the cyclic top SCC (kchol, 1 hop up)
    assert roots and roots[0]["uuid"] == "kchol"
    assert roots[0]["hops_from_seed"] == 1
    members = {m["uuid"] for m in group_members(conn, roots[0]["uuid"])}
    assert members == {"kchol", "kocfin", "kocfn", "leaf"}


def test_rooting_follows_kap_only_subsidiary_of_edge():
    """A KAP-only SUBSIDIARY_OF edge (no mirror CONTROLS) must be honored when
    walking up — the F6 docstring/code divergence fix."""
    conn = _db()
    for u in ("parent", "child"):
        _co(conn, u)
    _subsidiary_of(conn, "child", "parent")   # only edge: child -> parent
    roots = resolve_group_root(conn, "child")
    assert [r["uuid"] for r in roots] == ["parent"]
    assert roots[0]["hops_from_seed"] == 1


def test_uncontrolled_seed_is_its_own_apex():
    conn = _db()
    _co(conn, "solo")
    roots = resolve_group_root(conn, "solo")
    assert [r["uuid"] for r in roots] == ["solo"]
    assert roots[0]["hops_from_seed"] == 0

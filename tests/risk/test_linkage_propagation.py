"""M8.2 linkage-graph propagation — multi-hop ownership reconciliation + §4 honesty + blast-radius.

Pure, deterministic known-answer checks: the propagated magnitude is exactly the product of stake
fractions along each path times the origin shock; cycles are recorded not looped; an out-of-range
fraction raises; an unmapped origin is surfaced; the CONTROLS blast-radius is correct reachability.
"""
from __future__ import annotations

import pytest

from tmkg.risk.linkage_propagation import (
    OwnershipEdge,
    control_blast_radius,
    propagate_ownership_shock,
)
from tmkg.risk.run_linkage import run_linkage_shock


def _e(holder, held, frac, conf=1.0):
    return OwnershipEdge(holder=holder, held=held, fraction=frac, confidence=conf)


# --- ownership look-through reconciliation ----------------------------------------------------

def test_single_hop_propagation():
    # A owns 40% of C; shock C -20% -> A exposed -8%
    res = propagate_ownership_shock([_e("A", "C", 0.40)], {"C": -0.20})
    assert res.per_holder["A"] == pytest.approx(-0.08)


def test_multi_hop_compounds_along_chain():
    # A owns 50% of B, B owns 40% of C; shock C -20%
    # B exposure = 0.40*-0.20 = -0.08 ; A exposure = 0.50*0.40*-0.20 = -0.04
    edges = [_e("A", "B", 0.50), _e("B", "C", 0.40)]
    res = propagate_ownership_shock(edges, {"C": -0.20})
    assert res.per_holder["B"] == pytest.approx(-0.08)
    assert res.per_holder["A"] == pytest.approx(-0.04)
    # the A←B←C path is recorded with the compounded fraction
    a_path = next(p for p in res.paths if p["holder"] == "A")
    assert a_path["path"] == ["C", "B", "A"]
    assert a_path["fraction_product"] == pytest.approx(0.20)
    assert a_path["hops"] == 2


def test_direct_and_indirect_sum():
    # A owns 30% of C directly AND 50% of B which owns 40% of C
    edges = [_e("A", "C", 0.30), _e("A", "B", 0.50), _e("B", "C", 0.40)]
    res = propagate_ownership_shock(edges, {"C": -0.20})
    # A total = 0.30*-0.20 + 0.50*0.40*-0.20 = -0.06 + -0.04 = -0.10
    assert res.per_holder["A"] == pytest.approx(-0.10)


def test_min_confidence_prunes_low_trust_edges():
    edges = [_e("A", "C", 0.40, conf=0.5)]
    res = propagate_ownership_shock(edges, {"C": -0.20}, min_confidence=0.8)
    assert "A" not in res.per_holder  # edge pruned -> no propagation


# --- §4 honesty -------------------------------------------------------------------------------

def test_out_of_range_fraction_raises():
    with pytest.raises(ValueError):
        propagate_ownership_shock([_e("A", "C", 40.0)], {"C": -0.2})  # 40, not 0.40


def test_unmapped_origin_is_surfaced():
    res = propagate_ownership_shock([_e("A", "C", 0.4)], {"Z": -0.2})  # Z in no edge
    assert "Z" in res.unmapped_origins
    assert not res.per_holder


def test_cycle_is_recorded_not_looped():
    # A holds B, B holds A (a cycle); must terminate and record it
    edges = [_e("A", "B", 0.5), _e("B", "A", 0.5)]
    res = propagate_ownership_shock(edges, {"A": -0.10}, max_hops=10)
    assert res.cycles  # recorded
    assert all(isinstance(p["contribution"], float) for p in res.paths)  # finite, terminated


# --- CONTROLS blast-radius --------------------------------------------------------------------

def test_blast_radius_reaches_controllers_and_controlled():
    # GP controls P controls S
    edges = [("GP", "P"), ("P", "S")]
    up = control_blast_radius(edges, "P", direction="up")
    assert up["controllers"] == {"GP": 1}
    down = control_blast_radius(edges, "P", direction="down")
    assert down["controlled"] == {"S": 1}
    both = control_blast_radius(edges, "P", direction="both")
    assert set(both["blast_radius"]) == {"GP", "S"}


# --- runner on injected edges (no graph) ------------------------------------------------------

def test_runner_injected():
    edges = [_e("KCHOL", "ARCLK", 0.4853, 0.85)]
    controls = [("KCHOL", "ARCLK")]
    rep = run_linkage_shock(shocks={"ARCLK": -0.20}, edges=edges, controls_edges=controls)
    assert rep["tool"] == "linkage_shock_propagation"
    # KCHOL exposed = 0.4853 * -0.20
    worst = rep["ownership"]["worst_exposed"]
    assert worst["KCHOL"] == pytest.approx(-0.09706)
    assert rep["control_blast_radius"]["ARCLK"]["controllers"] == {"KCHOL": 1}
    assert "verdict" not in rep  # risk tool, not a signal

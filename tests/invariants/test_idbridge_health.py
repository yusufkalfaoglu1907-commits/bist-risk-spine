"""Id-bridge health invariant (M8.3) — the §5 single-point-of-failure stays healthy.

The resolver refuses-rather-than-guesses on a single lookup (tests/test_idbridge.py); this guards the
*standing* health of the whole bridge so silent decay is caught: no id-leg may collapse below its
coverage floor, no id value may collide across companies (ambiguous identity), no ticker may fail to
round-trip. Skips cleanly when the v1 graph is absent (same stance as test_idbridge.py).
"""
from __future__ import annotations

import pathlib

import pytest

from tmkg.monitor.idbridge_health import DEFAULT_COVERAGE_FLOORS, idbridge_health

GRAPH = pathlib.Path("data/tmkg.kuzu")


def _con_or_skip():
    if not GRAPH.exists():
        pytest.skip("v1 graph data/tmkg.kuzu not present")
    try:
        from tmkg.graph.connection import connect
        return connect()
    except Exception as e:  # pragma: no cover - lock/permission edge
        pytest.skip(f"Kuzu graph unopenable: {e}")


@pytest.mark.invariant
def test_idbridge_health_passes_on_real_graph():
    con = _con_or_skip()
    report = idbridge_health(con)
    assert report["passes"], f"id-bridge health regressed: {report['failures']}"


@pytest.mark.invariant
def test_no_identity_collisions():
    con = _con_or_skip()
    report = idbridge_health(con, full_round_trip=False)
    assert report["collision_counts"] == {"isin": 0, "kap_oid": 0, "lei": 0}, (
        f"an id value is shared across companies — ambiguous identity: {report['collisions']}"
    )


@pytest.mark.invariant
def test_coverage_meets_floors():
    con = _con_or_skip()
    report = idbridge_health(con, full_round_trip=False)
    for leg, floor in DEFAULT_COVERAGE_FLOORS.items():
        assert report["coverage"][leg] >= floor, (
            f"{leg} id coverage {report['coverage'][leg]} dropped below floor {floor}"
        )

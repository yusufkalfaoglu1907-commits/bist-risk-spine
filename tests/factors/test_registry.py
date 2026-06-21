"""The canonical core factor-set registry (tmkg.factors.registry).

Pins the manifest's well-formedness and the two derivations the engine consumes
(``specs`` / ``ladder_order``), plus the §5 guarantees: the foreign-flow leg is present
in the design intent but correctly *blocked* (surfaced, not silently dropped) until the
Matriks custodian list returns, and the strip order follows the §200 rung order over
**real factor names** — the case the old role-keyed default got wrong.
"""
from __future__ import annotations

from tmkg.factors import registry
from tmkg.factors.neutralize import DEFAULT_LADDER
from tmkg.factors.series import DIFF, SIMPLE


def test_manifest_is_well_formed():
    registry.validate()  # raises on a malformed manifest


def test_every_ladder_rung_has_a_factor():
    roles = {f.role for f in registry.CORE_FACTORS}
    assert roles == set(DEFAULT_LADDER)  # nothing in the ladder is unrepresented


def test_specs_excludes_blocked_factors_by_default():
    s = registry.specs()
    assert "FFLOW" not in s  # foreign-flow is blocked on Matriks Q1 -> not demanded yet
    # but every *available* factor is mapped to a return method
    assert s["XU100"] == SIMPLE and s["VIX"] == DIFF and s["TRY2Y"] == DIFF


def test_specs_full_set_includes_the_blocked_foreign_flow():
    s = registry.specs(available_only=False)
    assert "FFLOW" in s  # the design intent is complete; it is owed, not forgotten


def test_blocked_factors_are_surfaced_not_dropped():
    blocked = {f.name for f in registry.blocked_factors()}
    assert blocked == {"FFLOW"}  # exactly the foreign-flow leg, today


def test_ladder_order_is_rung_ordered_over_real_names():
    order = registry.ladder_order()  # available only
    pos = {f.name: f.role for f in registry.CORE_FACTORS}
    rung_index = {r: i for i, r in enumerate(DEFAULT_LADDER)}
    seen = [rung_index[pos[name]] for name in order]
    assert seen == sorted(seen)  # non-decreasing rung position == ladder order
    # market rung comes first, holding last; fx precedes energy precedes sector
    assert order[0] in {"XU100", "MSCIEM", "VIX"}
    assert order.index("USDTRY") < order.index("BRENT") < order.index("XBANK")
    assert "FFLOW" not in order  # blocked leg absent from an available-only run


def test_order_present_sorts_arbitrary_landed_names_into_rung_order():
    # a deliberately scrambled set of names that actually "landed" in L2
    present = ["XBANK", "USDTRY", "BRENT", "XU100", "VIX"]
    ordered = registry.order_present(present)
    rung_index = {r: i for i, r in enumerate(DEFAULT_LADDER)}
    by_name = {f.name: f for f in registry.CORE_FACTORS}
    seen = [rung_index[by_name[n].role] for n in ordered]
    assert seen == sorted(seen)
    # market names (XU100, VIX) precede fx (USDTRY) precede energy (BRENT) precede sector (XBANK)
    assert ordered.index("XU100") < ordered.index("USDTRY") < ordered.index("BRENT")
    assert ordered.index("BRENT") < ordered.index("XBANK")


def test_order_present_keeps_unknown_names_after_known_rungs_never_silently_first():
    # an unrecognised vendor-renamed series must still be stripped, but never reordered
    # ahead of a known rung (it sorts after all known rungs, first-seen among unknowns).
    present = ["MYSTERY", "USDTRY", "XU100"]
    ordered = registry.order_present(present)
    assert ordered.index("XU100") < ordered.index("USDTRY") < ordered.index("MYSTERY")

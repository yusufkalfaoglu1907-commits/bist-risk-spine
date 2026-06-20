"""accounting_regime invariant (CLAUDE.md §5, design §3).

The golden check passes today; the no-straddle engine guard is unskipped in M1.
"""
from __future__ import annotations

import pytest

from tmkg.pit.errors import RegimeStraddle
from tmkg.returns import guarded_growth, regime_for_period, same_regime


@pytest.mark.invariant
@pytest.mark.golden
def test_golden_shows_basis_divergence(load_golden):
    g = load_golden("accounting_regime_KCHOL_202412.json")
    adj = g["adjusted_ias29"]["revenue"]
    un = g["unadjusted_nominal"]["revenue"]
    # FY2024 is in the ias29_2023_2024 regime: the two bases must differ
    # materially. This is why a figure may never straddle a regime switch
    # without converting to a common basis — the regime SELECTS the basis.
    assert adj != un
    assert adj / un > 1.2  # ~1.31 in the captured sample


@pytest.mark.invariant
def test_regime_state_machine_maps_both_switches():
    assert regime_for_period("202212") == "nominal_pre2023"
    assert regime_for_period("202312") == "ias29_2023_2024"
    assert regime_for_period("202412") == "ias29_2023_2024"
    assert regime_for_period("202503") == "suspended_2025_2027"
    assert not same_regime("202212", "202312")  # FY2023 switch
    assert not same_regime("202412", "202503")  # FY2025 switch
    assert same_regime("202312", "202412")      # within IAS-29


@pytest.mark.invariant
def test_refuses_cross_regime_growth():
    """A growth/intensity/materiality calc spanning the FY2023 or FY2025 switch
    must raise unless both inputs are converted to a common basis."""
    # FY2022 (nominal) -> FY2023 (IAS-29): straddles the first switch -> refuse
    with pytest.raises(RegimeStraddle):
        guarded_growth(2000.0, "202312", 1000.0, "202212")
    # FY2024 (IAS-29) -> FY2025 (suspended): straddles the second switch -> refuse
    with pytest.raises(RegimeStraddle):
        guarded_growth(2000.0, "202503", 1000.0, "202412")
    # within one regime: allowed
    assert guarded_growth(1100.0, "202412", 1000.0, "202312") == pytest.approx(0.10)
    # across a switch but on a declared common basis: allowed
    assert guarded_growth(
        1100.0, "202503", 1000.0, "202412", basis_converted=True
    ) == pytest.approx(0.10)

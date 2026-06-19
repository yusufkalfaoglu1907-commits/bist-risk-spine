"""accounting_regime invariant (CLAUDE.md §5, design §3).

The golden check passes today; the no-straddle engine guard is unskipped in M1.
"""
from __future__ import annotations

import pytest


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
@pytest.mark.skip(reason="M1: implement the no-straddle guard in tmkg.returns.accounting_regime")
def test_refuses_cross_regime_growth():
    """A growth/intensity/materiality calc spanning the FY2023 or FY2025 switch
    must raise unless both inputs are converted to a common basis."""
    ...

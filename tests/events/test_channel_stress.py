"""M6 channel-stress re-pricing — hand-checked reconciliation (BUILD_PLAN.md M6 exit gate).

"stress P&L reconciles against a hand-checked shock." This is re-pricing, not statistics, so the
decisive test is arithmetic pinned to the penny — plus the §4 honesty rails: NaN exposure is not
fabricated to 0-impact-but-full-coverage, an unmodelled shock channel is surfaced not dropped,
and an empty channel intersection fails loud instead of returning a resilient-looking zero.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tmkg.events.channel_stress import channel_stress_pnl, shock_from_prior

# 3 names x 2 channels (fx, energy) — signed betas to each channel's factor.
EXPOSURES = pd.DataFrame(
    {"fx": [1.5, -0.5, 0.0], "energy": [0.0, 2.0, -1.0]},
    index=["AAA", "BBB", "CCC"],
)
SHOCK = {"fx": 0.10, "energy": 0.20}  # 10% TRY depreciation, oil +20%


def test_per_name_reconciles_to_hand_arithmetic():
    res = channel_stress_pnl(EXPOSURES, SHOCK)
    # AAA = 1.5*.10 + 0*.20 = .15 ; BBB = -.5*.10 + 2*.20 = .35 ; CCC = 0 + -1*.20 = -.20
    assert res.per_name["AAA"] == pytest.approx(0.15)
    assert res.per_name["BBB"] == pytest.approx(0.35)
    assert res.per_name["CCC"] == pytest.approx(-0.20)
    assert set(res.shocked_channels) == {"fx", "energy"}
    assert res.unmodelled_channels == ()


def test_worst_and_best_exposed():
    res = channel_stress_pnl(EXPOSURES, SHOCK)
    assert res.worst_exposed(1).index.tolist() == ["CCC"]
    assert res.best_exposed(1).index.tolist() == ["BBB"]


def test_portfolio_pnl_reconciles():
    weights = pd.Series({"AAA": 0.5, "BBB": 0.5, "CCC": -1.0})
    res = channel_stress_pnl(EXPOSURES, SHOCK, weights=weights)
    # .5*.15 + .5*.35 - 1*(-.20) = .075 + .175 + .20 = .45
    assert res.portfolio_pnl == pytest.approx(0.45)


def test_nan_exposure_contributes_zero_and_lowers_coverage():
    exp = EXPOSURES.copy()
    exp["rates_cds"] = [np.nan, 1.0, 2.0]  # AAA has no rates_cds beta
    res = channel_stress_pnl(exp, {"fx": 0.10, "energy": 0.20, "rates_cds": 0.05})
    # AAA: NaN rates term drops -> 1.5*.10 = .15 (not fabricated to 0-impact full coverage)
    assert res.per_name["AAA"] == pytest.approx(0.15)
    assert res.coverage["AAA"] == pytest.approx(2 / 3)  # known on fx,energy ; unknown rates
    assert res.coverage["BBB"] == pytest.approx(1.0)


def test_unmodelled_channel_is_surfaced_not_dropped_silently():
    # 'market' is a valid channel but absent from EXPOSURES columns -> recorded, not applied.
    res = channel_stress_pnl(EXPOSURES, {"fx": 0.10, "market": -0.30})
    assert res.unmodelled_channels == ("market",)
    assert set(res.shocked_channels) == {"fx"}
    assert res.per_name["AAA"] == pytest.approx(0.15)  # only fx applied


def test_empty_intersection_raises():
    # only 'holding' shocked but no holding column -> refuse a fabricated zero stress.
    with pytest.raises(ValueError):
        channel_stress_pnl(EXPOSURES, {"holding": 0.10})


def test_unknown_channel_and_empty_shock_raise():
    with pytest.raises(ValueError):
        channel_stress_pnl(EXPOSURES, {"not_a_channel": 0.1})
    with pytest.raises(ValueError):
        channel_stress_pnl(EXPOSURES, {})


def test_shock_from_prior_signs_and_scaling():
    s = shock_from_prior("energy_supply_disruption", severity=0.5)
    assert s["energy"] == pytest.approx(0.5)    # prior +1 * 0.5
    assert s["market"] == pytest.approx(-0.5)   # prior -1 * 0.5
    # and it flows straight into re-pricing
    res = channel_stress_pnl(EXPOSURES, {k: v for k, v in s.items() if k in EXPOSURES.columns})
    assert res.per_name["CCC"] == pytest.approx(-1.0 * 0.5)  # CCC energy beta -1 * shock .5

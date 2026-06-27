"""M8.1 scenario re-pricing — reconciliation + honesty invariants (the risk-tool exit gate).

A risk re-pricing has no Sharpe to judge; its definition-of-done is a **hand-checked reconciliation
to the penny** plus the §4 honesty rules (coverage surfaced, unmodelled channels never zero-filled,
empty intersection raises). These tests pin all of that, plus the library well-formedness and the
empirical-shock arithmetic.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.events.taxonomy import CHANNELS
from tmkg.risk.repricing import (
    latest_exposure_tensor,
    realized_channel_shock,
    reprice_scenario,
    reprice_suite,
)
from tmkg.risk.scenarios import (
    FRACTIONAL_CHANNELS,
    Scenario,
    scenario_from_factor_returns,
    stylized_library,
)
from tmkg.risk.run_scenarios import run_scenario_analysis


# --- reconciliation: the re-pricing is exactly Σ beta·shock -----------------------------------

def test_reprice_reconciles_to_the_penny():
    # two names, known betas to market and fx; a known two-channel shock.
    exposures = pd.DataFrame(
        {"market": {"AAA": 1.20, "BBB": 0.50}, "fx": {"AAA": -0.80, "BBB": +0.30}}
    )
    sc = Scenario("t", "test", {"market": -0.10, "fx": +0.05})
    res = reprice_scenario(exposures, sc)
    # AAA: 1.20*-0.10 + -0.80*0.05 = -0.12 - 0.04 = -0.16 ; BBB: 0.50*-0.10 + 0.30*0.05 = -0.035
    assert res.stress.per_name["AAA"] == pytest.approx(-0.16)
    assert res.stress.per_name["BBB"] == pytest.approx(-0.035)
    # worst-exposed ordering is by most-negative
    assert res.stress.worst_exposed(1).index[0] == "AAA"


def test_portfolio_pnl_is_weighted_sum():
    exposures = pd.DataFrame({"market": {"AAA": 1.0, "BBB": -1.0}})
    sc = Scenario("t", "test", {"market": -0.10})
    weights = pd.Series({"AAA": 0.6, "BBB": 0.4})
    res = reprice_scenario(exposures, sc, weights=weights)
    # per_name: AAA -0.10, BBB +0.10 ; pnl = 0.6*-0.10 + 0.4*0.10 = -0.02
    assert res.stress.portfolio_pnl == pytest.approx(-0.02)


# --- §4 honesty: coverage, unmodelled channels, loud-fail -------------------------------------

def test_unmodelled_channel_is_surfaced_not_invented():
    exposures = pd.DataFrame({"market": {"AAA": 1.0}})  # no 'energy' column
    sc = Scenario("t", "test", {"market": -0.10, "energy": +0.20})
    res = reprice_scenario(exposures, sc)
    assert "energy" in res.stress.unmodelled_channels
    assert res.stress.shocked_channels == ("market",)
    assert res.stress.per_name["AAA"] == pytest.approx(-0.10)  # energy contributes nothing


def test_nan_exposure_contributes_zero_and_lowers_coverage():
    exposures = pd.DataFrame({"market": {"AAA": 1.0}, "fx": {"AAA": np.nan}})
    sc = Scenario("t", "test", {"market": -0.10, "fx": +0.05})
    res = reprice_scenario(exposures, sc)
    assert res.stress.per_name["AAA"] == pytest.approx(-0.10)   # NaN fx -> no fx impact
    assert res.stress.coverage["AAA"] == pytest.approx(0.5)     # 1 of 2 shocked channels known


def test_all_unmodelled_raises_rather_than_zero_stress():
    exposures = pd.DataFrame({"market": {"AAA": 1.0}})
    sc = Scenario("t", "test", {"energy": +0.20})  # no energy column at all
    with pytest.raises(ValueError):
        reprice_scenario(exposures, sc)


def test_suite_skips_unshockable_scenario_instead_of_returning_zero():
    exposures = pd.DataFrame({"market": {"AAA": 1.0}})
    lib = {"ok": Scenario("ok", "", {"market": -0.1}),
           "dead": Scenario("dead", "", {"energy": +0.2})}
    out = reprice_suite(exposures, lib)
    assert "ok" in out and "dead" not in out


# --- scenario library well-formedness ---------------------------------------------------------

def test_stylized_library_is_well_formed():
    lib = stylized_library()
    assert lib  # non-empty
    for name, sc in lib.items():
        assert sc.name == name
        assert sc.tier == "stylized"
        assert sc.shocks
        for ch, v in sc.shocks.items():
            assert ch in CHANNELS
            assert np.isfinite(v)
            # stylized shocks stay on the unit-homogeneous fractional channels (units note, §4)
            assert ch in FRACTIONAL_CHANNELS


def test_scenario_rejects_bad_channel_and_nonfinite():
    with pytest.raises(ValueError):
        Scenario("x", "", {"not_a_channel": 0.1})
    with pytest.raises(ValueError):
        Scenario("x", "", {"market": float("nan")})
    with pytest.raises(ValueError):
        Scenario("x", "", {})  # empty shock


# --- exposure tensor + empirical shock from real factor levels --------------------------------

def test_latest_exposure_tensor_takes_most_recent_beta_per_symbol():
    betas = pd.DataFrame([
        {"symbol": "AAA", "factor": "XU100", "bar_date": dt.date(2025, 1, 1), "beta": 0.9},
        {"symbol": "AAA", "factor": "XU100", "bar_date": dt.date(2025, 2, 1), "beta": 1.1},  # newer
        {"symbol": "AAA", "factor": "USDTRY", "bar_date": dt.date(2025, 2, 1), "beta": -0.5},
        {"symbol": "BBB", "factor": "XU100", "bar_date": dt.date(2025, 2, 1), "beta": 0.7},
    ])
    t = latest_exposure_tensor(betas)
    assert t.loc["AAA", "market"] == pytest.approx(1.1)   # latest, not 0.9
    assert t.loc["AAA", "fx"] == pytest.approx(-0.5)
    assert set(t.columns) <= {"market", "fx"}             # only channels with data appear


def test_realized_channel_shock_respects_method_units():
    factors = pd.DataFrame([
        # XU100 (simple): 100 -> 90  => -10%
        {"factor": "XU100", "bar_date": dt.date(2025, 3, 18), "value": 100.0},
        {"factor": "XU100", "bar_date": dt.date(2025, 3, 25), "value": 90.0},
        # TRCDS5Y (diff): 300 -> 360 => +60 (level change, not a %)
        {"factor": "TRCDS5Y", "bar_date": dt.date(2025, 3, 18), "value": 300.0},
        {"factor": "TRCDS5Y", "bar_date": dt.date(2025, 3, 25), "value": 360.0},
    ])
    shock = realized_channel_shock(
        factors, start=dt.date(2025, 3, 18), end=dt.date(2025, 3, 25),
        methods={"XU100": "simple", "TRCDS5Y": "diff"})
    assert shock["market"] == pytest.approx(-0.10)
    assert shock["rates_cds"] == pytest.approx(60.0)
    sc = scenario_from_factor_returns("ep", shock)
    assert sc.tier == "empirical"


# --- end-to-end runner on an injected synthetic world (no L2, no network) ---------------------

def test_runner_end_to_end_injected():
    betas = pd.DataFrame([
        {"symbol": "AAA", "factor": "XU100", "bar_date": dt.date(2025, 2, 1), "beta": 1.2},
        {"symbol": "AAA", "factor": "USDTRY", "bar_date": dt.date(2025, 2, 1), "beta": -0.8},
        {"symbol": "BBB", "factor": "XU100", "bar_date": dt.date(2025, 2, 1), "beta": 0.5},
        {"symbol": "BBB", "factor": "USDTRY", "bar_date": dt.date(2025, 2, 1), "beta": +0.3},
    ])
    factors = pd.DataFrame([
        {"factor": "XU100", "bar_date": dt.date(2025, 3, 18), "value": 100.0},
        {"factor": "XU100", "bar_date": dt.date(2025, 3, 25), "value": 88.0},
        {"factor": "USDTRY", "bar_date": dt.date(2025, 3, 18), "value": 36.0},
        {"factor": "USDTRY", "bar_date": dt.date(2025, 3, 25), "value": 39.6},
    ])
    rep = run_scenario_analysis(
        as_of=dt.date(2025, 4, 1), inputs=(betas, factors),
        empirical_window=(dt.date(2025, 3, 18), dt.date(2025, 3, 25)),
        empirical_name="imamoglu_real")
    assert rep["tool"] == "scenario_repricing"
    assert rep["exposure_tensor"]["n_names"] == 2
    names = {s["scenario"] for s in rep["scenarios"]}
    assert "imamoglu_real" in names            # empirical scenario priced
    assert "try_depreciation_10" in names      # stylized library priced
    # the empirical shock = real moves: market 88/100-1 = -12%, fx 39.6/36-1 = +10%
    emp = rep["empirical"]["shock"]
    assert emp["market"] == pytest.approx(-0.12)
    assert emp["fx"] == pytest.approx(+0.10)
    # no registry / signal artifacts — this is a risk tool
    assert "verdict" not in rep and "signal_registry" not in rep

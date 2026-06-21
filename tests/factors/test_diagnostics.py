"""M2 exit-gate evaluators (tmkg.factors.diagnostics) — synthetic, deterministic.

Pins the two measurements the gate needs over a real fit: explained-variance share per
universe_class, and the across-2025-shock beta break measured against the within-regime
noise floor. Build-the-judge-before-the-data, so these are exact known-answer checks.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tmkg.factors.diagnostics import (
    assess_regime_break,
    factor_variance_share,
    variance_share_by_class,
)


def _dates(n):
    return [d.date() for d in pd.bdate_range("2024-01-02", periods=n)]


# === variance share ========================================================
def test_variance_share_matches_definition_and_bounds():
    n = 60
    dates = _dates(n)
    rng = np.random.default_rng(0)
    # FULLY explained name: residual ~ 0 -> r2 ~ 1
    ret_full = rng.normal(0, 0.02, n)
    res_full = np.zeros(n)
    # UNEXPLAINED name: residual == return -> r2 ~ 0
    ret_none = rng.normal(0, 0.02, n)
    res_none = ret_none.copy()

    returns = pd.concat([
        pd.DataFrame({"symbol": "FULL", "bar_date": dates, "ret": ret_full}),
        pd.DataFrame({"symbol": "NONE", "bar_date": dates, "ret": ret_none}),
    ], ignore_index=True)
    residuals = pd.concat([
        pd.DataFrame({"symbol": "FULL", "bar_date": dates, "residual": res_full,
                      "universe_class": "operating"}),
        pd.DataFrame({"symbol": "NONE", "bar_date": dates, "residual": res_none,
                      "universe_class": "operating"}),
    ], ignore_index=True)

    out = factor_variance_share(returns, residuals).set_index("symbol")
    assert out.loc["FULL", "r2"] == pytest.approx(1.0, abs=1e-12)
    assert out.loc["NONE", "r2"] == pytest.approx(0.0, abs=1e-12)
    # and it equals the raw definition for an arbitrary split
    assert out.loc["FULL", "var_residual"] == pytest.approx(0.0, abs=1e-12)


def test_variance_share_skips_thin_and_zero_variance_names():
    dates = _dates(30)
    rng = np.random.default_rng(1)
    returns = pd.concat([
        pd.DataFrame({"symbol": "THIN", "bar_date": dates[:10],
                      "ret": rng.normal(0, 0.02, 10)}),          # < min_obs
        pd.DataFrame({"symbol": "FLAT", "bar_date": dates,
                      "ret": np.zeros(30)}),                      # zero variance
        pd.DataFrame({"symbol": "OK", "bar_date": dates,
                      "ret": rng.normal(0, 0.02, 30)}),
    ], ignore_index=True)
    residuals = returns.rename(columns={"ret": "residual"}).assign(universe_class="operating")
    out = factor_variance_share(returns, residuals, min_obs=20)
    assert set(out["symbol"]) == {"OK"}  # THIN and FLAT both refused, never fabricated


def test_variance_share_by_class_aggregates_per_universe_class():
    rows = pd.DataFrame({
        "symbol": ["a", "b", "c", "d"],
        "universe_class": ["operating", "operating", "holding", None],
        "n_obs": [40, 40, 40, 40],
        "var_ret": [1.0, 1.0, 1.0, 1.0],
        "var_residual": [0.4, 0.2, 0.7, 0.5],
        "r2": [0.6, 0.8, 0.3, 0.5],
    })
    by = variance_share_by_class(rows).set_index("universe_class")
    assert by.loc["operating", "n_names"] == 2
    assert by.loc["operating", "r2_median"] == pytest.approx(0.7)  # median(0.6, 0.8)
    assert by.loc["holding", "r2_median"] == pytest.approx(0.3)
    assert by.loc["(unclassified)", "n_names"] == 1  # NULL class surfaced, not dropped


# === regime break ==========================================================
def _betas_two_regimes(symbol, factor, before_vals, after_vals):
    n_b, n_a = len(before_vals), len(after_vals)
    return pd.DataFrame({
        "symbol": symbol, "factor": factor,
        "beta": list(before_vals) + list(after_vals),
        "regime": ["orthodox_turn_2023"] * n_b + ["imamoglu_shock_2025"] * n_a,
    })


def test_regime_break_high_for_a_factor_whose_beta_flips_low_for_a_stable_one():
    rng = np.random.default_rng(2)
    frames = []
    for s in range(8):
        # MKT: beta ~ +2.0 before, ~ -1.0 after (a real break), tiny within-regime wobble
        frames.append(_betas_two_regimes(
            f"S{s}", "MKT",
            2.0 + rng.normal(0, 0.02, 10), -1.0 + rng.normal(0, 0.02, 10)))
        # FX: beta ~ -0.5 both sides (stable), same wobble
        frames.append(_betas_two_regimes(
            f"S{s}", "FX",
            -0.5 + rng.normal(0, 0.02, 10), -0.5 + rng.normal(0, 0.02, 10)))
    betas = pd.concat(frames, ignore_index=True)

    out = assess_regime_break(betas).set_index("factor")
    assert out.loc["MKT", "median_shift"] == pytest.approx(3.0, abs=0.1)
    # the break dominates the noise floor for MKT, not for FX
    assert out.loc["MKT", "median_break_ratio"] > 20
    assert out.loc["FX", "median_break_ratio"] < 3
    assert out.loc["MKT", "n_names"] == 8


def test_regime_break_skips_names_thin_on_either_side():
    betas = _betas_two_regimes("ONE", "MKT",
                               np.full(3, 2.0), np.full(10, -1.0))  # only 3 before
    out = assess_regime_break(betas, min_per_regime=5)
    assert out.empty  # no break emitted from a one-sided window — never fabricated

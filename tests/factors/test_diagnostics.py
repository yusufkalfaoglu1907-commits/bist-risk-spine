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


def _betas_two_regimes_dated(symbol, factor, before_vals, after_vals):
    """Like ``_betas_two_regimes`` but with monotone bar_dates so peri-shock ordering works."""
    n_b, n_a = len(before_vals), len(after_vals)
    dates = _dates(n_b + n_a)
    return pd.DataFrame({
        "symbol": symbol, "factor": factor,
        "beta": list(before_vals) + list(after_vals),
        "regime": ["orthodox_turn_2023"] * n_b + ["imamoglu_shock_2025"] * n_a,
        "bar_date": dates,
    })


def test_peri_obs_isolates_a_local_break_from_long_regime_drift():
    """A long pre-shock regime that *drifts* inflates the full-regime noise floor and hides a
    clean boundary jump; the peri-shock window (betas nearest the boundary) recovers it."""
    rng = np.random.default_rng(5)
    frames = []
    for s in range(8):
        # before regime (60d): the first 45d swing widely (high global variance), the last
        # 15d are a calm pre-shock plateau ~1.0. after (20d): a clean local jump to ~1.4.
        swing = 1.0 + 0.7 * np.array([(-1) ** i for i in range(45)], dtype=float)
        plateau = 1.0 + rng.normal(0, 0.01, 15)
        before = np.r_[swing, plateau]
        after = 1.4 + rng.normal(0, 0.01, 20)
        frames.append(_betas_two_regimes_dated(f"S{s}", "MKT", before, after))
    betas = pd.concat(frames, ignore_index=True)

    full = assess_regime_break(betas).set_index("factor")
    peri = assess_regime_break(betas, peri_obs=15).set_index("factor")
    # full-regime: the early swings inflate the noise floor and hide the jump (ratio < 1);
    # peri-shock: the calm plateau vs the jump is unmistakable (ratio ≫ 1).
    assert full.loc["MKT", "median_break_ratio"] < 1.5
    assert peri.loc["MKT", "median_break_ratio"] > 5
    assert peri.loc["MKT", "median_break_ratio"] > full.loc["MKT", "median_break_ratio"]


def test_peri_obs_requires_bar_date():
    betas = _betas_two_regimes("ONE", "MKT", np.full(10, 2.0), np.full(10, -1.0))  # no bar_date
    with pytest.raises(ValueError, match="peri_obs requires"):
        assess_regime_break(betas, peri_obs=5)


def test_regime_break_on_subset_recovers_a_planted_break():
    """The parsimonious-subset diagnostic: fit reduced betas from returns + a factor panel and
    recover a beta that genuinely shifts across the boundary (collinearity-free, so clean)."""
    from tmkg.factors.diagnostics import regime_break_on_subset

    n_b, n_a = 80, 80
    db = [d.date() for d in pd.bdate_range("2024-10-01", periods=n_b)]   # orthodox_turn_2023
    da = [d.date() for d in pd.bdate_range("2025-03-20", periods=n_a)]   # imamoglu_shock_2025
    dates = db + da
    rng = np.random.default_rng(11)
    mkt = rng.normal(0, 0.02, n_b + n_a)
    beta_path = np.r_[np.full(n_b, 0.8), np.full(n_a, 1.9)]  # market beta jumps 0.8 -> 1.9
    y = beta_path * mkt + rng.normal(0, 1e-4, n_b + n_a)
    returns = pd.DataFrame({"symbol": "AAA", "bar_date": dates, "ret": y})
    panel = pd.DataFrame({"factor": "XU100", "bar_date": dates, "ret": mkt})

    out = regime_break_on_subset(
        returns, panel, factors=("XU100",), window=40, min_obs=40, peri_obs=40,
    ).set_index("factor")
    assert out.loc["XU100", "median_shift"] == pytest.approx(1.1, abs=0.2)
    assert out.loc["XU100", "median_break_ratio"] > 3  # a real break clears the local floor

"""The judge's statistical core (tmkg.signals.stats) — DSR + PBO (BUILD_PLAN.md M4).

These pin the two anti-overfitting statistics *before* the first real signal exists, so the
promotion harness cannot be quietly tuned to flatter a signal later. The decisive properties:

  - a genuinely **null** strategy (Sharpe ≈ 0) must be *deflated below the pass bar* — DSR fails;
  - a genuinely **skilled** strategy must clear the trial-count-adjusted bar — DSR passes;
  - **PBO** must be ≈ 0.5 for a pile of indistinguishable noise strategies, and small when one
    strategy carries real out-of-sample edge.

Plus exact known-answers for the building blocks (Sharpe, PSR, expected-max-Sharpe).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from tmkg.signals.stats import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_ratio,
)


# --- Sharpe ----------------------------------------------------------------


def test_sharpe_known_answer():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.015])
    expected = r.mean() / r.std(ddof=1)
    assert sharpe_ratio(r) == pytest.approx(expected, rel=1e-12)


def test_sharpe_annualizes_by_sqrt():
    r = np.array([0.001, 0.002, -0.001, 0.003, 0.0, 0.0015])
    assert sharpe_ratio(r, periods_per_year=252) == pytest.approx(
        sharpe_ratio(r) * math.sqrt(252), rel=1e-12
    )


def test_sharpe_degenerate_is_nan_not_zero_or_inf():
    assert math.isnan(sharpe_ratio([0.01, 0.01, 0.01]))  # zero variance
    assert math.isnan(sharpe_ratio([0.01]))              # too few points


# --- Probabilistic Sharpe --------------------------------------------------


def test_psr_at_observed_sharpe_is_one_half():
    # PSR(sr_star = observed SR) = Φ(0) = 0.5 by construction.
    rng = np.random.default_rng(0)
    r = rng.normal(0.001, 0.01, size=500)
    sr = sharpe_ratio(r)
    assert probabilistic_sharpe_ratio(r, sr_star=sr) == pytest.approx(0.5, abs=1e-9)


def test_psr_rises_with_sample_length():
    # Same per-period Sharpe, more observations -> more confident -> higher PSR vs sr_star=0.
    rng = np.random.default_rng(1)
    base = rng.normal(0.0008, 0.01, size=2000)
    short = base[:100]
    long = base
    p_short = probabilistic_sharpe_ratio(short, sr_star=0.0)
    p_long = probabilistic_sharpe_ratio(long, sr_star=0.0)
    # the long sample's Sharpe estimate is far more credible at the same effect size
    assert p_long > p_short


# --- Expected-max Sharpe (the deflation benchmark) -------------------------


def test_expected_max_sharpe_zero_for_single_trial():
    assert expected_max_sharpe(0.04, n_trials=1) == 0.0


def test_expected_max_sharpe_grows_with_trials():
    a = expected_max_sharpe(0.04, n_trials=10)
    b = expected_max_sharpe(0.04, n_trials=1000)
    assert 0.0 < a < b  # more trials -> higher bar a lucky winner must clear


def test_expected_max_sharpe_zero_variance_is_zero():
    assert expected_max_sharpe(0.0, n_trials=500) == 0.0


# --- Deflated Sharpe: the known-null / known-good asymmetry ----------------


def test_dsr_fails_on_null_strategy_after_many_trials():
    # Pure noise (true Sharpe 0) selected as the best of many trials must NOT pass.
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.01, size=750)
    res = deflated_sharpe_ratio(r, n_trials=100, sr_variance=0.02)
    assert res.sr_benchmark > 0.0          # the trial-count haircut raised the bar
    assert res.dsr < 0.95                  # ...and the null does not clear it
    assert res.passes is False


def test_dsr_passes_on_strongly_skilled_strategy():
    # A high, stable Sharpe over a long sample clears even a stiff trial-count haircut.
    rng = np.random.default_rng(9)
    r = rng.normal(0.0015, 0.004, size=1000)  # per-period SR ~ 0.37, very high
    res = deflated_sharpe_ratio(r, n_trials=50, sr_variance=0.01)
    assert res.sr_observed > res.sr_benchmark
    assert res.dsr > 0.95
    assert res.passes is True


def test_dsr_more_trials_lowers_the_verdict():
    # Same returns, more trials -> higher benchmark -> lower DSR (monotone haircut).
    rng = np.random.default_rng(11)
    r = rng.normal(0.0007, 0.01, size=600)
    few = deflated_sharpe_ratio(r, n_trials=5, sr_variance=0.02).dsr
    many = deflated_sharpe_ratio(r, n_trials=5000, sr_variance=0.02).dsr
    assert few > many


def test_dsr_result_is_serializable():
    rng = np.random.default_rng(3)
    r = rng.normal(0.0005, 0.01, size=300)
    d = deflated_sharpe_ratio(r, n_trials=20).as_dict()
    assert set(d) == {"dsr", "sr_observed", "sr_benchmark", "n_obs",
                      "n_trials", "skew", "kurtosis", "passes"}


# --- PBO via CSCV ----------------------------------------------------------


def test_pbo_split_count_matches_combinations():
    rng = np.random.default_rng(20)
    res = probability_of_backtest_overfitting(rng.normal(0, 0.01, (1500, 12)), n_partitions=10)
    assert res.n_splits == math.comb(10, 5)


def test_pbo_near_half_for_indistinguishable_noise():
    # N independent noise strategies: the in-sample winner carries no real OOS information, so
    # PBO sits in the middle. A single split-set is high-variance (the order statistic over a
    # finite realization is noisy), so average over several independent matrices — the textbook
    # ~0.5 only emerges in expectation.
    rng = np.random.default_rng(21)
    pbos = [
        probability_of_backtest_overfitting(rng.normal(0.0, 0.01, (1500, 50)),
                                            n_partitions=10).pbo
        for _ in range(8)
    ]
    assert 0.30 < float(np.mean(pbos)) < 0.70


def test_pbo_low_when_one_strategy_has_real_edge():
    # One strategy has a genuine positive mean every period; it wins IS and stays top OOS.
    rng = np.random.default_rng(22)
    M = rng.normal(0.0, 0.01, size=(2000, 20))
    M[:, 0] += 0.004  # a real, persistent edge in column 0
    res = probability_of_backtest_overfitting(M, n_partitions=10)
    assert res.pbo < 0.10
    assert res.median_logit > 0.0


def test_pbo_high_when_in_sample_winner_reverses_out_of_sample():
    # The textbook overfit: a first-half boost that exactly reverses in the second half makes the
    # in-sample winner a guaranteed out-of-sample laggard -> PBO must be ~1 (overfitting caught).
    rng = np.random.default_rng(23)
    M = rng.normal(0.0, 0.01, size=(1500, 20))
    half = M.shape[0] // 2
    boost = rng.normal(0.0, 0.006, size=M.shape[1])
    M[:half] += boost
    M[half:] -= boost
    res = probability_of_backtest_overfitting(M, n_partitions=10)
    assert res.pbo > 0.90
    assert res.median_logit < 0.0


def test_pbo_requires_even_partitions_and_multiple_strategies():
    M = np.zeros((100, 5))
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(M, n_partitions=7)
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 1)), n_partitions=8)

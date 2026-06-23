"""Adversarial-review regression tests for the M4 judge (VERIFICATION §4, review 2026-06-23).

A fresh falsifying agent attacked the promotion harness and found defects. These tests pin the
fixes so the holes cannot silently reopen. Each maps to a finding:

  D1 (critical) — the data-mining haircut was opt-in (``n_trials`` defaulted to 1 ⇒ benchmark 0
                  ⇒ inert), so a noise winner mined over many trials was promoted. Fix: n_trials
                  is required; a mined family can be passed as ``trial_pnls`` so the cross-trial
                  variance drives DSR and the siblings enter the PBO set.
  D3 (low)      — a too-short candidate raised ValueError out of the PBO step instead of failing
                  the gate cleanly. Fix: ``_safe_pbo`` clamps partitions / returns NaN ⇒ rejected.

The clean-survivals the agent confirmed (DSR/PSR math, P&L arithmetic, degenerate inputs, the
lookahead-is-a-data-layer-invariant boundary) are already covered by test_stats / test_backtest
/ test_harness_selftest and the PIT-leak invariant; not duplicated here.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.backtest import RESEARCH, run_book
from tmkg.signals.promotion import dollar_neutral_unit, evaluate_candidate


def _idx(n):
    return [dt.date(2023, 1, 2) + dt.timedelta(days=i) for i in range(n)]


# --- D1: the data-mining haircut can no longer be silently disarmed ---------


def test_n_trials_is_required_keyword():
    w = pd.DataFrame({"A": [0.5, 0.5], "B": [-0.5, -0.5]}, index=_idx(2))
    with pytest.raises(TypeError):
        evaluate_candidate(w, w)            # n_trials omitted -> must not run


def test_n_trials_must_be_positive():
    w = pd.DataFrame({"A": [0.5], "B": [-0.5]}, index=_idx(1))
    with pytest.raises(ValueError):
        evaluate_candidate(w, w, n_trials=0)


def test_mined_noise_winner_is_rejected_when_trial_count_is_honest():
    """D1 reproduction: mine many random static portfolios over PURE NOISE, keep the in-sample
    best, and evaluate it honestly (n_trials = number mined, the family passed as trial_pnls).
    The frictionless research book gives the winner no cost help — the trial-count haircut alone
    must reject it. (Before the fix, with the n_trials=1 default, this winner was promoted.)"""
    rng = np.random.default_rng(7)
    T, N, M = 400, 8, 200
    idx, syms = _idx(T), [f"S{i}" for i in range(N)]
    fwd = pd.DataFrame(rng.normal(0.0, 0.01, (T, N)), index=idx, columns=syms)

    trial_pnls, trial_weights = {}, {}
    for m in range(M):
        scores = pd.DataFrame(np.tile(rng.normal(size=N), (T, 1)), index=idx, columns=syms)
        w = dollar_neutral_unit(scores)
        res = run_book(w, fwd, book=RESEARCH)
        trial_pnls[m] = res.pnl
        trial_weights[m] = w

    pnl_df = pd.DataFrame(trial_pnls)
    winner = int(pnl_df.apply(lambda c: c.mean() / (c.std(ddof=1) + 1e-12)).idxmax())

    res = evaluate_candidate(
        trial_weights[winner], fwd, n_trials=M,
        trial_pnls=pnl_df, book=RESEARCH,
    )
    assert not res.dsr.passes, res.dsr.as_dict()      # haircut deflates the mined Sharpe away
    assert not res.promoted, res.summary()


def test_trial_family_raises_effective_trial_count():
    # Passing a wider trial family than the stated n_trials bumps the haircut up to the family
    # size (honesty floor) — you cannot under-report how widely you searched.
    rng = np.random.default_rng(8)
    T, N = 300, 6
    idx, syms = _idx(T), [f"S{i}" for i in range(N)]
    fwd = pd.DataFrame(rng.normal(0.0, 0.01, (T, N)), index=idx, columns=syms)
    cand = dollar_neutral_unit(pd.DataFrame(rng.normal(size=(T, N)), index=idx, columns=syms))
    family = pd.DataFrame({f"v{j}": rng.normal(0, 0.01, T) for j in range(40)}, index=idx)
    res = evaluate_candidate(cand, fwd, n_trials=1, trial_pnls=family)
    assert res.n_trials >= 40


# --- D3: too-short history fails the gate cleanly, never raises --------------


def test_short_history_fails_cleanly_without_raising():
    idx = _idx(5)                                       # far fewer rows than pbo_partitions=10
    cand = pd.DataFrame({"A": np.linspace(0.1, 0.5, 5), "B": -np.linspace(0.1, 0.5, 5)}, index=idx)
    fwd = pd.DataFrame({"A": [0.01, -0.01, 0.02, 0.0, 0.01],
                        "B": [0.0, 0.01, -0.01, 0.0, 0.0]}, index=idx)
    res = evaluate_candidate(cand, fwd, n_trials=5)     # must not raise
    assert isinstance(res.promoted, bool)


def test_single_row_history_yields_nan_pbo_and_rejects():
    idx = _idx(1)
    cand = pd.DataFrame({"A": [0.5], "B": [-0.5]}, index=idx)
    fwd = pd.DataFrame({"A": [0.02], "B": [0.01]}, index=idx)
    res = evaluate_candidate(cand, fwd, n_trials=1)
    assert np.isnan(res.pbo.pbo)
    assert not res.promoted

"""PIT backtester (tmkg.signals.backtest) — purge/embargo, cost+borrow, three books (M4).

The decisive checks:
  - a **hand-checked toy P&L** reconciles to the penny in every book (M4 exit-gate criterion);
  - purge/embargo splits never train on the future and honor the gaps;
  - the three books are correctly *ordered* by friction (research ≥ venue_feasible, and the
    short-ban / limit-lock constraints actually bite);
  - all three books produce output on the same weights.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.backtest import (
    BOOKS,
    RESEARCH,
    STRESS,
    VENUE_FEASIBLE,
    CostModel,
    apply_book_constraints,
    capacity_curve,
    purged_walk_forward_splits,
    run_all_books,
    run_book,
)


def _d(n, start=dt.date(2024, 1, 2)):
    return [start + dt.timedelta(days=i) for i in range(n)]


# --- purge + embargo splits -------------------------------------------------


def test_walk_forward_never_trains_on_the_future():
    splits = purged_walk_forward_splits(100, n_splits=4)
    for s in splits:
        assert s.train.size == 0 or s.train.max() < s.test.min()  # strictly causal


def test_purge_opens_a_gap_before_each_test_block():
    purge = 5
    splits = purged_walk_forward_splits(120, n_splits=5, purge=purge)
    for s in splits:
        if s.train.size:
            assert s.test.min() - s.train.max() - 1 >= purge


def test_test_blocks_tile_forward_without_overlap():
    splits = purged_walk_forward_splits(100, n_splits=4)
    seen = np.concatenate([s.test for s in splits])
    assert len(seen) == len(set(seen.tolist()))          # no test row used twice
    assert list(seen) == sorted(seen.tolist())            # marching forward in time


def test_splits_reject_degenerate_config():
    with pytest.raises(ValueError):
        purged_walk_forward_splits(3, n_splits=10)
    with pytest.raises(ValueError):
        purged_walk_forward_splits(100, n_splits=1)


# --- hand-checked toy P&L (M4 exit-gate criterion) --------------------------


def _toy():
    idx = _d(2)
    weights = pd.DataFrame({"A": [1.0, 1.0], "B": [-1.0, -1.0]}, index=idx)
    fwd = pd.DataFrame({"A": [0.02, 0.03], "B": [0.01, -0.01]}, index=idx)
    return weights, fwd


def test_research_book_pnl_is_hand_checked():
    weights, fwd = _toy()
    res = run_book(weights, fwd, book=RESEARCH)
    # gross_t0 = 1*0.02 + (-1)*0.01 = 0.01 ; gross_t1 = 1*0.03 + (-1)*(-0.01) = 0.04
    assert res.gross_pnl.tolist() == pytest.approx([0.01, 0.04])
    assert res.pnl.tolist() == pytest.approx([0.01, 0.04])   # frictionless: net == gross
    assert res.total_net_return == pytest.approx(0.05)
    assert res.avg_gross_exposure == pytest.approx(2.0)       # |1|+|-1|


def test_venue_feasible_book_pnl_is_hand_checked():
    weights, fwd = _toy()
    cm = CostModel(cost_bps=10.0, borrow_bps_annual=100.0, periods_per_year=252)
    res = run_book(weights, fwd, book=VENUE_FEASIBLE, cost_model=cm)
    # turnover: t0 = |1-0|+|-1-0| = 2 ; t1 = 0 (held flat). cost = 0.001 * turnover.
    # borrow: short notional = 1 each period; rate = (100/1e4)/252 = 0.01/252.
    cost_rate, borrow = 0.001, 0.01 / 252
    exp_t0 = 0.01 - cost_rate * 2 - borrow
    exp_t1 = 0.04 - 0.0 - borrow
    assert res.pnl.tolist() == pytest.approx([exp_t0, exp_t1])
    assert res.total_net_return == pytest.approx(exp_t0 + exp_t1)
    assert res.avg_turnover == pytest.approx(1.0)             # (2 + 0) / 2


# --- the three books are correctly ordered by friction ----------------------


def test_costs_make_venue_feasible_no_better_than_research():
    rng = np.random.default_rng(5)
    idx = _d(60)
    syms = list("ABCDE")
    weights = pd.DataFrame(rng.normal(0, 0.3, (60, 5)), index=idx, columns=syms)
    fwd = pd.DataFrame(rng.normal(0.0005, 0.01, (60, 5)), index=idx, columns=syms)
    books = run_all_books(weights, fwd)
    assert set(books) == set(BOOKS)                          # all three produce output
    # frictionless research total return dominates the cost-charged venue book
    assert books["research"].total_net_return >= books["venue_feasible"].total_net_return


def test_short_eligibility_zeroes_a_banned_short():
    idx = _d(1)
    weights = pd.DataFrame({"A": [-0.5], "B": [0.5]}, index=idx)
    se = pd.DataFrame({"A": [False], "B": [True]}, index=idx)   # A cannot be shorted
    held = apply_book_constraints(weights, book=VENUE_FEASIBLE, short_eligible=se)
    assert held.loc[idx[0], "A"] == 0.0     # banned short clipped to flat
    assert held.loc[idx[0], "B"] == 0.5     # long untouched


def test_stress_short_ban_clips_every_short():
    idx = _d(1)
    weights = pd.DataFrame({"A": [-0.5], "B": [0.5], "C": [-0.2]}, index=idx)
    held = apply_book_constraints(weights, book=STRESS)
    assert (held.loc[idx[0]] >= 0).all()
    assert held.loc[idx[0], "B"] == 0.5


def test_limit_lock_carries_the_prior_weight():
    idx = _d(2)
    weights = pd.DataFrame({"A": [0.4, 0.9]}, index=idx)        # wants to rebalance up on t1
    ll = pd.DataFrame({"A": [False, True]}, index=idx)          # but A is limit-locked on t1
    held = apply_book_constraints(weights, book=VENUE_FEASIBLE, limit_lock=ll)
    assert held.loc[idx[0], "A"] == pytest.approx(0.4)
    assert held.loc[idx[1], "A"] == pytest.approx(0.4)         # carried, not 0.9


# --- capacity curve ---------------------------------------------------------


def test_capacity_curve_emits_a_point_per_scale():
    rng = np.random.default_rng(6)
    idx = _d(80)
    weights = pd.DataFrame(rng.normal(0, 0.3, (80, 4)), index=idx, columns=list("ABCD"))
    fwd = pd.DataFrame(rng.normal(0.0004, 0.01, (80, 4)), index=idx, columns=list("ABCD"))
    curve = capacity_curve(weights, fwd, scales=(1.0, 2.0, 5.0))
    assert [p.notional_scale for p in curve] == [1.0, 2.0, 5.0]
    # net Sharpe must be a finite number at every scale
    assert all(np.isfinite(p.net_sharpe) for p in curve)

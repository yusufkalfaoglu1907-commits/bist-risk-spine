"""Limit-lock censoring (CLAUDE.md §5 / VERIFICATION.md "Limit-lock censoring").

A known limit-locked sequence must be (1) flagged and (2) have its censored daily
returns replaced by the single cumulative return across the lock window. These are
controlled fixtures (the mechanism is what's under test, not a vendor value).
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from tmkg.returns import compute_total_returns, flag_limit_lock
from tmkg.returns.limit_lock import censor_lock_windows


def _prices(closes: list[float]) -> pd.DataFrame:
    dates = [date(2025, 3, d) for d in range(3, 3 + len(closes))]
    return pd.DataFrame({"bar_date": dates, "close": closes})


def test_flag_marks_pinned_band_days_only():
    # +10% , +10% (two locks) , then +2% release ; first bar never a lock
    p = flag_limit_lock(_prices([100.0, 110.0, 121.0, 123.42]))
    assert list(p["is_limit_lock"]) == [False, True, True, False]


def test_flag_ignores_sub_band_moves():
    p = flag_limit_lock(_prices([100.0, 107.0, 112.0]))  # +7%, +4.7% — not pinned
    assert list(p["is_limit_lock"]) == [False, False, False]


def test_flag_detects_down_limit():
    p = flag_limit_lock(_prices([100.0, 90.0, 81.0]))  # -10%, -10%
    assert list(p["is_limit_lock"]) == [False, True, True]


def test_cumulative_replaces_censored_daily_returns():
    """The core invariant: a +10%/+10% lock run resolving at +2% becomes one
    cumulative window return on the release day, with the censored days NaN'd."""
    prices = flag_limit_lock(_prices([100.0, 110.0, 121.0, 123.42]))
    tr = compute_total_returns(prices, symbol="X")

    by_date = {r["bar_date"]: r for _, r in tr.iterrows()}
    lock1, lock2, release = date(2025, 3, 4), date(2025, 3, 5), date(2025, 3, 6)

    # censored daily returns removed (not a true return)
    assert math.isnan(by_date[lock1]["ret_nominal_try"])
    assert math.isnan(by_date[lock2]["ret_nominal_try"])
    # release day carries the cumulative cross-window return: 123.42/100 - 1
    assert by_date[release]["ret_nominal_try"] == pytest.approx(0.2342, abs=1e-9)
    # every touched row flagged
    assert by_date[lock1]["limit_lock_adj"]
    assert by_date[lock2]["limit_lock_adj"]
    assert by_date[release]["limit_lock_adj"]


def test_compounded_return_is_preserved_across_censoring():
    """Censoring must not change the total compounded return over the series —
    it only relocates it from the censored days to the release day."""
    closes = [100.0, 110.0, 121.0, 123.42]
    raw = compute_total_returns(_prices(closes), symbol="X")  # no lock flags
    censored = compute_total_returns(flag_limit_lock(_prices(closes)), symbol="X")

    raw_cum = (1.0 + raw["ret_nominal_try"]).prod()
    cen_cum = (1.0 + censored["ret_nominal_try"].dropna()).prod()
    assert cen_cum == pytest.approx(raw_cum, abs=1e-12)


def test_unresolved_run_at_series_end_stays_nan():
    """A lock run with no release day yet: the true return is unknowable -> NaN +
    flagged, never fabricated."""
    df = pd.DataFrame(
        {
            "ret_nominal_try": [float("nan"), 0.10, 0.10],
            "is_limit_lock": [False, True, True],
        }
    )
    out = censor_lock_windows(df, ["ret_nominal_try"])
    assert math.isnan(out["ret_nominal_try"].iloc[1])
    assert math.isnan(out["ret_nominal_try"].iloc[2])
    assert list(out["limit_lock_adj"]) == [False, True, True]


def test_no_lock_is_a_noop():
    df = pd.DataFrame({"ret_nominal_try": [float("nan"), 0.01, 0.02],
                       "is_limit_lock": [False, False, False]})
    out = censor_lock_windows(df, ["ret_nominal_try"])
    assert out["ret_nominal_try"].iloc[1] == 0.01
    assert not any(out["limit_lock_adj"])

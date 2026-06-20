"""Limit-lock censoring invariant (CLAUDE.md §5 / VERIFICATION.md).

Asserts the rule, not a vendor value: a known limit-locked sequence is flagged AND
its censored daily returns are replaced by the cumulative cross-window return. The
fuller mechanism tests live in tests/returns/test_limit_lock.py.
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from tmkg.returns import compute_total_returns, flag_limit_lock


@pytest.mark.invariant
def test_known_locked_sequence_is_flagged_and_collapsed():
    # +10%, +10% locked, resolving +2% on the third move
    closes = [100.0, 110.0, 121.0, 123.42]
    prices = flag_limit_lock(
        pd.DataFrame({"bar_date": [date(2025, 3, d) for d in (3, 4, 5, 6)], "close": closes})
    )
    # (1) flagged
    assert list(prices["is_limit_lock"]) == [False, True, True, False]

    tr = compute_total_returns(prices, symbol="LOCK")
    by_date = {r["bar_date"]: r for _, r in tr.iterrows()}

    # (2) censored daily returns gone; cumulative on the release day; all flagged
    assert math.isnan(by_date[date(2025, 3, 4)]["ret_nominal_try"])
    assert math.isnan(by_date[date(2025, 3, 5)]["ret_nominal_try"])
    assert by_date[date(2025, 3, 6)]["ret_nominal_try"] == pytest.approx(0.2342, abs=1e-9)
    assert all(by_date[d]["limit_lock_adj"] for d in (date(2025, 3, 4), date(2025, 3, 5), date(2025, 3, 6)))

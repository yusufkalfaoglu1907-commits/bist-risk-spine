"""Staleness flags (CLAUDE.md §5, design §3 line 72).

A bar with no traded quantity is a carried-forward (stale) price, not a fresh
trade — it must be flagged so M2 estimators can screen it or apply a lagged-beta
correction. Controlled fixtures: the detection rule is what's under test.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tmkg.returns import flag_staleness


def _prices(quantities):
    n = len(quantities)
    return pd.DataFrame(
        {
            "bar_date": [date(2025, 1, d + 1) for d in range(n)],
            "close": [10.0] * n,
            "quantity": quantities,
        }
    )


def test_zero_quantity_is_stale():
    out = flag_staleness(_prices([1000.0, 0.0, 500.0]))
    assert list(out["is_stale"]) == [False, True, False]


def test_missing_quantity_is_stale():
    out = flag_staleness(_prices([1000.0, None, 500.0]))
    assert list(out["is_stale"]) == [False, True, False]


def test_all_traded_is_never_stale():
    out = flag_staleness(_prices([1.0, 2.0, 3.0]))
    assert not any(out["is_stale"])


def test_absent_quantity_column_leaves_all_false():
    df = pd.DataFrame({"bar_date": [date(2025, 1, 1)], "close": [10.0]})
    out = flag_staleness(df)
    assert list(out["is_stale"]) == [False]

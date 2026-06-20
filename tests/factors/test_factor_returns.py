"""Unit tests for the M2 factor-return constructor (tmkg.factors.series).

Pure, synthetic, deterministic — pins the per-factor return rules and the
no-fabrication-on-gaps contract (§4) without touching the network or L2.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tmkg.factors.series import DIFF, LOG, SIMPLE, compute_factor_returns


def _levels(factor, pairs):
    return pd.DataFrame(
        {"factor": factor, "bar_date": [d for d, _ in pairs], "value": [v for _, v in pairs]}
    )


def test_simple_return_and_nan_first_obs():
    df = _levels("XU100", [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 110.0)])
    out = compute_factor_returns(df, method=SIMPLE)
    assert math.isnan(out.loc[0, "ret"])  # first obs has no prior level — never 0
    assert out.loc[1, "ret"] == pytest.approx(0.10)


def test_log_return():
    df = _levels("USDTRY", [(date(2024, 1, 1), 30.0), (date(2024, 1, 2), 33.0)])
    out = compute_factor_returns(df, method=LOG)
    assert out.loc[1, "ret"] == pytest.approx(math.log(33.0 / 30.0))


def test_diff_method_for_rate_factors():
    """A yield/CDS level moves additively: 40% -> 42% is +200bps, not +5%."""
    df = _levels("TRY10Y", [(date(2024, 1, 1), 40.0), (date(2024, 1, 2), 42.0)])
    out = compute_factor_returns(df, method=DIFF)
    assert out.loc[1, "ret"] == pytest.approx(2.0)


def test_per_factor_method_map_and_grouping():
    df = pd.concat(
        [
            _levels("XU100", [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 105.0)]),
            _levels("CDS", [(date(2024, 1, 1), 250.0), (date(2024, 1, 2), 260.0)]),
        ]
    )
    out = compute_factor_returns(df, method={"XU100": SIMPLE, "CDS": DIFF})
    xu = out[out["factor"] == "XU100"].reset_index(drop=True)
    cds = out[out["factor"] == "CDS"].reset_index(drop=True)
    assert xu.loc[1, "ret"] == pytest.approx(0.05)   # multiplicative
    assert cds.loc[1, "ret"] == pytest.approx(10.0)  # additive (bps)
    # a factor absent from the map falls back to simple
    out2 = compute_factor_returns(df, method={"XU100": SIMPLE})
    cds2 = out2[out2["factor"] == "CDS"].reset_index(drop=True)
    assert cds2.loc[1, "ret"] == pytest.approx(260.0 / 250.0 - 1.0)


def test_single_observation_factor_yields_nan_not_a_guess():
    df = _levels("BRENT", [(date(2024, 1, 1), 80.0)])
    out = compute_factor_returns(df, method=SIMPLE)
    assert len(out) == 1
    assert math.isnan(out.loc[0, "ret"])  # §4: no prior -> NaN, never fabricated


def test_gaps_are_not_filled():
    """A missing trading day is not reindexed/forward-filled: returns are computed
    between consecutive *available* observations only, never across an invented bar."""
    df = _levels(
        "XU100",
        [(date(2024, 1, 1), 100.0), (date(2024, 1, 4), 120.0)],  # 1/2, 1/3 absent
    )
    out = compute_factor_returns(df, method=SIMPLE)
    assert len(out) == 2  # no phantom rows for the gap
    assert out.loc[1, "ret"] == pytest.approx(0.20)  # 100 -> 120 across the real gap


def test_non_positive_level_has_no_log_return():
    df = _levels("X", [(date(2024, 1, 1), 1.0), (date(2024, 1, 2), 0.0), (date(2024, 1, 3), 2.0)])
    out = compute_factor_returns(df, method=LOG)
    assert math.isnan(out.loc[1, "ret"])  # ln(0) refused, not -inf/guessed
    assert math.isnan(out.loc[2, "ret"])  # prior level was non-positive


def test_extra_columns_carried_through_for_landing():
    df = _levels("XU100", [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 110.0)])
    df["knowledge_date"] = df["bar_date"]
    df["source"] = "matriks"
    out = compute_factor_returns(df)
    assert {"knowledge_date", "source", "ret"} <= set(out.columns)


def test_unknown_method_refused():
    df = _levels("X", [(date(2024, 1, 1), 1.0), (date(2024, 1, 2), 2.0)])
    with pytest.raises(ValueError):
        compute_factor_returns(df, method="bogus")

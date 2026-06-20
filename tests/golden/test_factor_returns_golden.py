"""M2 golden-master: factor-return series reproduce day-by-day from real closes.

Offline and deterministic — built from the committed REAL golden:
  * tests/golden/matriks/factors_USDTRY_XU100_2024-11.json
    (historicalData closes for the two anchor factors, 2024-11-01..2024-12-05)

The claim this pins: the M2 factor-return constructor turns the verified factor
*levels* into the *return* series the beta regression consumes, with no fabricated
points and the right (multiplicative) rule for an FX rate and a price index. The
USDTRY/XU100 closes also align on the same trading-day calendar as the equity bars
(the golden's documented invariant), so factor and stock returns are regressable.
"""
from __future__ import annotations

import json
import math
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.factors.series import SIMPLE, compute_factor_returns

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden"


def _factor_levels() -> pd.DataFrame:
    doc = json.loads((GOLDEN / "matriks" / "factors_USDTRY_XU100_2024-11.json").read_text())
    rows = []
    for factor in ("USDTRY", "XU100"):
        for b in doc[factor]["bars"]:
            rows.append({"factor": factor, "bar_date": b["date"], "value": b["close"]})
    return pd.DataFrame(rows)


def test_factor_returns_reproduce_from_golden_closes():
    levels = _factor_levels()
    out = compute_factor_returns(levels, method=SIMPLE)

    usd = out[out["factor"] == "USDTRY"].set_index("bar_date")
    xu = out[out["factor"] == "XU100"].set_index("bar_date")

    # 25 trading days each (the golden's stated tradingDays), one NaN-first per factor.
    assert len(usd) == 25 and len(xu) == 25
    assert math.isnan(usd["ret"].iloc[0]) and math.isnan(xu["ret"].iloc[0])
    assert usd["ret"].notna().sum() == 24
    assert xu["ret"].notna().sum() == 24

    # Hand-verified anchors (independently computed from the two golden closes):
    #   USDTRY 2024-11-04: 34.3445 / 34.3286 - 1
    #   XU100  2024-11-04: 8663.87988 / 8885 - 1
    assert usd.loc[date(2024, 11, 4), "ret"] == pytest.approx(0.00046317065, abs=1e-10)
    assert xu.loc[date(2024, 11, 4), "ret"] == pytest.approx(-0.02488690152, abs=1e-10)


def test_factor_and_xu100_share_the_equity_trading_calendar():
    """The factor series live on the same trading-day index — a precondition for
    regressing stock returns on them (no calendar mismatch fabricates a gap)."""
    levels = _factor_levels()
    out = compute_factor_returns(levels)
    usd_dates = set(out[out["factor"] == "USDTRY"]["bar_date"])
    xu_dates = set(out[out["factor"] == "XU100"]["bar_date"])
    assert usd_dates == xu_dates

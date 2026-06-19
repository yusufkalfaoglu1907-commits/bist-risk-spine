"""M1 golden-master: USD-primary total return across a hand-verified corporate
action (VERIFICATION.md §2). Offline and deterministic — built from the committed
EREGL/ASELS OHLCV + USDTRY golden samples (real captured data), so it pins the
return constructor forever without a network call.

The single claim this catches: Matriks bars are back-adjusted (W7), so a close-to-
close return ACROSS a capital-increase ex-date is a small real economic return, not
a multi-ten-percent dilution gap. A regression that drops the adjustment, or adds a
nominal dividend onto the adjusted basis, breaks these numbers.

Hand-verified anchors (see BUILD_LOG.md 2026-06-19):
  EREGL rights ex-date 2024-11-27:  TRY -1.3972% ,  USD -1.3803%
  ASELS rights ex-date 2023-08-25:  TRY +2.1081%
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.matriks import MatriksAdapter
from tmkg.returns.total_return import compute_total_returns, dividend_yields_from_raw

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "matriks"


def _bars_frame(fname: str, *, symbol: str | None = None, key: str | None = None):
    """Load a golden OHLCV file -> a price frame via the real adapter parser.
    ``key`` selects a top-level sub-object (e.g. 'USDTRY' in the factors golden)."""
    doc = json.loads((GOLDEN / fname).read_text())
    payload = doc[key] if key else doc["data"]
    rows = MatriksAdapter.parse_bars(payload, symbol=symbol)
    return pd.DataFrame(rows)


def test_eregl_usd_total_return_across_rights_exdate():
    prices = _bars_frame("ohlcv_EREGL_2024-11.json")
    fx = _bars_frame("factors_USDTRY_XU100_2024-11.json", symbol="USDTRY", key="USDTRY")

    tr = compute_total_returns(prices, fx, symbol="EREGL")
    row = tr.loc[tr["bar_date"] == date(2024, 11, 27)].iloc[0]

    assert row["ret_nominal_try"] == pytest.approx(-0.013972, abs=1e-6)
    assert row["ret_usd"] == pytest.approx(-0.013803, abs=1e-6)
    # back-adjustment proof: a naive unadjusted series would gap ~ -tens of %.
    assert abs(row["ret_nominal_try"]) < 0.05


def test_asels_total_return_across_rights_exdate():
    prices = _bars_frame("ohlcv_ASELS_2023-08.json")
    tr = compute_total_returns(prices, symbol="ASELS")  # no FX golden for 2023 window
    row = tr.loc[tr["bar_date"] == date(2023, 8, 25)].iloc[0]

    assert row["ret_nominal_try"] == pytest.approx(0.021081, abs=1e-6)
    assert abs(row["ret_nominal_try"]) < 0.05  # no dilution gap -> back-adjusted
    assert pd.isna(row["ret_usd"])             # no FX supplied -> NaN, not fabricated


def test_dividend_added_as_yield_makes_total_return():
    """The dividend MECHANISM: a cash dividend enters total return as a yield on its
    ex-date, on top of the price return (controlled fixture, not from L2)."""
    prices = pd.DataFrame(
        {"bar_date": ["2024-01-02", "2024-01-03"], "close": [100.0, 102.0]}
    )
    # raw close on ex-date 2024-01-03 = 102 nominal; cash dividend 5.10 -> yield 5%.
    y = dividend_yields_from_raw({date(2024, 1, 3): 5.10}, {date(2024, 1, 3): 102.0})
    assert y[date(2024, 1, 3)] == pytest.approx(0.05)

    tr = compute_total_returns(prices, symbol="X", dividend_yields=y)
    row = tr.iloc[0]
    # price return 2% + dividend yield 5% = 7% total return.
    assert row["ret_nominal_try"] == pytest.approx(0.02 + 0.05, abs=1e-9)


def test_dividend_yield_refuses_missing_raw_close():
    """No raw close on the ex-date -> refuse, never fabricate a yield (§4)."""
    with pytest.raises(ValueError):
        dividend_yields_from_raw({date(2024, 1, 3): 5.0}, {date(2024, 1, 2): 100.0})


def test_parse_corporate_actions_refuses_blank_exdates():
    """Golden key_fact: some ASELS capital_increase rows carry empty ex-dates; the
    parser must DROP and COUNT them, never coerce to a guessed date."""
    doc = json.loads((GOLDEN / "corpactions_EREGL_ASELS.json").read_text())
    rec = MatriksAdapter.parse_corporate_actions(doc["ASELS"])
    assert rec["refused_capital"] == 2          # two blank "" ex-dates in the golden
    assert "2023-08-25" in rec["capital_increase_exdates"]
    assert "" not in rec["capital_increase_exdates"]
    assert rec["dividends"][0]["ex_date"] == "2023-11-22"

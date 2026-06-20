"""M1 golden-master: CPI-real-TRY cross-check (VERIFICATION.md / BUILD_PLAN M1).

Offline and deterministic — built from two committed REAL goldens:
  * tests/golden/evds/cpi_TP.FG.J0_2023.json   — TÜFE genel endeks, monthly 2023 (EVDS3)
  * tests/golden/matriks/ohlcv_EREGL_monthly_2023.json — EREGL monthly closes, 2023

The claim this pins: the real-TRY return deflates the nominal-TRY return by realised
CPI inflation, step by step —  ret_real = (1 + ret_nominal) / (1 + cpi_inflation) - 1.
Both series share month-start bar_dates, so the constructor's per-step merge aligns
them exactly. Hand-verified anchors (computed independently from the two goldens):

  2023-02:  nominal +15.7480% ,  real +12.2187%   (CPI +3.1450%)
  2023-08:  nominal  +3.1654% ,  real  -5.4281%   (inflation overtakes a nominal gain)
  FY2023 :  nominal  +7.6116% ,  real -30.3486%   (CPI +54.5003%)

The FY headline is the whole reason USD is primary and nominal TRY is "reference
only" (CLAUDE.md §3): EREGL was *up* 7.6% in lira and *down* 30% in purchasing power.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.evds import EvdsAdapter
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.returns.total_return import compute_total_returns

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden"


def _cpi_frame() -> pd.DataFrame:
    doc = json.loads((GOLDEN / "evds" / "cpi_TP.FG.J0_2023.json").read_text())
    rows = EvdsAdapter.parse_cpi(doc["data"])
    return pd.DataFrame(rows)[["bar_date", "value"]]


def _eregl_prices() -> pd.DataFrame:
    doc = json.loads((GOLDEN / "matriks" / "ohlcv_EREGL_monthly_2023.json").read_text())
    return pd.DataFrame(MatriksAdapter.parse_bars(doc["data"], symbol="EREGL"))


def test_cpi_parses_to_real_index_values():
    """The EVDS golden carries the real, immutable 2003=100 CPI index levels."""
    cpi = _cpi_frame().set_index("bar_date")["value"]
    assert cpi[date(2023, 1, 1)] == pytest.approx(1203.48)
    assert cpi[date(2023, 12, 1)] == pytest.approx(1859.38)
    assert len(cpi) == 12


def test_real_try_deflates_nominal_by_cpi_monthly():
    cpi = _cpi_frame()
    prices = _eregl_prices()
    tr = compute_total_returns(prices, cpi=cpi, symbol="EREGL")

    feb = tr.loc[tr["bar_date"] == date(2023, 2, 1)].iloc[0]
    assert feb["ret_nominal_try"] == pytest.approx(0.157480, abs=1e-6)
    assert feb["ret_real_try"] == pytest.approx(0.122187, abs=1e-6)

    # August: a positive nominal month turns NEGATIVE in real terms (inflation > price gain).
    aug = tr.loc[tr["bar_date"] == date(2023, 8, 1)].iloc[0]
    assert aug["ret_nominal_try"] == pytest.approx(0.031654, abs=1e-6)
    assert aug["ret_real_try"] == pytest.approx(-0.054281, abs=1e-6)
    assert aug["ret_nominal_try"] > 0 > aug["ret_real_try"]


def test_fy2023_real_return_is_deeply_negative_despite_nominal_gain():
    """The headline cross-check: compounding the monthly real returns over 2023 must
    reproduce the hand-computed FY real return — up in lira, sharply down in real."""
    tr = compute_total_returns(_eregl_prices(), cpi=_cpi_frame(), symbol="EREGL")
    real = tr["ret_real_try"].dropna().astype(float)
    nominal = tr["ret_nominal_try"].dropna().astype(float)
    fy_real = float((1.0 + real).prod() - 1.0)
    fy_nominal = float((1.0 + nominal).prod() - 1.0)
    assert fy_nominal == pytest.approx(0.076116, abs=1e-5)
    assert fy_real == pytest.approx(-0.303486, abs=1e-5)


def test_cpi_tracking_asset_has_zero_real_return():
    """Identity anchor (fully real EVDS data): an asset whose nominal price IS the CPI
    index has, by construction, ~zero real return every month — a parameter-free check
    that the deflation is wired the right way round."""
    cpi = _cpi_frame()
    prices = cpi.rename(columns={"value": "close"})  # nominal price == CPI index
    tr = compute_total_returns(prices, cpi=cpi, symbol="CPIBASKET")
    assert tr["ret_real_try"].astype(float).abs().max() < 1e-12

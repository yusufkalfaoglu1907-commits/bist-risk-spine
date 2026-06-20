"""CPI-real-TRY through the full M1 pipeline — EVDS -> L2 factors -> PIT -> L2.

The pure-constructor golden (tests/golden/test_cpi_real_try_reconciliation.py) proves
the *math*; this proves the *plumbing*: CPI is ingested into L2 ``factors`` exactly
like FX, read BACK through ``PITAccess`` (knowledge_date gate and all), and the
constructed ``ret_real_try`` lands in L2 ``total_returns`` where signal code reads it.

Two things only an end-to-end test catches:
  1. ``ret_real_try`` actually persists through a DuckDB round-trip (it was computed
     in the constructor but never wired into ``build_total_returns`` until this slice);
  2. the CPI ``knowledge_date`` gate bites — December 2023 CPI prints 2024-01-03, so a
     build dated before that cannot deflate December, and ``ret_real_try`` stays NULL
     for that month rather than borrowing a future print (§4 / PIT).

Anchors (hand-verified, same as the constructor golden): FY2023 EREGL nominal +7.6116%,
real -30.3486% — up in lira, sharply down in purchasing power.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.evds import EvdsAdapter
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import build_total_returns, ingest_cpi, ingest_prices
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden"


class _GoldenEvds(EvdsAdapter):
    """Offline EVDS adapter: serves the committed CPI golden payload instead of the
    network, so ``ingest_cpi``'s parse/coerce/write path runs verbatim."""

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload

    def fetch(self, series, *, start, end, rtype="json"):  # type: ignore[override]
        return self._payload


class _GoldenMatriks(MatriksAdapter):
    """Offline Matriks adapter serving one symbol's golden bars."""

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload

    def fetch(self, tool, **params):  # type: ignore[override]
        return self._payload


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _evds() -> _GoldenEvds:
    doc = json.loads((GOLDEN / "evds" / "cpi_TP.FG.J0_2023.json").read_text())
    return _GoldenEvds(doc["data"])


def _eregl_monthly() -> _GoldenMatriks:
    doc = json.loads((GOLDEN / "matriks" / "ohlcv_EREGL_monthly_2023.json").read_text())
    return _GoldenMatriks(doc["data"])


def _land_inputs(store: L2Store) -> None:
    """Land the monthly EREGL bars + the 2023 CPI series into L2."""
    ingest_prices(_eregl_monthly(), store, "EREGL",
                  start="2023-01-01", end="2023-12-31", interval="monthly")
    ingest_cpi(_evds(), store, start="2023-01-01", end="2023-12-31")


def test_ingest_cpi_lands_in_factors_with_release_knowledge_date(tmp_path):
    store = _store(tmp_path)
    summary = ingest_cpi(_evds(), store, start="2023-01-01", end="2023-12-31")
    assert summary["n_points"] == 12

    df = store.read_table("factors", where="factor = 'CPI_TUFE'")
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    df["knowledge_date"] = pd.to_datetime(df["knowledge_date"]).dt.date
    jan = df.loc[df["bar_date"] == date(2023, 1, 1)].iloc[0]
    assert jan["value"] == pytest.approx(1203.48)
    # PIT honesty: January's index is only knowable on the ~3rd of February.
    assert jan["knowledge_date"] == date(2023, 2, 3)
    dec = df.loc[df["bar_date"] == date(2023, 12, 1)].iloc[0]
    assert dec["knowledge_date"] == date(2024, 1, 3)


def test_real_try_lands_in_l2_and_reconciles_fy2023(tmp_path):
    """End-to-end: ingest CPI + prices, build through PIT at a vintage where the whole
    2023 CPI is knowable (after the Dec print, 2024-01-03), read ret_real_try back from
    L2 and reconcile the FY headline."""
    store = _store(tmp_path)
    _land_inputs(store)

    summary = build_total_returns(store, "EREGL", as_of=date(2024, 2, 1))
    assert summary["n_returns"] == 11           # 12 monthly bars -> 11 returns
    assert summary["ret_real_try_null"] == 0    # every month's CPI was knowable

    con = store.connect()
    try:
        tr = PITAccess(date(2024, 2, 1), l2=con).series("total_returns", symbol="EREGL")
    finally:
        con.close()
    tr["bar_date"] = pd.to_datetime(tr["bar_date"]).dt.date

    feb = tr.loc[tr["bar_date"] == date(2023, 2, 1)].iloc[0]
    assert feb["ret_nominal_try"] == pytest.approx(0.157480, abs=1e-6)
    assert feb["ret_real_try"] == pytest.approx(0.122187, abs=1e-6)

    real = tr["ret_real_try"].dropna().astype(float)
    nominal = tr["ret_nominal_try"].dropna().astype(float)
    assert float((1.0 + nominal).prod() - 1.0) == pytest.approx(0.076116, abs=1e-5)
    assert float((1.0 + real).prod() - 1.0) == pytest.approx(-0.303486, abs=1e-5)


def test_december_real_try_null_before_its_cpi_is_knowable(tmp_path):
    """The CPI knowledge_date gate. At as_of 2023-12-15 the December PRICE bar is
    knowable (its knowledge_date is the month-start), so the December return IS built —
    but the December CPI prints 2024-01-03, so its ``ret_real_try`` must be NULL, never
    deflated by a future print (§4). November (CPI knowable 2023-12-03) keeps a real
    return, proving it's the knowledge-date gate biting and not a coverage gap."""
    store = _store(tmp_path)
    _land_inputs(store)

    summary = build_total_returns(store, "EREGL", as_of=date(2023, 12, 15))
    assert summary["n_returns"] == 11           # all 12 monthly bars knowable -> 11 returns
    assert summary["ret_real_try_null"] == 1    # exactly December, gated out

    con = store.connect()
    try:
        tr = PITAccess(date(2023, 12, 15), l2=con).series("total_returns", symbol="EREGL")
    finally:
        con.close()
    tr["bar_date"] = pd.to_datetime(tr["bar_date"]).dt.date

    dec = tr.loc[tr["bar_date"] == date(2023, 12, 1)].iloc[0]
    nov = tr.loc[tr["bar_date"] == date(2023, 11, 1)].iloc[0]
    assert pd.isna(dec["ret_real_try"])         # Dec CPI not yet published -> NULL
    assert pd.notna(dec["ret_nominal_try"])     # nominal needs no CPI -> still present
    assert pd.notna(nov["ret_real_try"])        # Nov CPI knowable 2023-12-03 <= as_of

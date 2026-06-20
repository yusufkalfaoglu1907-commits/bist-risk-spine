"""M1 end-to-end ingestion pipeline — offline reconciliation through L2 + PIT.

This is the milestone-tying test the slice-1 BUILD_LOG promised: it runs the FULL
M1 path — parse -> L2 write -> PITAccess read -> total-return construct -> L2 write
-> PITAccess read-back — and reconciles against the hand-verified golden anchor.
The golden OHLCV/FX captures stand in for the live fetch (real data, deterministic,
no network), so the live drift guard lives separately in tests/test_matriks_live.py.

Two distinct things proven here that the pure-constructor golden test cannot:
  1. the series survives a real DuckDB bitemporal round-trip (dtypes, NULLs, PKs);
  2. the PIT gate holds on the BUILT total_returns — a read at as_of = D never
     returns a return row whose knowledge_date is after D.

Anchor (BUILD_LOG 2026-06-19): EREGL 2024-11-27 = TRY -1.3972% / USD -1.3803%.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.ingest import pipeline as pipe
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import build_total_returns, ingest_factor_series, ingest_prices
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "matriks"


class _GoldenAdapter(MatriksAdapter):
    """Offline adapter: serves committed golden payloads instead of the network,
    so the pipeline's parse/transform/write logic is exercised verbatim (only the
    httpx hop is bypassed). Keyed by the ``symbol`` param the pipeline passes."""

    def __init__(self, payloads: dict[str, dict]) -> None:
        super().__init__()
        self._payloads = payloads

    def fetch(self, tool, **params):  # type: ignore[override]
        sym = params["symbol"]
        if sym not in self._payloads:
            raise AssertionError(f"unexpected fetch for {sym!r}")
        return self._payloads[sym]


def _adapter() -> _GoldenAdapter:
    eregl = json.loads((GOLDEN / "ohlcv_EREGL_2024-11.json").read_text())["data"]
    usdtry = json.loads((GOLDEN / "factors_USDTRY_XU100_2024-11.json").read_text())["USDTRY"]
    return _GoldenAdapter({"EREGL": eregl, "USDTRY": usdtry})


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _dates(df: pd.DataFrame) -> set:
    """DuckDB DATE columns read back as Timestamps; normalise to python date."""
    return set(pd.to_datetime(df["bar_date"]).dt.date)


def test_pipeline_reconciles_eregl_anchor_through_l2_and_pit(tmp_path):
    store = _store(tmp_path)
    adapter = _adapter()

    ingest_factor_series(adapter, store, "USDTRY", "USDTRY",
                         start="2024-11-01", end="2024-12-05")
    ingest_prices(adapter, store, "EREGL", start="2024-11-20", end="2024-12-05")
    summary = build_total_returns(store, "EREGL", as_of=date(2024, 12, 31))
    assert summary["n_returns"] > 0
    assert summary["ret_usd_null"] == 0  # FX present for every EREGL bar -> no NaN

    con = store.connect()
    try:
        pit = PITAccess(date(2024, 12, 31), l2=con)
        tr = pit.series("total_returns", symbol="EREGL")
    finally:
        con.close()

    tr["bar_date"] = pd.to_datetime(tr["bar_date"]).dt.date
    row = tr.loc[tr["bar_date"] == date(2024, 11, 27)].iloc[0]
    assert row["ret_nominal_try"] == pytest.approx(-0.013972, abs=1e-6)
    assert row["ret_usd"] == pytest.approx(-0.013803, abs=1e-6)


def test_pipeline_total_returns_obey_the_pit_gate(tmp_path):
    """The built total_returns row for 2024-11-27 (knowledge_date 2024-11-27) must be
    invisible to a read taken as of 2024-11-26 — the PIT gate on the derived series."""
    store = _store(tmp_path)
    adapter = _adapter()
    ingest_factor_series(adapter, store, "USDTRY", "USDTRY",
                         start="2024-11-01", end="2024-12-05")
    ingest_prices(adapter, store, "EREGL", start="2024-11-20", end="2024-12-05")
    build_total_returns(store, "EREGL", as_of=date(2024, 12, 31))

    con = store.connect()
    try:
        before = PITAccess(date(2024, 11, 26), l2=con).series("total_returns", symbol="EREGL")
        after = PITAccess(date(2024, 11, 27), l2=con).series("total_returns", symbol="EREGL")
    finally:
        con.close()

    assert date(2024, 11, 27) not in _dates(before)  # not yet knowable
    assert date(2024, 11, 27) in _dates(after)       # knowable on the day


def test_build_total_returns_respects_as_of_input_visibility(tmp_path):
    """Building as of 2024-11-26 must not even construct the 2024-11-27 return,
    because its input bar was not knowable yet (input-side PIT, not just read-side)."""
    store = _store(tmp_path)
    adapter = _adapter()
    ingest_factor_series(adapter, store, "USDTRY", "USDTRY",
                         start="2024-11-01", end="2024-12-05")
    ingest_prices(adapter, store, "EREGL", start="2024-11-20", end="2024-12-05")

    build_total_returns(store, "EREGL", as_of=date(2024, 11, 26))

    con = store.connect()
    try:
        # read with a permissive as_of: the 11-27 row was never built
        tr = PITAccess(date(2024, 12, 31), l2=con).series("total_returns", symbol="EREGL")
    finally:
        con.close()
    visible = _dates(tr)
    assert date(2024, 11, 27) not in visible
    assert max(visible) <= date(2024, 11, 26)


def test_run_m1_ingestion_orchestrator_writes_audit(tmp_path, monkeypatch):
    """The full orchestrator: lands FX + prices + total_returns and emits one audit
    report. Audit writer is stubbed so the unit test never touches the repo's
    data/cache; we assert on the returned report structure instead."""
    captured = {}
    monkeypatch.setattr(pipe, "write_run_report",
                        lambda name, payload: captured.update(name=name, payload=payload))

    store = _store(tmp_path)
    report = pipe.run_m1_ingestion(
        _adapter(), store, symbols=["EREGL"],
        start="2024-11-20", end="2024-12-05", as_of=date(2024, 12, 31),
    )

    assert captured["name"] == "m1_ingestion"
    assert captured["payload"] is report
    assert report["factors"][0]["factor"] == "USDTRY"
    assert report["prices"][0]["symbol"] == "EREGL" and report["prices"][0]["n_bars"] > 0
    assert report["total_returns"][0]["n_returns"] > 0
    assert report["total_returns"][0]["ret_usd_null"] == 0

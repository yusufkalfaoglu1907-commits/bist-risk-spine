"""L2 store — schema bootstrap + golden-bar Parquet round-trip (M0 T2).

Offline and reproducible: it lands the *captured* EREGL/ASELS OHLCV golden bars
(real adapter-shaped data) through L2Store.write_parquet and asserts the read-back
equals the golden bars field-for-field. No network — the live adapter path has its
own drift guard in tests/test_matriks_live.py.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

from tmkg.l2.store import L2Store

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "matriks"

# every table schema.sql must create (the bitemporal spine)
EXPECTED_TABLES = {
    "prices", "total_returns", "factors", "foreign_flow", "betas", "residuals",
    "residual_corr", "accounting_regime", "short_eligible", "signal_registry",
}


def _golden_prices_frame(fname: str) -> pd.DataFrame:
    """historicalData golden payload -> a `prices`-schema frame. knowledge_date =
    bar_date (a daily close is known end-of-that-day). M1 adds dividends/limit-lock."""
    payload = json.loads((GOLDEN / fname).read_text())["data"]
    sym = payload["symbol"]
    adjusted = payload["period"]["adjusted"]
    rows = []
    for b in payload["allBars"]:
        rows.append(
            {
                "symbol": sym,
                "bar_date": b["date"],
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume_try": b["volume"],
                "quantity": b["quantity"],
                "adjusted": adjusted,
                "is_limit_lock": False,
                "is_stale": False,
                "knowledge_date": b["date"],
                "source": "matriks",
            }
        )
    df = pd.DataFrame(rows)
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    df["knowledge_date"] = pd.to_datetime(df["knowledge_date"]).dt.date
    return df


def test_schema_bootstraps_all_tables(tmp_path):
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    assert EXPECTED_TABLES.issubset(set(store.tables()))


def test_golden_bars_roundtrip_equal(tmp_path):
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()

    expected = {}
    for fname in ("ohlcv_EREGL_2024-11.json", "ohlcv_ASELS_2023-08.json"):
        df = _golden_prices_frame(fname)
        store.write_parquet("prices", df)
        for _, r in df.iterrows():
            expected[(r["symbol"], r["bar_date"])] = (r["open"], r["high"], r["low"], r["close"])

    # a Parquet part was written (durable layer)
    assert list((tmp_path / "l2" / "prices").glob("*.parquet")), "no Parquet part written"

    back = store.read_table("prices")
    back["bar_date"] = pd.to_datetime(back["bar_date"]).dt.date  # DATE -> Timestamp on read
    assert len(back) == len(expected)
    got = {
        (r["symbol"], r["bar_date"]): (r["open"], r["high"], r["low"], r["close"])
        for _, r in back.iterrows()
    }
    assert got == expected


def test_write_parquet_is_pk_idempotent(tmp_path):
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    df = _golden_prices_frame("ohlcv_EREGL_2024-11.json")
    store.write_parquet("prices", df)
    store.write_parquet("prices", df)  # re-land same PKs -> no duplicates
    assert len(store.read_table("prices")) == len(df)

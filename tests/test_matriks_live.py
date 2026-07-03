"""Matriks REST adapter — offline unit guards + the live drift guard (M0 T1).

The offline tests run in the fast inner loop (`make verify`): they pin the
containment matcher and the auth-header contract (ADR-0002 — no X-Client-ID).
The live test hits the real REST API and SKIPS when Matriks is unreachable, so
the offline suite still passes in CI (v1 test_kap_live pattern). Run explicitly:

    PYTHONPATH=src python -m pytest tests/test_matriks_live.py -v
"""
from __future__ import annotations

import os

import pytest

from tmkg.ingest.matriks import MatriksAdapter, _golden_contains


# --- offline unit guards ---------------------------------------------------
def test_auth_header_is_username_key_no_client_id():
    """ADR-0002: auth is X-API-Key '<username>:<key>' ALONE. An X-Client-ID header
    makes the gateway 500 'Authentication failed', so it must never be sent."""
    a = MatriksAdapter()
    a.username, a.api_key = "00000", "sk_live_demo"
    h = a._rest_headers()
    assert h["X-API-Key"] == "00000:sk_live_demo"
    assert "X-Client-ID" not in h


def test_rest_endpoint_maps_snake_to_camel_slug():
    a = MatriksAdapter()
    assert a._rest_endpoint("historical_data").endswith("/tools/historicalData/execute")
    # camelCase passthrough (golden _provenance.tool uses camelCase directly)
    assert a._rest_endpoint("historicalData").endswith("/tools/historicalData/execute")


def test_golden_contains_allows_additive_live_fields_and_reordering():
    # live is a superset (extra 'timestamp') + reordered list -> still contained
    gold = {"symbol": "EREGL", "bars": [{"date": "d2", "close": 2.0}, {"date": "d1", "close": 1.0}]}
    live = {
        "symbol": "EREGL",
        "extra": "ok",
        "bars": [
            {"date": "d1", "close": 1.0, "timestamp": 111},
            {"date": "d2", "close": 2.0, "timestamp": 222},
        ],
    }
    assert _golden_contains(gold, live) == []


def test_golden_contains_flags_value_drift():
    gold = {"close": 1.0}
    bad = _golden_contains(gold, {"close": 1.5})
    assert bad and "close" in bad[0]


def test_golden_contains_flags_missing_key():
    bad = _golden_contains({"a": 1, "b": 2}, {"a": 1})
    assert any("b" in m for m in bad)


# --- live drift guard ------------------------------------------------------
def _adapter_or_skip() -> MatriksAdapter:
    if not os.getenv("MATRIKS_API_KEY"):
        pytest.skip("MATRIKS_API_KEY not in env (load .env) — skipping live Matriks test")
    a = MatriksAdapter()
    # Probe a real, cheap tool — NOT openapi.json, which has been observed to
    # time out (504/HTTP 000) while the actual tool endpoints serve fine. The
    # reachability signal must be a real tool call or the live guard skips falsely.
    try:
        a.fetch("symbolSearch", query="EREGL")
    except Exception:
        pytest.skip("Matriks unreachable — skipping live test")
    return a


@pytest.mark.live
def test_matriks_smoke_check_live():
    """The M0 [STOP] gate as a test: live re-fetch matches the immutable OHLCV
    value anchors and every connector tool is reachable. Raises on drift."""
    a = _adapter_or_skip()
    a.smoke_check()  # raises ContractDrift / SourceUnreachable on failure


@pytest.mark.live
def test_m1_pipeline_live_reconciles_eregl_anchor(tmp_path):
    """M1 end-to-end over the REAL network: fetch EREGL bars + USDTRY live, land them
    in a throwaway L2, build total_returns through PIT, and reconcile the hand-verified
    anchor (EREGL 2024-11-27 = TRY -1.3972% / USD -1.3803%). This is the live twin of
    tests/l2/test_m1_ingestion_pipeline.py — proves the live path, not just the goldens."""
    from datetime import date

    import pandas as pd

    from tmkg.ingest.pipeline import build_total_returns, ingest_factor_series, ingest_prices
    from tmkg.l2.store import L2Store
    from tmkg.pit.access import PITAccess

    a = _adapter_or_skip()
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()

    ingest_factor_series(a, store, "USDTRY", "USDTRY", start="2024-11-01", end="2024-12-05")
    ingest_prices(a, store, "EREGL", start="2024-11-20", end="2024-12-05")
    build_total_returns(store, "EREGL", as_of=date(2024, 12, 31))

    con = store.connect()
    try:
        tr = PITAccess(date(2024, 12, 31), l2=con).series("total_returns", symbol="EREGL")
    finally:
        con.close()
    tr["bar_date"] = pd.to_datetime(tr["bar_date"]).dt.date
    row = tr.loc[tr["bar_date"] == date(2024, 11, 27)].iloc[0]
    assert row["ret_nominal_try"] == pytest.approx(-0.013972, abs=1e-6)
    assert row["ret_usd"] == pytest.approx(-0.013803, abs=1e-6)


@pytest.mark.live
def test_accounting_regime_ingestion_live_reconciles_kchol_declaration_gate(tmp_path):
    """The accounting_regime consumer over the REAL network: pull KCHOL's live
    declaration history, tag regimes, land them in a throwaway L2, and reconcile the
    golden's PIT worked example — as_of 2025-04-15 the latest KNOWN period is 202412
    (declared 2025-02-18, ias29), with 202503 (declared 2025-04-30) still invisible.
    Declaration dates are immutable history, so this is a real value anchor."""
    from datetime import date

    from tmkg.ingest.pipeline import ingest_accounting_regime
    from tmkg.l2.store import L2Store
    from tmkg.pit.access import PITAccess

    a = _adapter_or_skip()
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    ingest_accounting_regime(a, store, "KCHOL")

    con = store.connect()
    try:
        latest = PITAccess(date(2025, 4, 15), l2=con).series(
            "accounting_regime", symbol="KCHOL", latest_by="period"
        )
    finally:
        con.close()
    assert list(latest["period"]) == ["202412"]   # 202503 declared later -> not yet known
    assert latest.iloc[0]["regime"] == "ias29_2023_2024"

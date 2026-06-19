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
    a.username, a.api_key = "39617", "sk_live_demo"
    h = a._rest_headers()
    assert h["X-API-Key"] == "39617:sk_live_demo"
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
    try:
        import httpx

        httpx.get("https://mcp.matriks.ai/openapi.json", timeout=8)
    except Exception:
        pytest.skip("Matriks unreachable — skipping live test")
    return a


@pytest.mark.live
def test_matriks_smoke_check_live():
    """The M0 [STOP] gate as a test: live re-fetch matches the immutable OHLCV
    value anchors and every connector tool is reachable. Raises on drift."""
    a = _adapter_or_skip()
    a.smoke_check()  # raises ContractDrift / SourceUnreachable on failure

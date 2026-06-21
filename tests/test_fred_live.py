"""FRED adapter — offline unit guards + the live drift guard.

Offline tests run in the fast inner loop (`make verify`): they pin the legacy
api_key contract, the observation parse, FRED's ``"."`` missing-value drop (§4),
and the fail-loud paths. The live test hits the real FRED API and SKIPS when it is
unreachable, so the offline suite still passes on an outage. Run explicitly:

    PYTHONPATH=src python -m pytest tests/test_fred_live.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import date

import httpx
import pytest

import tmkg.config as config
from tmkg.ingest.fred import VIX_FACTOR, VIX_SERIES, FredAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden" / "fred" / "vixcls_2025-03.json"


# --- offline unit guards ---------------------------------------------------
def test_obs_params_carry_key_as_query_param_not_bearer():
    a = FredAdapter()
    a.api_key = "demo_key"
    p = a._obs_params(VIX_SERIES, "2025-03-10", "2025-03-31")
    assert p["api_key"] == "demo_key"  # legacy auth: key is a query param
    assert p["series_id"] == VIX_SERIES
    assert p["file_type"] == "json"


def test_missing_key_raises_not_fabricate():
    a = FredAdapter()
    a.api_key = ""
    with pytest.raises(SourceUnreachable):
        a.fetch(VIX_SERIES, start="2025-03-10", end="2025-03-31")


def test_http_error_is_unreachable(monkeypatch):
    a = FredAdapter()
    a.api_key = "demo_key"

    def fake_get(url, **kw):
        return httpx.Response(400, text="Bad Request. Variable api_key is not set.",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("tmkg.ingest.fred.httpx.get", fake_get)
    with pytest.raises(SourceUnreachable):
        a.fetch(VIX_SERIES, start="2025-03-10", end="2025-03-31")


def test_json_without_observations_is_contract_drift(monkeypatch):
    a = FredAdapter()
    a.api_key = "demo_key"

    def fake_get(url, **kw):
        return httpx.Response(200, json={"realtime_start": "x", "unexpected": True},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("tmkg.ingest.fred.httpx.get", fake_get)
    with pytest.raises(ContractDrift):
        a.fetch(VIX_SERIES, start="2025-03-10", end="2025-03-31")


def test_parse_observations_from_golden_to_factor_rows():
    doc = json.loads(GOLDEN.read_text())
    rows = FredAdapter.parse_observations(doc["data"])
    by_date = {r["bar_date"]: r for r in rows}
    anchor = by_date[date(2025, 3, 17)]
    assert anchor["factor"] == VIX_FACTOR
    assert anchor["value"] == pytest.approx(20.51)
    assert anchor["knowledge_date"] == date(2025, 3, 17)  # close known end-of-day, no revisions
    assert anchor["source"] == "fred"
    assert anchor["ret"] is None
    # the 19-Mar shock-day VIX print is present
    assert by_date[date(2025, 3, 19)]["value"] == pytest.approx(19.9)


def test_parse_drops_fred_missing_sentinel_and_refuses_empty():
    # FRED writes a non-trading day as value "." -> dropped, never coerced (§4)...
    rows = FredAdapter.parse_observations(
        {"observations": [
            {"date": "2025-03-15", "value": "."},          # weekend / no print
            {"date": "2025-03-17", "value": "20.51"},
        ]}
    )
    assert [r["bar_date"] for r in rows] == [date(2025, 3, 17)]
    # ...and an all-missing payload is a loud failure, not an empty success.
    with pytest.raises(ContractDrift):
        FredAdapter.parse_observations({"observations": [{"date": "2025-03-15", "value": "."}]})


# --- live drift guard (skips when FRED is unreachable) ---------------------
def _fred_or_skip() -> FredAdapter:
    if not os.getenv("FRED_API_KEY") and not config.FRED_API_KEY:
        pytest.skip("FRED_API_KEY not set")
    a = FredAdapter(timeout=20.0)
    if not a.api_key:
        a.api_key = config.FRED_API_KEY
    try:
        a.fetch(VIX_SERIES, start="2025-03-17", end="2025-03-17")
    except SourceUnreachable as e:
        pytest.skip(f"FRED unreachable: {e}")
    return a


@pytest.mark.live
def test_live_vix_matches_golden_values():
    a = _fred_or_skip()
    doc = json.loads(GOLDEN.read_text())
    p = doc["_provenance"]["params"]
    live = a.fetch(p["series_id"], start=p["observation_start"], end=p["observation_end"])
    live_obs = {o["date"]: o["value"] for o in live["observations"]}
    for o in doc["data"]["observations"]:
        assert live_obs.get(o["date"]) == o["value"], f"VIX drift on {o['date']}"


@pytest.mark.live
def test_live_smoke_check_passes():
    a = _fred_or_skip()
    a.smoke_check()  # raises ContractDrift on any value mismatch; writes audit report

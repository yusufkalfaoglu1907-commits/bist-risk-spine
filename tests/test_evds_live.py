"""EVDS adapter — offline unit guards + the live drift guard.

The offline tests run in the fast inner loop (`make verify`): they pin the resolved
evds3 contract (the ``/igmevdsms-dis/series=...`` URL, the ``key`` header), the
CPI parse, and the critical SPA-HTML fail-loud (a moved/dead endpoint returns the
React app at HTTP 200 — that must raise, never parse as data, §4). The live test
hits the real EVDS API and SKIPS when it is unreachable, so the offline suite still
passes on an outage. Run explicitly:

    PYTHONPATH=src python -m pytest tests/test_evds_live.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import date

import httpx
import pytest

import tmkg.config as config
from tmkg.ingest.evds import (
    CPI_TUFE_SERIES,
    EvdsAdapter,
    _evds_date,
    _item_field,
    _release_knowledge_date,
)
from tmkg.pit.errors import ContractDrift, SourceUnreachable

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden" / "evds" / "cpi_TP.FG.J0_2023.json"


# --- offline unit guards ---------------------------------------------------
def test_series_url_and_key_header_match_resolved_contract():
    a = EvdsAdapter()
    a.api_key = "demo_key"
    a.base_url = "https://evds3.tcmb.gov.tr/igmevdsms-dis"
    url = a._series_url("TP.FG.J0", "2023-01-01", "2023-12-01", "json")
    assert url == (
        "https://evds3.tcmb.gov.tr/igmevdsms-dis/series=TP.FG.J0"
        "&startDate=01-01-2023&endDate=01-12-2023&type=json"
    )
    assert a._headers() == {"key": "demo_key"}  # post-2024 key is a header


def test_date_and_field_helpers():
    assert _evds_date("2023-03-01") == "01-03-2023"  # ISO -> EVDS
    assert _evds_date("01-03-2023") == "01-03-2023"  # already EVDS -> passthrough
    assert _item_field("TP.FG.J0") == "TP_FG_J0"     # dots -> underscores


def test_release_knowledge_date_is_next_month_pit_honest():
    # month M's CPI is only knowable from ~the 3rd of month M+1 (no lookahead).
    assert _release_knowledge_date(2023, 1) == date(2023, 2, 3)
    assert _release_knowledge_date(2023, 12) == date(2024, 1, 3)  # year rollover


def test_missing_key_raises_not_fabricate():
    a = EvdsAdapter()
    a.api_key = ""
    with pytest.raises(SourceUnreachable):
        a.fetch(CPI_TUFE_SERIES, start="2023-01-01", end="2023-12-01")


def test_spa_html_at_200_is_unreachable_not_data(monkeypatch):
    """The dead legacy path / a moved endpoint returns the React SPA at HTTP 200.
    fetch must FAIL LOUD, never parse HTML as a series (the §4 trap this slice fixes)."""
    a = EvdsAdapter()
    a.api_key = "demo_key"

    def fake_get(url, **kw):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<!DOCTYPE html><html><head><title>EVDS</title></head></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("tmkg.ingest.evds.httpx.get", fake_get)
    with pytest.raises(SourceUnreachable):
        a.fetch(CPI_TUFE_SERIES, start="2023-01-01", end="2023-12-01")


def test_json_without_items_is_contract_drift(monkeypatch):
    a = EvdsAdapter()
    a.api_key = "demo_key"

    def fake_get(url, **kw):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"totalCount": 0, "unexpected": True},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("tmkg.ingest.evds.httpx.get", fake_get)
    with pytest.raises(ContractDrift):
        a.fetch(CPI_TUFE_SERIES, start="2023-01-01", end="2023-12-01")


def test_parse_cpi_from_golden_to_factor_rows():
    doc = json.loads(GOLDEN.read_text())
    rows = EvdsAdapter.parse_cpi(doc["data"])
    assert len(rows) == 12
    jan = rows[0]
    assert jan["factor"] == "CPI_TUFE"
    assert jan["bar_date"] == date(2023, 1, 1)
    assert jan["value"] == pytest.approx(1203.48)
    assert jan["knowledge_date"] == date(2023, 2, 3)  # PIT-honest release date
    assert jan["source"] == "evds"


def test_parse_cpi_drops_nonnumeric_and_refuses_empty():
    # a blank/"ND" reading is dropped, never coerced (§4)...
    rows = EvdsAdapter.parse_cpi(
        {"items": [{"Tarih": "2023-1", "TP_FG_J0": "ND"}, {"Tarih": "2023-2", "TP_FG_J0": "1241.33"}]}
    )
    assert [r["bar_date"] for r in rows] == [date(2023, 2, 1)]
    # ...and an all-empty payload is a loud failure, not an empty success.
    with pytest.raises(ContractDrift):
        EvdsAdapter.parse_cpi({"items": [{"Tarih": "2023-1", "TP_FG_J0": None}]})


# --- live drift guard (skips when EVDS is unreachable) ---------------------
def _evds_or_skip() -> EvdsAdapter:
    if not os.getenv("EVDS_API_KEY") and not config.EVDS_API_KEY:
        pytest.skip("EVDS_API_KEY not set")
    a = EvdsAdapter(timeout=20.0)
    if not a.api_key:
        a.api_key = config.EVDS_API_KEY
    try:
        a.fetch(CPI_TUFE_SERIES, start="2023-01-01", end="2023-01-01")
    except SourceUnreachable as e:
        pytest.skip(f"EVDS unreachable: {e}")
    return a


def test_live_cpi_matches_golden_values():
    """Published CPI is immutable — the live re-fetch must reproduce the golden
    field-for-field. Real drift teeth on the evds3 contract."""
    a = _evds_or_skip()
    a.smoke_check()  # raises ContractDrift on any mismatch; writes the audit report

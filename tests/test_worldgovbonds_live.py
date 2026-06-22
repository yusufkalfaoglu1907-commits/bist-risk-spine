"""WorldGovernmentBonds adapter — offline unit guards + the live drift guard.

Offline tests run in the fast inner loop (`make verify`): they pin the CDS parse, the
date-window clip, the no-fabrication drops (§4) and the fail-loud paths. The live test
hits the real WGB chart API and SKIPS when unreachable, so the offline suite still passes
on an outage. Run explicitly:

    PYTHONPATH=src python -m pytest tests/test_worldgovbonds_live.py -v
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import httpx
import pytest

from tmkg.ingest.worldgovbonds import TURKEY_CDS_FACTOR, WorldGovBondsAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

GOLDEN = (pathlib.Path(__file__).resolve().parent
          / "golden" / "worldgovbonds" / "turkey_cds_5y_anchors.json")


def _result(quotes):
    """A minimal WGB result envelope from (DATA_VAL, CLOSE_VAL) pairs."""
    return {"num": len(quotes), "quote": {
        str(i + 1): {"CLOSE_VAL": cv, "DATA_VAL": dv}
        for i, (dv, cv) in enumerate(quotes)}}


# --- offline unit guards ---------------------------------------------------
def test_parse_cds_to_factor_rows():
    rows = WorldGovBondsAdapter.parse_series(
        _result([("2025-03-21", 252.18), ("2025-03-24", 300.20)]))
    by = {r["bar_date"]: r for r in rows}
    a = by[date(2025, 3, 24)]
    assert a["factor"] == TURKEY_CDS_FACTOR
    assert a["value"] == pytest.approx(300.20)        # bps, verbatim
    assert a["knowledge_date"] == date(2025, 3, 24)   # daily close known end-of-day
    assert a["source"] == "worldgovbonds" and a["ret"] is None


def test_parse_cds_clips_to_window():
    rows = WorldGovBondsAdapter.parse_series(
        _result([("2022-12-30", 500.0), ("2024-01-02", 280.23), ("2027-01-01", 100.0)]),
        start="2023-01-01", end="2026-06-22")
    assert [r["bar_date"] for r in rows] == [date(2024, 1, 2)]  # out-of-window dropped


def test_parse_cds_drops_nonnumeric_and_refuses_empty():
    rows = WorldGovBondsAdapter.parse_series(
        _result([("2025-03-21", None), ("2025-03-24", 300.20)]))
    assert [r["bar_date"] for r in rows] == [date(2025, 3, 24)]  # blank dropped (§4)
    with pytest.raises(ContractDrift):
        WorldGovBondsAdapter.parse_series(_result([("2025-03-21", None)]))


def test_registry_wgb_factors_all_have_a_fetch_config():
    """Every registry factor sourced from WGB must have a FUNCTION/tenor config — else the
    driver would not know how to fetch it. Guards the rates/CDS rung end to end."""
    from tmkg.factors import registry
    from tmkg.ingest.worldgovbonds import WGB_FACTORS

    wgb = {f.name for f in registry.CORE_FACTORS if f.source == "worldgovbonds"}
    assert wgb == {"TRCDS5Y", "TRY2Y", "TRY10Y"}
    assert wgb <= set(WGB_FACTORS)  # each has a fetch config


def test_non_success_envelope_is_unreachable(monkeypatch):
    a = WorldGovBondsAdapter()

    def fake_post(url, **kw):
        return httpx.Response(403, json={"code": "rest_forbidden", "data": {"status": 403}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr("tmkg.ingest.worldgovbonds.httpx.post", fake_post)
    with pytest.raises(SourceUnreachable):
        a.fetch()


def test_success_without_quote_is_contract_drift(monkeypatch):
    a = WorldGovBondsAdapter()

    def fake_post(url, **kw):
        return httpx.Response(200, json={"success": True, "result": {"num": 0}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr("tmkg.ingest.worldgovbonds.httpx.post", fake_post)
    with pytest.raises(ContractDrift):
        a.fetch()


# --- live drift guard (skips when WGB is unreachable) ----------------------
def _wgb_or_skip() -> WorldGovBondsAdapter:
    a = WorldGovBondsAdapter(timeout=20.0)
    try:
        a.fetch()
    except SourceUnreachable as e:
        pytest.skip(f"WorldGovernmentBonds unreachable: {e}")
    return a


@pytest.mark.live
def test_live_cds_matches_golden_anchors():
    a = _wgb_or_skip()
    doc = json.loads(GOLDEN.read_text())
    result = a.fetch()
    live = {q["DATA_VAL"]: q["CLOSE_VAL"] for q in result["quote"].values()}
    for d, gv in doc["data"]["anchors"].items():
        assert abs(float(live[d]) - float(gv)) < 1e-6, f"Turkey CDS drift on {d}"


@pytest.mark.live
def test_live_smoke_check_passes():
    a = _wgb_or_skip()
    a.smoke_check()  # raises ContractDrift on any anchor mismatch; writes audit report

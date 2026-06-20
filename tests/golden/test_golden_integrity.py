"""Golden-sample integrity: the Matriks snapshots parse and their pinned facts
hold. These are the reconciliation anchors for M0/M1 (VERIFICATION.md). They run
offline (no connector) and pass today; the live drift-guard re-fetch is added in M0.
"""
from __future__ import annotations

import json
import pathlib

import pytest

GOLDEN = pathlib.Path(__file__).resolve().parent / "matriks"


def _load(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text())


@pytest.mark.golden
def test_all_golden_json_valid():
    files = list(GOLDEN.glob("*.json"))
    assert files, "no golden samples found"
    for f in files:
        json.loads(f.read_text())  # raises on invalid JSON


@pytest.mark.golden
def test_eregl_back_adjusted_no_gap_on_rights_exdate():
    bars = {b["date"]: b for b in _load("ohlcv_EREGL_2024-11.json")["data"]["allBars"]}
    move = bars["2024-11-27"]["close"] / bars["2024-11-26"]["close"] - 1
    # rights ex-date 2024-11-27: back-adjusted => a normal daily move, not a gap
    assert abs(move) < 0.05


@pytest.mark.golden
def test_declaration_dates_support_pit_example():
    from tmkg.ingest.matriks import MatriksAdapter

    # parse the REAL contract shape (declarationDates.items[*].periods) via the
    # production parser, so this golden anchor and the ingestion code can't diverge.
    rows = MatriksAdapter.parse_declaration_periods(
        _load("declaration_dates_KCHOL.json")["data"]
    )
    periods = {r["period"]: r["declaration_date"] for r in rows}
    # the PIT-leak worked example: 202503 only knowable from 2025-04-30
    assert periods["202503"] == "2025-04-30"
    assert periods["202412"] == "2025-02-18"


@pytest.mark.golden
def test_accounting_regime_bases_diverge():
    g = _load("accounting_regime_KCHOL_202412.json")
    assert g["adjusted_ias29"]["revenue"] / g["unadjusted_nominal"]["revenue"] > 1.2


@pytest.mark.golden
def test_universe_cross_section_present():
    u = _load("universe_bist30.json")["data"]
    assert u["totalSymbols"] == 30
    for s in ("EKGYO", "KCHOL", "EREGL", "GARAN"):
        assert s in u["symbols"]


@pytest.mark.golden
def test_foreign_flow_historic_resolves_quirk_and_classifies_foreign_houses():
    """The historic (range) mode is the fix for the M0 foreign-flow quirk; foreign
    custodian houses are present and classifiable, and the per-investor demographic
    series is the still-open 400 residual."""
    d = _load("foreign_flow_GARAN_historic_2025Q1.json")["data"]
    foreign = {b["code"]: b for b in d["foreign_brokers_seen"]}
    assert {"MLB", "HSY"} <= set(foreign)  # BofA + HSBC, the foreign conduits
    assert all(b["classification"] == "foreign" for b in d["foreign_brokers_seen"])
    # GARANTI BBVA is DOMESTIC despite the foreign parent — the classification caveat
    grm = next(b for b in d["domestic_brokers_sample"] if b["code"] == "GRM")
    assert grm["classification"] == "domestic"
    # the open residual is pinned so we notice if it ever starts working
    assert any("investor/historic" in lim["service"] for lim in d["limitations"])


@pytest.mark.golden
def test_takas_custody_shows_the_imamoglu_shock():
    """The 19-Mar-2025 regime boundary is visible in settlement-custody value — a
    real-data anchor for the M2 betas-break-across-the-shock exit-gate criterion."""
    tv = _load("takas_GARAN_2025_shock.json")["data"]["tV_series"]
    assert tv["20250319"] < tv["20250318"]        # shock day drop
    assert tv["20250321"] < tv["20250319"]        # keeps falling
    assert tv["20250318"] / tv["20250321"] > 1.25  # ~23%+ custody-value contraction

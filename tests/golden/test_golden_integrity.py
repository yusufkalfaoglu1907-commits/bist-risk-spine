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
    periods = {
        p["period"]: p["declarationDate"]
        for p in _load("declaration_dates_KCHOL.json")["data"]["periods"]
    }
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

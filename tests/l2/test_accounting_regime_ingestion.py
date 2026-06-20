"""accounting_regime fundamentals ingestion — the consumer the state machine lacked.

The state machine (tests/invariants/test_accounting_regime.py) maps a period to its
regime and refuses cross-regime growth; this proves the INGESTION that actually
populates the L2 ``accounting_regime`` table from the vendor's declaration-date history
— and that the regime tag inherits the fundamental's PIT gate (knowledge_date =
declarationDate), so a period is invisible until it was declared.

Driven offline by the committed declaration-dates golden (real KCHOL declaration
history back to FY2008). The worked example the golden documents is the make-or-break
PIT check: period 202503 (Q1 2025) was declared 2025-04-30, so a read as_of 2025-04-15
must see 202412 (declared 2025-02-18) as the latest known KCHOL fundamental, NOT 202503.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import ingest_accounting_regime
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "matriks"


class _GoldenAdapter(MatriksAdapter):
    """Serves the committed declaration-dates payload instead of the network."""

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload

    def fetch(self, tool, **params):  # type: ignore[override]
        return self._payload


def _adapter() -> _GoldenAdapter:
    doc = json.loads((GOLDEN / "declaration_dates_KCHOL.json").read_text())
    return _GoldenAdapter(doc["data"])


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def test_ingest_tags_every_period_with_its_regime(tmp_path):
    store = _store(tmp_path)
    summary = ingest_accounting_regime(_adapter(), store, "KCHOL")
    assert summary["n_periods"] > 20  # full history back to FY2008
    # all three regimes appear (the history spans both switches)
    assert set(summary["regimes"]) == {
        "nominal_pre2023", "ias29_2023_2024", "suspended_2025_2027"
    }

    con = store.connect()
    try:
        df = PITAccess(date(2026, 6, 1), l2=con).series("accounting_regime", symbol="KCHOL")
    finally:
        con.close()
    regime = df.set_index("period")["regime"].to_dict()
    assert regime["202212"] == "nominal_pre2023"     # pre-2023
    assert regime["202312"] == "ias29_2023_2024"     # FY2023 switch
    assert regime["202412"] == "ias29_2023_2024"
    assert regime["202503"] == "suspended_2025_2027"  # FY2025 switch


def test_knowledge_date_is_the_declaration_date(tmp_path):
    store = _store(tmp_path)
    ingest_accounting_regime(_adapter(), store, "KCHOL")
    con = store.connect()
    try:
        df = PITAccess(date(2026, 6, 1), l2=con).series("accounting_regime", symbol="KCHOL")
    finally:
        con.close()
    df["knowledge_date"] = pd.to_datetime(df["knowledge_date"]).dt.date
    kd = df.set_index("period")["knowledge_date"].to_dict()
    assert kd["202503"] == date(2025, 4, 30)   # Q1 2025 declared 2025-04-30
    assert kd["202412"] == date(2025, 2, 18)   # FY2024 declared 2025-02-18


def test_regime_tag_inherits_the_declaration_pit_gate(tmp_path):
    """The golden's worked example: as_of 2025-04-15, the latest KNOWN KCHOL period is
    202412 (declared 2025-02-18); 202503 (declared 2025-04-30) is NOT yet visible — the
    regime tag is gated exactly like the fundamental it describes."""
    store = _store(tmp_path)
    ingest_accounting_regime(_adapter(), store, "KCHOL")
    con = store.connect()
    try:
        visible = PITAccess(date(2025, 4, 15), l2=con).series(
            "accounting_regime", symbol="KCHOL", latest_by="period"
        )
    finally:
        con.close()
    assert list(visible["period"]) == ["202412"]            # latest KNOWN, not 202503
    assert visible.iloc[0]["regime"] == "ias29_2023_2024"

    con = store.connect()
    try:
        all_visible = set(
            PITAccess(date(2025, 4, 15), l2=con)
            .series("accounting_regime", symbol="KCHOL")["period"]
        )
    finally:
        con.close()
    assert "202503" not in all_visible  # declared 2025-04-30 > as_of -> invisible
    assert "202412" in all_visible

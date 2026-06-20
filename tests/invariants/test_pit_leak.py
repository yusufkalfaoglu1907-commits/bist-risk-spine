"""PIT-leak detector — the single most important guard (CLAUDE.md §5).

The full detector: land KCHOL's real declaration-dated periods into a bitemporal
L2 table (knowledge_date = declarationDate) and prove PITAccess never returns a
period that had not yet been declared as of the read's as_of date.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.matriks import MatriksAdapter
from tmkg.l2.store import L2Store
from tmkg.pit import PITAccess, PITViolation
from tmkg.returns import regime_for_period


@pytest.mark.invariant
def test_pitaccess_refuses_without_as_of():
    with pytest.raises(PITViolation):
        PITAccess(None)  # type: ignore[arg-type]


def _land_kchol_declarations(store: L2Store, load_golden) -> None:
    # parse the REAL declaration-dates contract (declarationDates.items[*].periods)
    # via the production parser, then tag each period's regime — the same path the
    # ingestion uses, so this detector exercises the shipped code, not a parallel copy.
    decls = MatriksAdapter.parse_declaration_periods(
        load_golden("declaration_dates_KCHOL.json")["data"]
    )
    rows = [
        {
            "symbol": "KCHOL",
            "period": d["period"],
            "regime": regime_for_period(d["period"]),
            "knowledge_date": pd.to_datetime(d["declaration_date"]).date(),
        }
        for d in decls
    ]
    store.bootstrap_schema()
    store.write_parquet("accounting_regime", pd.DataFrame(rows))


@pytest.mark.invariant
def test_no_read_returns_knowledge_date_after_as_of(tmp_path, load_golden):
    """Worked example: with as_of=2025-04-15, the latest KCHOL fundamental visible
    must be period 202412 (declared 2025-02-18), NOT 202503 (declared 2025-04-30)."""
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    _land_kchol_declarations(store, load_golden)
    con = store.connect()
    try:
        as_of = date(2025, 4, 15)
        pit = PITAccess(as_of, l2=con)

        visible = pit.series("accounting_regime", symbol="KCHOL")
        # the core invariant: nothing leaked from the future
        kd = pd.to_datetime(visible["knowledge_date"]).dt.date
        assert (kd <= as_of).all()
        # 202503 was declared 2025-04-30 > as_of -> must be invisible
        assert "202503" not in set(visible["period"])

        latest = pit.series("accounting_regime", symbol="KCHOL", latest_by="period")
        assert list(latest["period"]) == ["202412"]
        assert latest.iloc[0]["regime"] == "ias29_2023_2024"

        # and a later vintage DOES see 202503 (the filter is a date gate, not a drop)
        later = PITAccess(date(2025, 5, 1), l2=con).series(
            "accounting_regime", symbol="KCHOL", latest_by="period"
        )
        assert list(later["period"]) == ["202503"]
    finally:
        con.close()

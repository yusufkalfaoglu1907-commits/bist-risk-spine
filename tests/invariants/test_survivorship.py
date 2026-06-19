"""Survivorship / W2-wall invariant (CLAUDE.md §5, data-sourcing W2).

A delisted name must stay in the store with its history intact, and the as-of
universe must be survivorship-correct: a read dated inside a now-dead name's
listing window still includes it; a read after delisting excludes it from the
active set WITHOUT erasing the name. This proves the mechanism early (M0 T5);
real delisted-name ingestion with sourced dates lands in M1.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tmkg.l2.store import L2Store
from tmkg.pit import PITAccess


def _land(store: L2Store) -> None:
    store.bootstrap_schema()
    rows = [
        # a live anchor — open membership
        {"symbol": "EREGL", "universe": "listed", "universe_class": "operating",
         "valid_from": date(2015, 1, 1), "valid_to": None,
         "knowledge_date": date(2015, 1, 1), "source": "test"},
        # a delisted name — listed 2016-03-01, delisted 2021-06-30
        {"symbol": "DLSTD", "universe": "listed", "universe_class": "operating",
         "valid_from": date(2016, 3, 1), "valid_to": date(2021, 6, 30),
         "knowledge_date": date(2016, 3, 1), "source": "test"},
    ]
    store.write_parquet("universe_membership", pd.DataFrame(rows))


def test_delisted_name_present_in_past_universe_absent_in_present(tmp_path):
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    _land(store)
    con = store.connect()
    try:
        # 2020: DLSTD was still listed -> in the as-of universe
        past = set(PITAccess(date(2020, 1, 1), l2=con).universe()["symbol"])
        assert {"EREGL", "DLSTD"} <= past

        # 2026: DLSTD delisted in 2021 -> NOT in the active universe (no look-ahead
        # survivorship), but EREGL persists
        now = set(PITAccess(date(2026, 1, 1), l2=con).universe()["symbol"])
        assert "EREGL" in now and "DLSTD" not in now

        # survivorship: the dead name's history is RETAINED, not deleted
        retained = con.execute(
            "SELECT count(*) FROM universe_membership WHERE symbol = 'DLSTD'"
        ).fetchone()[0]
        assert retained == 1
    finally:
        con.close()


@pytest.mark.invariant
def test_universe_respects_knowledge_date(tmp_path):
    """A membership only learned later must not appear in an earlier as-of read."""
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    store.write_parquet(
        "universe_membership",
        pd.DataFrame([
            {"symbol": "LATE", "universe": "listed", "universe_class": "operating",
             "valid_from": date(2018, 1, 1), "valid_to": None,
             "knowledge_date": date(2024, 1, 1), "source": "test"},
        ]),
    )
    con = store.connect()
    try:
        # as_of 2020 is inside the validity window but BEFORE we knew of it
        assert PITAccess(date(2020, 1, 1), l2=con).universe().empty
        assert "LATE" in set(PITAccess(date(2024, 6, 1), l2=con).universe()["symbol"])
    finally:
        con.close()

"""Real delisted-name survivorship (CLAUDE.md §5 / data-sourcing W2 / BUILD_PLAN M1).

The synthetic ``DLSTD`` test proves the mechanism; this proves it on a REAL name with
SOURCED dates: SODA (Soda Sanayii A.Ş.), delisted from Borsa İstanbul on the
2020-09-30 last-trading-day when it was absorbed into Şişecam (SISE). Every date is
traceable to the committed provenance golden (a Borsa İstanbul announcement).

Beyond mere retention, this exercises the BITEMPORAL correction the real case carries
and the synthetic one cannot: the delisting was only ANNOUNCED on 2020-09-18, so a
universe read dated before then must show SODA as still-open (no look-ahead), while a
read after it sees the closed window — yet the dead row is never erased.

All assertions sit inside the airtight 2020-06..2020-10 sourced window; none depends on
the (deliberately conservative, documented) valid_from lower bound.
"""
from __future__ import annotations

import pathlib
from datetime import date

import pytest

from tmkg.ingest.survivorship import ingest_delisting
from tmkg.l2.store import L2Store
from tmkg.pit import PITAccess

GOLDEN = (
    pathlib.Path(__file__).resolve().parents[1]
    / "golden" / "borsa" / "delisting_SODA_2020.json"
)


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    ingest_delisting(store, GOLDEN)
    return store


def test_ingest_delisting_lands_sourced_rows(tmp_path):
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    report = ingest_delisting(store, GOLDEN)
    assert report["ticker"] == "SODA"
    assert report["n_rows"] == 2  # open membership + delisting correction
    assert report["key_dates"]["last_trading_day"] == "2020-09-30"

    df = store.read_table("universe_membership", where="symbol = 'SODA'")
    assert len(df) == 2  # both bitemporal versions retained


@pytest.mark.invariant
def test_soda_present_while_listed_absent_after_delisting(tmp_path):
    store = _store(tmp_path)
    con = store.connect()
    try:
        # mid-2020: SODA listed and trading -> in the as-of universe
        mid = set(PITAccess(date(2020, 6, 30), l2=con).universe()["symbol"])
        assert "SODA" in mid

        # one day before the last trading day -> still in
        last = set(PITAccess(date(2020, 9, 29), l2=con).universe()["symbol"])
        assert "SODA" in last

        # delisting date and after -> gone from the active universe
        after = set(PITAccess(date(2020, 10, 1), l2=con).universe()["symbol"])
        assert "SODA" not in after

        # survivorship: the dead name's rows are RETAINED, not deleted
        retained = con.execute(
            "SELECT count(*) FROM universe_membership WHERE symbol = 'SODA'"
        ).fetchone()[0]
        assert retained == 2
    finally:
        con.close()


@pytest.mark.invariant
def test_no_delisting_lookahead_before_it_was_announced(tmp_path):
    """The bitemporal correction: the delisting was announced 2020-09-18. A read dated
    BEFORE that must not 'see' the future delisting — SODA reads as still-open. A read
    after the announcement (but before the last trading day) still includes SODA, now
    on the closed window. Neither read fabricates nor hides the dead name."""
    store = _store(tmp_path)
    con = store.connect()
    try:
        # 2020-09-10: before the Borsa announcement -> only the OPEN row is knowable
        before_announce = set(
            PITAccess(date(2020, 9, 10), l2=con).universe()["symbol"]
        )
        assert "SODA" in before_announce  # known as a live member, not as delisting

        # 2020-09-20: after the announcement, before last trade -> still trading, present
        after_announce = set(
            PITAccess(date(2020, 9, 20), l2=con).universe()["symbol"]
        )
        assert "SODA" in after_announce
    finally:
        con.close()

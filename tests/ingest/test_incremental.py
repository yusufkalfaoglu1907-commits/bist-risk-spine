"""Incremental update core (M9.2) — window + freshness logic, deterministic.

Pins the daily-refresh brain: the incremental window (no pull when current, full backfill when empty,
overlap re-fetch otherwise) and the freshness split (current / stale / missing).
"""
from __future__ import annotations

import datetime as dt

from tmkg.ingest.incremental import classify_freshness, incremental_window

AS_OF = dt.date(2026, 6, 27)


# --- incremental_window -----------------------------------------------------------------------

def test_no_window_when_already_current():
    assert incremental_window(dt.date(2026, 6, 27), AS_OF) is None
    assert incremental_window(dt.date(2026, 6, 30), AS_OF) is None   # ahead of as_of


def test_full_backfill_when_no_history():
    assert incremental_window(None, AS_OF, full_start="2023-01-01") == ("2023-01-01", "2026-06-27")


def test_overlap_refetch_from_last_bar():
    # last bar 2026-06-20, overlap 5 -> start 2026-06-15
    assert incremental_window(dt.date(2026, 6, 20), AS_OF, overlap_days=5) == ("2026-06-15", "2026-06-27")


def test_zero_overlap_starts_at_last_bar():
    assert incremental_window(dt.date(2026, 6, 20), AS_OF, overlap_days=0) == ("2026-06-20", "2026-06-27")


# --- classify_freshness -----------------------------------------------------------------------

def test_freshness_splits_current_stale_missing():
    latest = {
        "FRESH": dt.date(2026, 6, 26),   # 1 day -> current
        "STALE": dt.date(2026, 5, 1),    # ~57 days -> stale
        # "GONE" absent -> missing
    }
    r = classify_freshness(latest, ["FRESH", "STALE", "GONE"], AS_OF, max_age_days=7)
    assert r["n_current"] == 1 and r["n_stale"] == 1 and r["n_missing"] == 1
    assert r["stale"][0]["symbol"] == "STALE"
    assert r["missing"] == ["GONE"]


def test_freshness_budget_tolerates_weekend_lag():
    # 3-day-old bar with a 7-day budget is still current (weekends/holidays)
    r = classify_freshness({"X": dt.date(2026, 6, 24)}, ["X"], AS_OF, max_age_days=7)
    assert r["n_current"] == 1 and r["n_stale"] == 0


def test_stale_sorted_oldest_first():
    latest = {"A": dt.date(2026, 6, 1), "B": dt.date(2026, 3, 1)}
    r = classify_freshness(latest, ["A", "B"], AS_OF, max_age_days=7)
    assert [s["symbol"] for s in r["stale"]] == ["B", "A"]   # oldest (most stale) first

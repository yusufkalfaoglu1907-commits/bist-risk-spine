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


# --- plan_daily_pulls -------------------------------------------------------------------------

def test_plan_daily_pulls_builds_incremental_windows():
    from tmkg.ingest.incremental import plan_daily_pulls
    stale = [{"symbol": "A", "last_bar": "2026-06-20", "age_days": 7},
             {"symbol": "B", "last_bar": "2026-03-01", "age_days": 118}]
    plan = plan_daily_pulls(stale, AS_OF, overlap_days=5)
    assert plan[0] == {"symbol": "A", "start": "2026-06-15", "end": "2026-06-27", "age_days": 7}
    assert plan[1]["symbol"] == "B" and plan[1]["start"] == "2026-02-24"


def test_plan_daily_pulls_respects_limit():
    from tmkg.ingest.incremental import plan_daily_pulls
    stale = [{"symbol": s, "last_bar": "2026-01-01", "age_days": 177} for s in ("A", "B", "C")]
    assert len(plan_daily_pulls(stale, AS_OF, limit=2)) == 2


# --- 429 rate-limit backoff/retry (the M9 hardening, BUILD_LOG 2026-07-01) ---------------------
# The Matriks gateway 429s after ~59 back-to-back pulls; the old loop dropped the other ~500
# names. A simulated 429 must be RETRIED (after a backoff sleep), not dropped.

class _FakePrices:
    """Stand-in for pipeline.ingest_prices: raises RateLimited the first ``fail_times`` calls
    (per symbol), then succeeds. Records how many times it was called."""
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, adapter, store, sym, *, start, end):
        from tmkg.pit.errors import RateLimited
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RateLimited(f"Matriks {sym} HTTP 429 RATE_LIMIT_EXCEEDED")
        return {"symbol": sym, "n_bars": 3}


def _patch_pipeline(monkeypatch, fake_prices):
    import tmkg.ingest.pipeline as pipe
    monkeypatch.setattr(pipe, "ingest_prices", fake_prices)
    monkeypatch.setattr(pipe, "build_total_returns", lambda *a, **k: {"n_returns": 3})


def test_rate_limited_pull_is_retried_not_dropped(monkeypatch):
    from tmkg.ingest.incremental import _pull_one
    _patch_pipeline(monkeypatch, _FakePrices(fail_times=2))
    slept: list[float] = []
    item = {"symbol": "AKBNK", "start": "2026-06-20", "end": "2026-07-02"}
    r = _pull_one(None, None, item, AS_OF, backoff_seconds=65.0, max_retries=3,
                  sleep=slept.append)
    assert r["ok"] is True and r["n_bars"] == 3
    assert r["retries"] == 2                      # succeeded on the 3rd attempt
    assert slept == [65.0, 65.0]                  # backed off before each retry, not dropped


def test_rate_limited_exhausted_reports_failure_not_silent(monkeypatch):
    from tmkg.ingest.incremental import _pull_one
    _patch_pipeline(monkeypatch, _FakePrices(fail_times=99))  # never recovers
    slept: list[float] = []
    item = {"symbol": "GARAN", "start": "2026-06-20", "end": "2026-07-02"}
    r = _pull_one(None, None, item, AS_OF, backoff_seconds=65.0, max_retries=3,
                  sleep=slept.append)
    assert r["ok"] is False and r["rate_limited"] is True   # surfaced, not swallowed
    assert len(slept) == 3                                   # backed off max_retries times


def test_run_daily_update_paces_between_symbols_and_aggregates(monkeypatch):
    import tmkg.ingest.incremental as inc
    # stub the L2/graph scan so this stays a pure unit test (no DB/network)
    monkeypatch.setattr(inc, "freshness_report", lambda con, store, *, as_of, max_age_days: {
        "n_current": 0, "n_stale": 2, "n_missing": 0, "n_symbols": 2,
        "stale": [{"symbol": "A", "last_bar": "2026-06-20", "age_days": 12},
                  {"symbol": "B", "last_bar": "2026-06-20", "age_days": 12}],
        "missing": [],
    })
    _patch_pipeline(monkeypatch, _FakePrices(fail_times=0))   # both succeed immediately
    slept: list[float] = []
    r = inc.run_daily_update(None, None, adapter=object(), as_of=AS_OF, dry_run=False,
                             pace_seconds=1.5, sleep=slept.append)
    assert r["n_ok"] == 2 and r["n_failed"] == 0
    assert slept == [1.5]           # paced once BETWEEN the two symbols (not before the first)

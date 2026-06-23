"""M4 exit-gate self-test — the harness must reject noise before it can certify signal.

BUILD_PLAN.md M4 exit gate: "a **known-null** signal (shuffled labels) correctly **fails** the
gate (DSR ≤ 0, doesn't beat persistence) · a **known-good toy** signal passes · backtester
reproduces a hand-checked toy P&L · all three books produce output."

The hand-checked toy P&L lives in test_backtest.py (run_book reconciled to the penny in every
book). This file pins the rest of the exit gate end-to-end through the promotion gate:

  - a candidate that genuinely predicts forward returns is **promoted** (beats the baseline
    ladder, DSR passes the trial-count haircut, PBO low);
  - the *same* candidate with its labels **shuffled** (the forward returns permuted) carries no
    real edge and is **rejected** — decisively on DSR;
  - all three books (research / venue_feasible / stress) produce output;
  - the verdict round-trips into the L2 signal_registry and reads back through PITAccess.

A harness that promoted the shuffled-label null would be a harness that flatters; the whole M4
milestone exists to make that impossible.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.backtest import BOOKS, run_all_books
from tmkg.signals.promotion import dollar_neutral_unit, evaluate_candidate
from tmkg.signals.registry import (
    build_registry_entry,
    write_registry_entry,
    write_registry_report,
)

T, N = 600, 12
PERSIST = 0.95          # AR(1) persistence of the latent predictor (=> slow, low-turnover book)
N_TRIALS = 50           # honest trial count the candidate is haircut against


def _ar1(rng, t, n, phi):
    x = np.zeros((t, n))
    x[0] = rng.normal(size=n)
    for i in range(1, t):
        x[i] = phi * x[i - 1] + np.sqrt(1 - phi * phi) * rng.normal(size=n)
    return x


def _toy_world(seed=101):
    """A persistent latent predictor ``x`` known at t; forward returns = 0.6·x + noise. The
    candidate sees ``x`` directly; the baselines see only the noisy realized returns."""
    rng = np.random.default_rng(seed)
    idx = [dt.date(2023, 1, 2) + dt.timedelta(days=i) for i in range(T)]
    syms = [f"S{i:02d}" for i in range(N)]
    x = _ar1(rng, T, N, PERSIST)
    noise = rng.normal(0.0, 1.6, size=(T, N))
    fwd = pd.DataFrame(0.006 * (0.6 * x + noise), index=idx, columns=syms)  # ~daily-scale
    predictor = pd.DataFrame(x, index=idx, columns=syms)
    candidate = dollar_neutral_unit(predictor)        # weights from the clean predictor
    return candidate, fwd


def test_known_good_candidate_is_promoted():
    candidate, fwd = _toy_world()
    res = evaluate_candidate(candidate, fwd, n_trials=N_TRIALS)
    assert res.beats_baselines, res.summary()
    assert res.dsr.passes, res.dsr.as_dict()
    assert res.pbo.pbo < 0.5, res.pbo.as_dict()
    assert res.promoted, res.failed_checks


def test_known_null_shuffled_labels_is_rejected():
    candidate, fwd = _toy_world()
    # Shuffle the labels: permute the forward-return rows so the candidate's weights line up
    # with the *wrong* day's returns -> the real edge is destroyed, structure preserved.
    rng = np.random.default_rng(202)
    perm = rng.permutation(len(fwd))
    fwd_shuffled = pd.DataFrame(fwd.to_numpy()[perm], index=fwd.index, columns=fwd.columns)
    res = evaluate_candidate(candidate, fwd_shuffled, n_trials=N_TRIALS)
    assert not res.dsr.passes, res.dsr.as_dict()      # the decisive failure
    assert not res.promoted, res.summary()


def test_all_three_books_produce_output():
    candidate, fwd = _toy_world()
    books = run_all_books(candidate, fwd)
    assert set(books) == set(BOOKS) == {"research", "venue_feasible", "stress"}
    for name, r in books.items():
        assert r.n_periods == T
        assert np.isfinite(r.net_sharpe), name


def test_verdict_round_trips_through_l2_and_pit(tmp_path):
    from datetime import date

    from tmkg.l2.store import L2Store
    from tmkg.pit.access import PITAccess

    candidate, fwd = _toy_world()
    res = evaluate_candidate(candidate, fwd, n_trials=N_TRIALS)

    store = L2Store(db_path=tmp_path / "l2.duckdb")
    entry = build_registry_entry(
        res, signal_id="m4_selftest_known_good",
        hypothesis="latent persistent predictor; toy self-test",
        feature_family="toy", knowledge_date=date(2024, 6, 1),
        test_start=date(2023, 1, 2), test_end=date(2024, 8, 24),
        cost_model="CostModel(default)", purge_embargo="n/a (single-pass self-test)",
    )
    write_registry_entry(store, entry)
    report = write_registry_report(entry, tmp_path / "m4_registry_report.json")
    assert report.exists()

    # read back ONLY through PITAccess; a read dated before the write sees nothing (no lookahead)
    con = store.connect()
    try:
        before = PITAccess(date(2024, 5, 1), l2=con).series("signal_registry")
        after = PITAccess(date(2024, 7, 1), l2=con).series("signal_registry")
    finally:
        con.close()
    assert before.empty                                          # knowledge_date > as_of hidden
    assert len(after) == 1
    assert bool(after.iloc[0]["promoted"]) == res.promoted

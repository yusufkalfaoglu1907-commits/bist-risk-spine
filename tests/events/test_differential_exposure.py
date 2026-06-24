"""M6 differential-exposure event signal (system-design-v2.md §236 / BUILD_PLAN.md M6).

Pins the cross-sectional spread logic, the §238 control-survival guard, the PIT hold semantics,
and — the exit-gate self-test — that a planted exposure-drift edge is **promoted** through the M4
judge while a no-edge null is **rejected**. Synthetic worlds only; the real-data run is its own
slice (events ingested + betas read through PITAccess).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tmkg.events.differential_exposure import (
    EventSpec,
    control_survival,
    differential_exposure_weights,
    event_cross_section_weights,
)
from tmkg.signals.backtest import RESEARCH
from tmkg.signals.promotion import evaluate_candidate


# --- the cross-section spread ------------------------------------------------


def test_cross_section_longs_high_shorts_low_dollar_neutral():
    exp = pd.Series([-2.0, -1.0, 0.0, 1.0, 2.0], index=list("abcde"))
    w = event_cross_section_weights(exp, shock_sign=+1, quantile=0.3)
    assert w["d"] > 0 and w["e"] > 0          # high exposure -> long
    assert w["a"] < 0 and w["b"] < 0          # low exposure -> short
    assert w["c"] == 0.0                      # middle -> no bet
    assert w.sum() == pytest.approx(0.0)      # dollar-neutral
    assert w.abs().sum() == pytest.approx(1.0)  # unit gross


def test_shock_sign_flips_the_book():
    exp = pd.Series([-2.0, -1.0, 0.0, 1.0, 2.0], index=list("abcde"))
    up = event_cross_section_weights(exp, +1, quantile=0.3)
    dn = event_cross_section_weights(exp, -1, quantile=0.3)
    pd.testing.assert_series_equal(dn, -up)


def test_no_dispersion_is_flat_not_fabricated():
    exp = pd.Series([1.0, 1.0, 1.0, 1.0], index=list("abcd"))
    w = event_cross_section_weights(exp, +1, quantile=0.3)
    assert (w == 0.0).all()


def test_nan_exposure_is_dropped_never_zeroed():
    exp = pd.Series([-2.0, np.nan, 0.0, 2.0], index=list("abcd"))
    w = event_cross_section_weights(exp, +1, quantile=0.3)
    assert w["b"] == 0.0  # unknown exposure carries no position
    assert w.abs().sum() == pytest.approx(1.0)


# --- the §238 control-survival guard ----------------------------------------


def test_control_survival_counts_low_exposure_names():
    exp = pd.Series([-2.0, -0.1, 0.0, 0.2, 2.0], index=list("abcde"))
    cs = control_survival(exp, control_threshold=0.5)
    assert cs.n_names == 5
    assert cs.n_control == 3      # |−0.1|, |0|, |0.2| <= 0.5
    assert cs.n_high == 2         # |−2|, |2| > 0.5
    assert cs.control_fraction == pytest.approx(0.6)


def test_whole_tape_shock_leaves_no_controls():
    # §238: the channel moves everyone -> all highly exposed -> thin/zero controls -> down-weight.
    exp = pd.Series([2.0, -2.1, 1.8, -1.9, 2.2], index=list("abcde"))
    cs = control_survival(exp, control_threshold=0.5)
    assert cs.n_control == 0
    assert cs.control_fraction == 0.0


# --- the panel: PIT hold semantics ------------------------------------------


def _const_exposure_panel(betas: pd.Series, index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        np.tile(betas.to_numpy(), (len(index), 1)), index=index, columns=betas.index)


def test_weights_are_zero_outside_drift_windows_and_use_lagged_exposure():
    index = pd.date_range("2025-01-01", periods=20, freq="D").date
    betas = pd.Series(np.linspace(-2, 2, 6), index=[f"S{i}" for i in range(6)])
    panel = _const_exposure_panel(betas, index)
    ev = EventSpec("E1", index[10], "fx", +1)
    w, diags = differential_exposure_weights(
        {"fx": panel}, [ev], drift_window=3, quantile=0.3, exposure_lag=1)
    # nonzero only on the 3-day hold [10, 11, 12]
    nz = w.abs().sum(axis=1)
    assert (nz.iloc[10:13] > 0).all()
    held = set(range(10, 13))
    assert all(nz.iloc[i] == 0 for i in range(20) if i not in held)
    assert diags[0].event_id == "E1" and diags[0].n_names == 6


def test_event_too_early_for_lag_is_skipped_cleanly():
    index = pd.date_range("2025-01-01", periods=20, freq="D").date
    betas = pd.Series(np.linspace(-2, 2, 6), index=[f"S{i}" for i in range(6)])
    panel = _const_exposure_panel(betas, index)
    ev = EventSpec("E0", index[0], "fx", +1)  # pos 0, lag 1 -> no prior exposure
    w, diags = differential_exposure_weights({"fx": panel}, [ev], exposure_lag=1)
    assert (w.abs().sum().sum() == 0.0)
    assert diags[0].n_names == 0  # skipped, not fabricated


# --- the exit-gate self-test: planted edge promoted, null rejected ----------


def _planted_world(*, drift: float, shuffle_returns: bool, seed: int = 0):
    """A world where, on each event's entry day, return_i = drift * beta_i (+ tiny noise) and every
    other day is pure noise. The high-minus-low exposure spread captures the drift; momentum /
    own-event / market-beta baselines can't (the move is concentrated at entry, betas are symmetric
    so the market mean ≈ 0). ``shuffle_returns`` breaks the exposure↔drift link -> a null."""
    rng = np.random.default_rng(seed)
    T, N = 320, 40
    index = pd.date_range("2025-01-01", periods=T, freq="B").date
    names = [f"S{i}" for i in range(N)]
    betas = pd.Series(np.linspace(-2.0, 2.0, N), index=names)
    panel = _const_exposure_panel(betas, index)

    event_pos = list(range(20, T - 10, 11))  # ~25 well-separated events
    events = [EventSpec(f"E{k}", index[p], "fx", +1) for k, p in enumerate(event_pos)]

    drive = betas.sample(frac=1.0, random_state=seed).set_axis(names) if shuffle_returns else betas
    rets = pd.DataFrame(rng.normal(0, 0.002, size=(T, N)), index=index, columns=names)
    for p in event_pos:
        rets.iloc[p] += drift * drive.to_numpy()  # the concentrated event-day drift
    return panel, events, rets


def test_planted_exposure_drift_is_promoted():
    panel, events, rets = _planted_world(drift=0.02, shuffle_returns=False)
    w, _ = differential_exposure_weights({"fx": panel}, events, drift_window=1, exposure_lag=1)
    verdict = evaluate_candidate(
        w, rets, n_trials=1, returns_for_baselines=rets, book=RESEARCH, capacity_floor=0.0)
    assert verdict.beats_baselines, verdict.summary()
    assert verdict.dsr.passes, verdict.summary()
    assert verdict.promoted, verdict.summary()


def test_shuffled_exposure_null_is_rejected():
    panel, events, rets = _planted_world(drift=0.02, shuffle_returns=True)
    w, _ = differential_exposure_weights({"fx": panel}, events, drift_window=1, exposure_lag=1)
    verdict = evaluate_candidate(
        w, rets, n_trials=1, returns_for_baselines=rets, book=RESEARCH, capacity_floor=0.0)
    assert not verdict.promoted, verdict.summary()

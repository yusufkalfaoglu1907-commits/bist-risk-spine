"""M5 residual stat-arb — the signal's pure machinery (BUILD_PLAN.md M5).

The runner-level exit gate (venue-feasible survival, DSR, capacity, registry) is exercised in
test_m5_exit_gate.py against the real L2 store. This file pins the *construction* on synthetic
worlds where the answer is known by design:

  - **the peer-relative spread beats naïve own-name reversion** — a world built so the residual
    is a non-reverting common sector move plus a daily-*reversing* idiosyncratic part. Fading the
    raw residual (baseline (c)) is polluted by the common move; fading the peer-*relative* spread
    strips it and reverts only the part that actually reverts → higher Sharpe. This is the whole
    reason the candidate is allowed to exist;
  - **no edges ⇒ no positions** (the NO-GO posture is flat, never a fabricated bet);
  - **no lookahead** — a weight at t is invariant to residuals at/after t;
  - **edge_matrix / comove_predicted** reconcile to a hand computation.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from tmkg.signals.backtest import RESEARCH, run_book
from tmkg.signals.promotion import baseline_own_factor_event
from tmkg.signals.statarb import (
    StatArbParams,
    build_edge_schedule,
    comove_predicted,
    edge_matrix,
    residual_spread,
    statarb_signal,
    statarb_weights,
)

N_PER_SECTOR = 6
N_SECTORS = 4
T = 600


def _sectors() -> dict[str, str]:
    out = {}
    for s in range(N_SECTORS):
        for k in range(N_PER_SECTOR):
            out[f"S{s}N{k}"] = f"SEC{s}"
    return out


def _world(seed: int = 7, reversal: float = 0.4, g_scale: float = 3.0):
    """Residual panel = common (non-reverting) sector move g + daily-reversing idiosyncratic e.

    e_i,t = −reversal·e_i,{t−1} + η (negative autocorrelation ⇒ fading e earns); g is a large iid
    common-per-sector move that cancels in a peer-relative spread but pollutes an own-name z."""
    rng = np.random.default_rng(seed)
    sectors = _sectors()
    syms = list(sectors)
    idx = [dt.date(2023, 1, 2) + dt.timedelta(days=i) for i in range(T)]
    sec_of = {s: sectors[s] for s in syms}
    uniq_sec = sorted(set(sectors.values()))
    g = {sec: rng.normal(0.0, g_scale, size=T) for sec in uniq_sec}      # common, non-reverting
    e = np.zeros((T, len(syms)))
    eta = rng.normal(0.0, 1.0, size=(T, len(syms)))
    for t in range(1, T):
        e[t] = -reversal * e[t - 1] + eta[t]
    R = np.zeros((T, len(syms)))
    for j, s in enumerate(syms):
        R[:, j] = g[sec_of[s]] + e[:, j]
    panel = pd.DataFrame(R, index=idx, columns=syms)
    return panel, sectors


def test_edge_matrix_and_comove_predicted_hand_check():
    syms = ["A", "B", "C"]
    edges = pd.DataFrame({"src": ["A", "A"], "dst": ["B", "C"], "corr": [0.8, -0.5]})
    A = edge_matrix(edges, syms)
    assert A[0, 1] == 0.8 and A[1, 0] == 0.8        # symmetric
    assert A[0, 2] == -0.5 and A[2, 0] == -0.5
    assert A[0, 0] == 0.0                            # zero diagonal (no self-prediction)
    # r̂_A = (0.8·r_B + (−0.5)·r_C) / (0.8 + 0.5)
    R = np.array([[1.0, 2.0, 4.0]])                  # rows = time, cols = A,B,C
    pred = comove_predicted(R, A)
    expected_A = (0.8 * 2.0 + (-0.5) * 4.0) / (0.8 + 0.5)
    assert np.isclose(pred[0, 0], expected_A)


def test_peerless_name_has_no_spread():
    syms = ["A", "B", "C"]
    edges = pd.DataFrame({"src": ["A"], "dst": ["B"], "corr": [0.7]})   # C has no edge
    A = edge_matrix(edges, syms)
    pred = comove_predicted(np.array([[1.0, 2.0, 3.0]]), A)
    assert np.isnan(pred[0, 2])                      # peerless ⇒ NaN, never a fabricated 0


def test_no_edges_means_flat_book():
    panel, _ = _world()
    # an empty schedule => spread all-NaN => weights all zero (no fabricated bet)
    spread = residual_spread(panel, [])
    assert spread.isna().all().all()
    w = statarb_weights(panel, {}, StatArbParams(edge_window=120, refit_step=60, z_lookback=40,
                                                 min_obs=40))
    # with no sector mapping there are no within-sector candidate pairs => no edges => flat
    assert np.allclose(w.fillna(0.0).to_numpy(), 0.0)


def test_signal_is_shifted_no_lookahead():
    panel, sectors = _world()
    sched = build_edge_schedule(panel, sectors, edge_window=120, refit_step=60, min_obs=40)
    sig = statarb_signal(panel, sched, z_lookback=40)
    # first row must be NaN (shift(1)) — a weight can never use its own day's residual
    assert sig.iloc[0].isna().all()

    # perturbing residuals at/after date t leaves the signal strictly before t unchanged
    t = 400
    perturbed = panel.copy()
    perturbed.iloc[t:] += 5.0
    sig2 = statarb_signal(perturbed, sched, z_lookback=40)
    pd.testing.assert_frame_equal(sig.iloc[:t], sig2.iloc[:t])


def test_peer_relative_beats_own_name_reversion():
    """The reason the candidate may exist: stripping the common move lifts the reversion Sharpe
    above the naïve own-name fade (baseline (c)) — same world, same book, no costs."""
    panel, sectors = _world(seed=11)
    sched = build_edge_schedule(panel, sectors, edge_window=150, refit_step=60, min_obs=60,
                                min_abs_corr=0.0)
    # the rolling refit must actually find surviving within-sector edges
    assert max(s.n_edges for s in sched) > 0, "no edges discovered — world/params mis-specified"

    cand_w = statarb_weights(panel, sectors,
                             StatArbParams(edge_window=150, refit_step=60, z_lookback=40,
                                           min_obs=60), schedule=sched)
    own_w = baseline_own_factor_event(panel, lookback=40, z_threshold=0.0)

    fwd = panel  # weight.loc[t] (info <= t-1) earns the residual return at t
    cand = run_book(cand_w, fwd, book=RESEARCH)
    own = run_book(own_w, fwd, book=RESEARCH)
    assert cand.net_sharpe > own.net_sharpe, (cand.net_sharpe, own.net_sharpe)
    assert cand.net_sharpe > 0

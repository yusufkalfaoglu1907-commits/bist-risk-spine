"""M5 exit-gate self-test — the residual-stat-arb runner, end to end (BUILD_PLAN.md M5).

The real-data verdict (NO-GO: a small frictionless edge that does not survive the venue-feasible
book) is recorded in BUILD_LOG / data/cache. This file pins the *runner wiring* deterministically
on a synthetic L2 store where the answer is known by construction, so a regression in the
plumbing (PIT reads, purged walk-forward selection, the three books, registry round-trip,
residual_corr landing) is caught without depending on the live store:

  - a **strong, low-turnover** peer-relative reversion edge is **promoted** through the full
    runner — beats the baseline ladder, clears DSR/PBO, survives venue-feasible costs;
  - the **same world with shuffled labels** (forward returns permuted) is **rejected**;
  - all three books produce output, the verdict round-trips into ``signal_registry`` and reads
    back through ``PITAccess`` (knowledge_date honored), and the **filtered** residual-corr
    snapshot lands in L2 (never the dense matrix).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess
from tmkg.signals.statarb import StatArbParams

N_SEC, PER_SEC, T = 4, 8, 700
PHI_G, PHI_U = 0.9, 0.7        # sector-common (smooth) vs idiosyncratic (reverting) persistence
K = 0.9                        # strength of the idiosyncratic reversion that the signal earns


def _ou(rng, phi, shape):
    x = np.zeros(shape)
    for t in range(1, shape[0]):
        x[t] = phi * x[t - 1] + np.sqrt(1 - phi * phi) * rng.normal(size=shape[1])
    return x


def _synthetic_world(seed=3):
    """residual_i = g_sector (smooth, common ⇒ within-sector edges) + u_i (reverting, idiosyncratic);
    earned return reverts u with strength K. Fading the *peer-relative* part (u) earns; the common
    g cancels in the dollar-neutral book — a strong, low-turnover edge that clears costs."""
    rng = np.random.default_rng(seed)
    syms, sec_of = [], {}
    for s in range(N_SEC):
        for k in range(PER_SEC):
            sym = f"S{s}N{k}"
            syms.append(sym)
            sec_of[sym] = f"SEC{s}"
    n = len(syms)
    idx = [dt.date(2023, 1, 2) + dt.timedelta(days=i) for i in range(T)]

    g = {f"SEC{s}": _ou(rng, PHI_G, (T, 1))[:, 0] for s in range(N_SEC)}
    u = _ou(rng, PHI_U, (T, n))
    resid = np.zeros((T, n))
    ret = np.zeros((T, n))
    for j, sym in enumerate(syms):
        gj = g[sec_of[sym]]
        resid[:, j] = 0.9 * gj + 1.0 * u[:, j]               # within-sector corr via g
        # earned return: the reversion of yesterday's idiosyncratic dislocation + common + noise
        rev = np.zeros(T)
        rev[1:] = -K * u[:-1, j]
        ret[:, j] = 0.012 * (rev + 0.3 * gj + 0.2 * rng.normal(size=T))
    resid_df = pd.DataFrame(resid, index=idx, columns=syms)
    ret_df = pd.DataFrame(ret, index=idx, columns=syms)
    sectors = sec_of
    return resid_df, ret_df, sectors, idx


def _land(store: L2Store, resid_df, ret_df, idx):
    store.bootstrap_schema()
    kd = idx  # knowledge_date = bar_date (known same day) -> visible at any as_of >= bar_date
    rows_r = []
    rows_t = []
    for j, sym in enumerate(resid_df.columns):
        for i, d in enumerate(idx):
            rows_r.append({"symbol": sym, "bar_date": d, "residual": float(resid_df.iat[i, j]),
                           "strip_order": "test", "universe_class": "operating",
                           "knowledge_date": kd[i]})
            rows_t.append({"symbol": sym, "bar_date": d, "ret_usd": float(ret_df.iat[i, j]),
                           "ret_real_try": None, "ret_nominal_try": None,
                           "limit_lock_adj": False, "knowledge_date": kd[i]})
    store.write_parquet("residuals", pd.DataFrame(rows_r))
    store.write_parquet("total_returns", pd.DataFrame(rows_t))


def _grid():
    """A small honest grid (edge_window small so edges exist early in the T=700 sample)."""
    out = []
    for accum_window in (1, 5):
        for z_threshold in (0.0, 1.0):
            for min_abs_corr in (0.0, 0.2):
                out.append(StatArbParams(edge_window=150, refit_step=60, min_obs=60,
                                         accum_window=accum_window, z_threshold=z_threshold,
                                         min_abs_corr=min_abs_corr, z_lookback=60))
    return out


@pytest.fixture(scope="module")
def world():
    return _synthetic_world()


def test_known_good_is_promoted_and_lands(tmp_path, world):
    from tmkg.signals.run_statarb import run_m5_statarb

    resid_df, ret_df, sectors, idx = world
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    _land(store, resid_df, ret_df, idx)

    rep = run_m5_statarb(
        store, as_of=idx[-1], sectors=sectors, grid=_grid(), top_by_coverage=None,
        panel_min_obs=120, n_splits=4, purge=5, embargo=5,
        report_dir=tmp_path, write_l2=True)

    # all three books produced output
    assert set(rep["books"]) == {"research", "venue_feasible", "stress"}
    # the edge is strong and low-turnover by construction -> promoted through the full gate
    assert rep["verdict"]["beats_baselines"], rep["verdict"]
    assert rep["verdict"]["dsr"]["passes"], rep["verdict"]["dsr"]
    assert rep["exit_gate"]["promoted"], rep["exit_gate"]
    # venue-feasible (with costs) is the binding book — it must be the one that survives
    assert rep["books"]["venue_feasible"]["net_sharpe"] > 0

    # the filtered residual-corr snapshot landed (and is a *snapshot*, not the dense matrix)
    n_names = len(resid_df.columns)
    rc = store.read_table("residual_corr")
    assert 0 < len(rc) < n_names * (n_names - 1) // 2
    assert bool(rc["fdr_passed"].all())

    # the verdict round-trips into the registry and reads back through PITAccess
    con = store.connect()
    try:
        before = PITAccess(idx[0], l2=con).series("signal_registry")
        after = PITAccess(idx[-1], l2=con).series("signal_registry")
    finally:
        con.close()
    assert before.empty                       # knowledge_date > as_of hidden (no lookahead)
    assert len(after) == 1
    assert bool(after.iloc[0]["promoted"]) is True


def test_shuffled_labels_is_rejected(tmp_path, world):
    from tmkg.signals.run_statarb import run_m5_statarb

    resid_df, ret_df, sectors, idx = world
    rng = np.random.default_rng(99)
    perm = rng.permutation(len(ret_df))
    ret_shuf = pd.DataFrame(ret_df.to_numpy()[perm], index=ret_df.index, columns=ret_df.columns)

    store = L2Store(db_path=tmp_path / "l2_null.duckdb")
    _land(store, resid_df, ret_shuf, idx)
    rep = run_m5_statarb(
        store, as_of=idx[-1], sectors=sectors, grid=_grid(), top_by_coverage=None,
        panel_min_obs=120, n_splits=4, purge=5, embargo=5, write_l2=False)
    assert not rep["exit_gate"]["promoted"], rep["verdict"]

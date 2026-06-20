"""Unit tests for the M2 rolling regime-aware beta engine (tmkg.factors.betas).

Synthetic and deterministic. Pins: partial-beta recovery, Ledoit–Wolf stability near
p≈n, the regime-break / no-straddle guarantee (the exit-gate behaviour), and the
no-fabrication contract (§4).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tmkg.factors.betas import LEDOIT_WOLF, OLS, factor_panel, rolling_factor_betas


def _dates(n):
    return [d.date() for d in pd.bdate_range("2024-01-01", periods=n)]


def _factor_long(dates, cols: dict[str, np.ndarray]) -> pd.DataFrame:
    rows = []
    for fname, vals in cols.items():
        for d, v in zip(dates, vals):
            rows.append({"factor": fname, "bar_date": d, "ret": float(v)})
    return pd.DataFrame(rows)


def _stock(dates, ret: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({"bar_date": dates, "ret": ret})


def test_single_factor_recovers_known_beta():
    rng = np.random.default_rng(0)
    n = 80
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    y = 1.5 * mkt  # exact
    out = rolling_factor_betas(
        _stock(dates, y), _factor_long(dates, {"MKT": mkt}),
        symbol="AAA", window=40, method=OLS,
    )
    assert (out["factor"] == "MKT").all()
    assert out["beta"].iloc[-1] == pytest.approx(1.5, abs=1e-9)
    # one beta per eligible end-date: dates indices 39..79 -> 41 rows
    assert len(out) == n - 40 + 1


def test_multifactor_partial_betas_recovered_ols():
    rng = np.random.default_rng(1)
    n = 90
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    fx = rng.normal(0, 0.01, n)
    y = 0.8 * mkt - 0.5 * fx  # exact partial betas
    out = rolling_factor_betas(
        _stock(dates, y), _factor_long(dates, {"MKT": mkt, "FX": fx}),
        symbol="AAA", window=50, method=OLS,
    )
    last = out[out["bar_date"] == out["bar_date"].max()].set_index("factor")["beta"]
    assert last["MKT"] == pytest.approx(0.8, abs=1e-9)
    assert last["FX"] == pytest.approx(-0.5, abs=1e-9)


def test_ledoit_wolf_is_finite_and_recovers_the_beta_vector_near_p_equals_n():
    """With many factors and a short window (p≈n) the raw inverse is unstable; the
    shrunk estimator must stay finite and recover the beta *vector* in aggregate
    (individual partial betas are genuinely noisy at this ratio — that is sampling,
    not a bug, which is exactly why §201 mandates shrinkage before inversion)."""
    rng = np.random.default_rng(2)
    n, k = 50, 8
    dates = _dates(n)
    fnames = [f"F{j}" for j in range(k)]
    F = {f: rng.normal(0, 0.02, n) for f in fnames}
    true = np.linspace(-1.0, 1.0, k)
    y = sum(b * F[f] for b, f in zip(true, F)) + rng.normal(0, 0.001, n)
    out = rolling_factor_betas(
        _stock(dates, y), _factor_long(dates, F),
        symbol="AAA", window=30, method=LEDOIT_WOLF,
    )
    last = out[out["bar_date"] == out["bar_date"].max()].set_index("factor")["beta"]
    est = last.reindex(fnames).to_numpy()
    assert np.isfinite(est).all()
    # the estimated beta vector tracks the true one (sign/rank), even if noisy per-factor
    assert np.corrcoef(est, true)[0, 1] > 0.6


def test_betas_break_across_a_regime_boundary_and_do_not_straddle():
    """The exit-gate behaviour: a window is confined to its end date's regime, so the
    beta is ~b1 well inside regime A, ~b2 well inside regime B, and there is NO estimate
    on dates whose trailing window would straddle the break."""
    rng = np.random.default_rng(3)
    n = 100
    dates = _dates(n)
    boundary = dates[50]
    mkt = rng.normal(0, 0.02, n)
    y = np.where(np.array(dates) < boundary, 2.0 * mkt, -1.0 * mkt)
    regime_of = (lambda d: "A" if d < boundary else "B")

    out = rolling_factor_betas(
        _stock(dates, y), _factor_long(dates, {"MKT": mkt}),
        symbol="AAA", window=20, min_obs=20, method=OLS, regime_of=regime_of,
    )
    out = out.assign(reg=out["bar_date"].map(regime_of))

    a = out[out["reg"] == "A"]
    b = out[out["reg"] == "B"]
    assert a["beta"].iloc[-1] == pytest.approx(2.0, abs=1e-9)
    assert b["beta"].iloc[-1] == pytest.approx(-1.0, abs=1e-9)
    # every estimate carries its own regime label
    assert (a["regime"] == "A").all() and (b["regime"] == "B").all()
    # no-straddle: the first in-regime-B estimate needs a full 20 obs in B, so the 19
    # dates immediately after the boundary get no beta (window would straddle).
    post = [d for d in dates if d >= boundary]
    assert b["bar_date"].min() == post[19]


def test_min_obs_refuses_thin_windows_no_fabrication():
    rng = np.random.default_rng(4)
    n = 10
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    out = rolling_factor_betas(
        _stock(dates, mkt * 1.2), _factor_long(dates, {"MKT": mkt}),
        symbol="AAA", window=30, min_obs=30, method=OLS,
    )
    assert out.empty  # never a guessed beta on too-few obs


def test_nan_return_rows_are_dropped_not_filled():
    rng = np.random.default_rng(5)
    n = 60
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    y = (1.1 * mkt).astype(float)
    y[5] = np.nan  # a missing stock return
    out = rolling_factor_betas(
        _stock(dates, y), _factor_long(dates, {"MKT": mkt}),
        symbol="AAA", window=30, min_obs=25, method=OLS,
    )
    # still recovers the beta from the surviving rows, never invents the missing one
    assert out["beta"].iloc[-1] == pytest.approx(1.1, abs=1e-9)


def test_knowledge_date_equals_window_end():
    rng = np.random.default_rng(6)
    n = 50
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    out = rolling_factor_betas(
        _stock(dates, mkt), _factor_long(dates, {"MKT": mkt}),
        symbol="AAA", window=30, method=OLS, universe_class="operating",
    )
    assert (out["knowledge_date"] == out["bar_date"]).all()  # PIT-honest
    assert (out["universe_class"] == "operating").all()


def test_factor_panel_preserves_first_seen_order():
    dates = _dates(3)
    long = _factor_long(dates, {"ZED": np.ones(3), "ALPHA": np.zeros(3)})
    panel = factor_panel(long)
    assert list(panel.columns) == ["ZED", "ALPHA"]  # not alphabetised


def test_unknown_method_refused():
    dates = _dates(40)
    mkt = np.random.default_rng(7).normal(0, 0.02, 40)
    with pytest.raises(ValueError):
        rolling_factor_betas(
            _stock(dates, mkt), _factor_long(dates, {"MKT": mkt}),
            symbol="AAA", window=30, method="bogus",
        )

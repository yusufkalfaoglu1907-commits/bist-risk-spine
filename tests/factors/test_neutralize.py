"""Unit tests for the M2 neutralization ladder (tmkg.factors.neutralize)."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tmkg.factors.neutralize import (
    neutralize_window,
    rolling_residuals,
    strip_residual,
)


def _dates(n):
    return [d.date() for d in pd.bdate_range("2024-01-01", periods=n)]


def _factor_long(dates, cols):
    rows = []
    for fname, vals in cols.items():
        for d, v in zip(dates, vals):
            rows.append({"factor": fname, "bar_date": d, "ret": float(v)})
    return pd.DataFrame(rows)


def test_residual_orthogonal_to_every_stripped_factor():
    rng = np.random.default_rng(0)
    n = 200
    mkt = rng.normal(0, 1, n)
    fx = rng.normal(0, 1, n)
    y = 1.3 * mkt - 0.7 * fx + rng.normal(0, 0.5, n)
    F = np.column_stack([mkt, fx])
    r = strip_residual(y, F)
    # orthogonal to each stripped factor (the M2 exit-gate guarantee)
    for j in range(F.shape[1]):
        fc = F[:, j] - F[:, j].mean()
        corr = np.corrcoef(r, fc)[0, 1]
        assert abs(corr) < 1e-10


def test_residual_equals_joint_ols_residual():
    rng = np.random.default_rng(1)
    n = 150
    F = rng.normal(0, 1, (n, 3))
    y = F @ np.array([0.5, -1.0, 0.25]) + rng.normal(0, 0.3, n)
    r = strip_residual(y, F)
    # joint OLS with intercept
    X = np.column_stack([np.ones(n), F])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    ols_resid = y - X @ beta
    assert np.allclose(r, ols_resid, atol=1e-9)


def test_ladder_order_drives_attribution():
    """When two factors share variance, the earlier rung claims it."""
    rng = np.random.default_rng(2)
    n = 500
    base = rng.normal(0, 1, n)
    a = base + 0.1 * rng.normal(0, 1, n)   # a and b strongly shared
    b = base + 0.1 * rng.normal(0, 1, n)
    y = base + rng.normal(0, 0.2, n)
    F_ab = np.column_stack([a, b])
    F_ba = np.column_stack([b, a])
    _, stages_ab = strip_residual(y, F_ab, return_stages=True)
    _, stages_ba = strip_residual(y, F_ba, return_stages=True)
    # first rung claims the lion's share either way; the second adds little
    assert stages_ab[0] > stages_ab[1]
    assert stages_ba[0] > stages_ba[1]
    # final residual variance is order-invariant (same span), attribution is not
    r_ab = strip_residual(y, F_ab)
    r_ba = strip_residual(y, F_ba)
    assert np.isclose(r_ab @ r_ab, r_ba @ r_ba, rtol=1e-9)


def test_collinear_factor_strips_nothing():
    rng = np.random.default_rng(3)
    n = 100
    f = rng.normal(0, 1, n)
    y = 2.0 * f + rng.normal(0, 0.1, n)
    F = np.column_stack([f, 3.0 * f])  # second column collinear with first
    r, stages = strip_residual(y, F, return_stages=True)
    assert stages[1] == 0.0  # the collinear rung adds no new direction
    # residual same as stripping the single factor
    r1 = strip_residual(y, f.reshape(-1, 1))
    assert np.allclose(r, r1, atol=1e-12)


def test_neutralize_window_orthogonality_on_frame():
    rng = np.random.default_rng(4)
    dates = _dates(120)
    mkt = rng.normal(0, 0.02, 120)
    fx = rng.normal(0, 0.01, 120)
    panel = pd.DataFrame({"market": mkt, "fx": fx}, index=dates)
    y = pd.Series(0.9 * mkt - 0.4 * fx + rng.normal(0, 0.005, 120), index=dates)
    res = neutralize_window(y, panel, order=["market", "fx"])
    assert abs(np.corrcoef(res, panel["market"])[0, 1]) < 1e-10
    assert abs(np.corrcoef(res, panel["fx"])[0, 1]) < 1e-10


def test_rolling_residuals_emit_pit_honest_self_describing_rows():
    rng = np.random.default_rng(5)
    n = 90
    dates = _dates(n)
    mkt = rng.normal(0, 0.02, n)
    y = 1.2 * mkt + rng.normal(0, 0.003, n)
    out = rolling_residuals(
        pd.DataFrame({"bar_date": dates, "ret": y}),
        _factor_long(dates, {"market": mkt}),
        order=["market"], symbol="AAA", window=40, universe_class="operating",
    )
    assert len(out) == n - 40 + 1
    assert (out["knowledge_date"] == out["bar_date"]).all()  # PIT-honest
    assert (out["strip_order"] == "market").all()
    assert (out["universe_class"] == "operating").all()


def test_rolling_residuals_refuse_thin_window():
    dates = _dates(10)
    mkt = np.random.default_rng(6).normal(0, 0.02, 10)
    out = rolling_residuals(
        pd.DataFrame({"bar_date": dates, "ret": mkt}),
        _factor_long(dates, {"market": mkt}),
        order=["market"], symbol="AAA", window=30, min_obs=30,
    )
    assert out.empty  # §4: no residual on too-few obs

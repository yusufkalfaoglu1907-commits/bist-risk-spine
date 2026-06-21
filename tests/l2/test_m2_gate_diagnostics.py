"""m2_gate_diagnostics — reads landed M2 outputs back through PIT and emits the exit-gate
numbers. Offline, synthetic L2 seed; proves the reader plumbs total_returns/residuals/betas
into the pure evaluators and honors the PIT gate (a knowledge_date after as_of is invisible).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tmkg.factors.diagnostics import m2_gate_diagnostics
from tmkg.l2.store import L2Store


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _seed(store: L2Store, n: int = 40):
    dates = [d.date() for d in pd.bdate_range("2025-01-02", periods=n)]
    rng = np.random.default_rng(0)
    # one name, residual = 30% of return variance left -> r2 ~ 0.7
    ret = rng.normal(0, 0.02, n)
    resid = np.sqrt(0.3) * ret  # var(resid)/var(ret) = 0.3 exactly (collinear scaling)
    store.write_parquet("total_returns", pd.DataFrame({
        "symbol": "AAA", "bar_date": dates, "ret_usd": ret,
        "ret_real_try": np.nan, "ret_nominal_try": np.nan,
        "limit_lock_adj": False, "knowledge_date": dates,
    }))
    store.write_parquet("residuals", pd.DataFrame({
        "symbol": "AAA", "bar_date": dates, "residual": resid,
        "strip_order": "XU100>USDTRY", "universe_class": "operating",
        "knowledge_date": dates,
    }))
    # betas straddling the 2025 shock: MKT flips +2 -> -1 across the boundary
    bdates_before = [d.date() for d in pd.bdate_range("2025-02-03", periods=8)]
    bdates_after = [d.date() for d in pd.bdate_range("2025-03-20", periods=8)]
    store.write_parquet("betas", pd.DataFrame({
        "symbol": "AAA", "factor": "XU100",
        "bar_date": bdates_before + bdates_after,
        "beta": list(2.0 + rng.normal(0, 0.01, 8)) + list(-1.0 + rng.normal(0, 0.01, 8)),
        "method": "ols", "window": 30,
        "regime": ["orthodox_turn_2023"] * 8 + ["imamoglu_shock_2025"] * 8,
        "universe_class": "operating",
        "knowledge_date": bdates_before + bdates_after,
    }))
    return dates


def test_gate_diagnostics_reports_variance_share_and_regime_break(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    # as_of after all post-shock betas (dated through ~2025-03 end) so both regimes are visible
    rep = m2_gate_diagnostics(store, as_of=date(2025, 4, 30), break_factors=["XU100"],
                              min_obs=20, min_per_regime=5)

    by_class = {r["universe_class"]: r for r in rep["variance_share_by_class"]}
    assert "operating" in by_class
    assert by_class["operating"]["r2_median"] == __import__("pytest").approx(0.7, abs=1e-6)

    breaks = {r["factor"]: r for r in rep["regime_break"]["by_factor"]}
    assert "XU100" in breaks
    assert breaks["XU100"]["median_shift"] == __import__("pytest").approx(3.0, abs=0.1)
    assert breaks["XU100"]["median_break_ratio"] > 20  # break dominates the noise floor


def test_gate_diagnostics_pit_gate_hides_future_betas(tmp_path):
    store = _store(tmp_path)
    dates = _seed(store)
    # as_of before the post-shock betas were known -> only the pre-shock side is visible,
    # so no two-sided break can be formed (the reader must not see future knowledge).
    rep = m2_gate_diagnostics(store, as_of=date(2025, 2, 28), break_factors=["XU100"])
    assert rep["regime_break"]["by_factor"] == []  # post-shock betas invisible at this as_of

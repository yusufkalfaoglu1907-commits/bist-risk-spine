"""Regression: the neutralization ladder must strip *real* factor names in §200 rung
order — the case the old role-keyed default (``order=("market","fx",...)``) silently got
wrong, because those role strings match no real ``factors.factor`` value (XU100, USDTRY…),
so the strip order came out empty and nothing was neutralized.

Seeds factor levels under their real registry names through L2, then drives the M2
residual builder with ``order=None`` (registry-derived) and asserts the recorded
``strip_order`` is the registry rung order over those names — and that a name fully
explained by the factors neutralizes to ~0.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tmkg.ingest.pipeline import build_residuals, run_m2_factor_model
from tmkg.l2.store import L2Store

_TRUE = {"XU100": 1.2, "USDTRY": -0.7}  # the name's true partial betas


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _seed(store: L2Store, n: int = 70, seed: int = 1):
    rng = np.random.default_rng(seed)
    dates = [d.date() for d in pd.bdate_range("2024-01-02", periods=n)]  # one regime
    r_m = np.concatenate([[np.nan], rng.normal(0, 0.02, n - 1)])
    r_f = np.concatenate([[np.nan], rng.normal(0, 0.01, n - 1)])

    def _levels(rets, factor):
        lvl = [100.0]
        for r in rets[1:]:
            lvl.append(lvl[-1] * (1.0 + r))
        return pd.DataFrame({
            "factor": factor, "bar_date": dates, "value": lvl,
            "ret": None, "knowledge_date": dates, "source": "test",
        })

    # land USDTRY *before* XU100 to prove the ladder re-orders by rung, not insertion order
    store.write_parquet("factors", _levels(r_f, "USDTRY"))
    store.write_parquet("factors", _levels(r_m, "XU100"))

    ret_usd = _TRUE["XU100"] * r_m + _TRUE["USDTRY"] * r_f
    store.write_parquet("total_returns", pd.DataFrame({
        "symbol": "AAA", "bar_date": dates, "ret_usd": ret_usd,
        "ret_real_try": np.nan, "ret_nominal_try": np.nan,
        "limit_lock_adj": False, "knowledge_date": dates,
    }))
    store.write_parquet("universe_membership", pd.DataFrame({
        "symbol": ["AAA"], "universe": ["listed"], "universe_class": ["operating"],
        "valid_from": [pd.Timestamp("2020-01-01").date()], "valid_to": [None],
        "knowledge_date": [pd.Timestamp("2020-01-01").date()], "source": ["test"],
    }))
    return dates


_SPECS = {"XU100": "simple", "USDTRY": "simple"}


def test_strip_order_is_registry_rung_order_over_real_names(tmp_path):
    store = _store(tmp_path)
    dates = _seed(store)
    # order=None -> derive from the registry. market (XU100) must precede fx (USDTRY),
    # even though USDTRY was landed first.
    summary = build_residuals(store, "AAA", as_of=dates[-1], specs=_SPECS,
                              order=None, window=40)
    assert summary["n_residuals"] > 0
    assert summary["strip_order"] == "XU100>USDTRY"  # rung order, not insertion order

    con = store.connect()
    try:
        res = con.execute("SELECT residual FROM residuals").df()
    finally:
        con.close()
    # AAA is entirely the two factors -> neutralized residual ~0 (proves the strip ran)
    assert np.abs(res["residual"].to_numpy()).max() < 1e-9


def test_run_m2_with_default_order_uses_registry(tmp_path):
    import tmkg.config as config

    store = _store(tmp_path)
    dates = _seed(store)
    p = config.REPO_ROOT / "data" / "cache" / "m2_factor_model_report.json"
    existed = p.exists()
    try:
        report = run_m2_factor_model(store, symbols=["AAA"], as_of=dates[-1],
                                     specs=_SPECS, order=None, window=40, method="ols")
        assert report["ladder"] == "XU100>USDTRY"
        assert report["residuals"][0]["strip_order"] == "XU100>USDTRY"
    finally:
        if not existed and p.exists():
            p.unlink()  # synthetic run: don't leave a fake audit report behind

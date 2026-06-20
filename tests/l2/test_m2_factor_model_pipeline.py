"""M2 factor-model pipeline — end-to-end through L2 + PIT (offline, synthetic).

Proves the compute path the pure-engine tests cannot:
  1. factor *returns* are derived on read from L2 factor *levels* (the BUILD_LOG
     2026-06-20 decision), through PITAccess;
  2. rolling betas and neutralized residuals survive a real DuckDB bitemporal
     round-trip and land in L2 ``betas`` / ``residuals``;
  3. the PIT gate holds — a read at as_of = D yields no beta/residual whose
     knowledge_date is after D, and a recovered beta uses only data knowable then;
  4. each row carries the name's PIT-known ``universe_class``.

Synthetic but exact: stock USD returns are a known linear combination of two factor
returns, so the OLS betas must recover the coefficients to machine precision.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tmkg.ingest.pipeline import (
    build_betas,
    build_factor_return_panel,
    build_residuals,
    factor_coverage,
    run_m2_factor_model,
)
from tmkg.l2.store import L2Store

_TRUE = {"market": 1.5, "fx": -0.5}


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _seed(store: L2Store, n: int = 70, seed: int = 0):
    """Land factor levels (cumulated from known returns) + matching stock total returns
    + a universe_class row. Returns the date axis."""
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

    store.write_parquet("factors", _levels(r_m, "market"))
    store.write_parquet("factors", _levels(r_f, "fx"))

    ret_usd = _TRUE["market"] * r_m + _TRUE["fx"] * r_f  # exact; NaN on day 0
    store.write_parquet("total_returns", pd.DataFrame({
        "symbol": "AAA", "bar_date": dates, "ret_usd": ret_usd,
        "ret_real_try": np.nan, "ret_nominal_try": np.nan,
        "limit_lock_adj": False, "knowledge_date": dates,
    }))
    store.write_parquet("universe_membership", pd.DataFrame({
        "symbol": ["AAA"], "universe": ["listed"], "universe_class": ["operating"],
        "valid_from": [date(2020, 1, 1)], "valid_to": [None],
        "knowledge_date": [date(2020, 1, 1)], "source": ["test"],
    }))
    return dates


_SPECS = {"market": "simple", "fx": "simple"}


def test_factor_panel_derived_from_levels_through_pit(tmp_path):
    store = _store(tmp_path)
    dates = _seed(store)
    panel = build_factor_return_panel(store, as_of=dates[-1], specs=_SPECS)
    assert set(panel["factor"].unique()) == {"market", "fx"}
    assert panel["ret"].notna().all()  # the NaN first-obs is dropped


def test_betas_recover_known_coefficients_and_land(tmp_path):
    store = _store(tmp_path)
    dates = _seed(store)
    summary = build_betas(store, "AAA", as_of=dates[-1], specs=_SPECS,
                          window=40, method="ols")
    assert summary["n_betas"] > 0
    assert summary["universe_class"] == "operating"

    con = store.connect()
    try:
        betas = con.execute(
            "SELECT factor, beta FROM betas WHERE bar_date = (SELECT max(bar_date) FROM betas)"
        ).df()
    finally:
        con.close()
    got = betas.set_index("factor")["beta"]
    assert got["market"] == pytest.approx(_TRUE["market"], abs=1e-9)
    assert got["fx"] == pytest.approx(_TRUE["fx"], abs=1e-9)


def test_residuals_land_and_are_near_zero_for_a_pure_factor_name(tmp_path):
    """AAA's return is *entirely* explained by the two factors, so its neutralized
    residual is ~0 — there is no genuine idiosyncratic component to find."""
    store = _store(tmp_path)
    dates = _seed(store)
    summary = build_residuals(store, "AAA", as_of=dates[-1], specs=_SPECS,
                              order=("market", "fx"), window=40)
    assert summary["n_residuals"] > 0
    assert summary["strip_order"] == "market>fx"

    con = store.connect()
    try:
        res = con.execute("SELECT residual FROM residuals").df()
    finally:
        con.close()
    assert np.abs(res["residual"].to_numpy()).max() < 1e-9


def test_no_factor_silently_dropped(tmp_path):
    """Exit-gate rule: a configured factor with no series must be surfaced (and, under
    require_all, refused) — never silently omitted to fit a thinner model."""
    store = _store(tmp_path)
    dates = _seed(store)
    specs = {"market": "simple", "fx": "simple", "brent": "simple"}  # brent never landed

    panel = build_factor_return_panel(store, as_of=dates[-1], specs=specs)
    present, missing = factor_coverage(panel, specs)
    assert present == ["market", "fx"]
    assert missing == ["brent"]

    # require_all turns the silent drop into a loud failure
    with pytest.raises(ValueError, match="brent"):
        build_factor_return_panel(store, as_of=dates[-1], specs=specs, require_all=True)


def test_run_reports_factor_coverage(tmp_path):
    import tmkg.config as config

    store = _store(tmp_path)
    dates = _seed(store)
    specs = {"market": "simple", "fx": "simple", "brent": "simple"}
    report_path = config.REPO_ROOT / "data" / "cache" / "m2_factor_model_report.json"
    existed = report_path.exists()
    try:
        report = run_m2_factor_model(store, symbols=["AAA"], as_of=dates[-1],
                                     specs=specs, order=("market", "fx", "brent"),
                                     window=40, method="ols")
        assert report["missing_factors"] == ["brent"]
        assert set(report["present_factors"]) == {"market", "fx"}
        # the residual strip_order reflects only the factors actually present
        assert report["residuals"][0]["strip_order"] == "market>fx"
    finally:
        if not existed and report_path.exists():
            report_path.unlink()  # synthetic run: don't leave a fake audit report behind


def test_pit_gate_holds_on_built_betas(tmp_path):
    store = _store(tmp_path)
    dates = _seed(store)
    cut = dates[50]
    build_betas(store, "AAA", as_of=cut, specs=_SPECS, window=40, method="ols")
    con = store.connect()
    try:
        mx = con.execute("SELECT max(knowledge_date), max(bar_date) FROM betas").fetchone()
    finally:
        con.close()
    assert mx[0] <= cut and mx[1] <= cut  # nothing learned after the as_of leaked in

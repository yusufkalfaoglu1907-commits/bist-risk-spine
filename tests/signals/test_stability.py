"""M3 residual-network stability + GO/NO-GO decision (tmkg.signals.stability) — synthetic.

The kill-experiment's teeth: a **persistent latent block** must produce a GO (structure recurs
far beyond chance), and **structureless residuals** must produce a NO-GO (the pillar fails
honestly here, not in a backtest). Plus exact known-answers for the component metrics.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.stability import (
    decide_gate,
    jaccard,
    random_overlap_jaccard,
    rolling_stability,
    rolling_window_bounds,
    stability_summary,
)


def _dates(n, start=dt.date(2023, 1, 2)):
    return [start + dt.timedelta(days=i) for i in range(n)]


# --- component metrics ------------------------------------------------------


def test_window_bounds_non_overlapping_by_default():
    b = rolling_window_bounds(300, window=100, step=100)
    assert b == [(0, 100), (100, 200), (200, 300)]


def test_window_bounds_overlapping():
    b = rolling_window_bounds(250, window=100, step=50)
    assert b == [(0, 100), (50, 150), (100, 200), (150, 250)]


def test_jaccard_basic_and_empty():
    assert jaccard({1, 2, 3}, {2, 3, 4}) == pytest.approx(2 / 4)
    assert jaccard(set(), set()) == 1.0  # vacuously identical
    assert jaccard({1}, set()) == 0.0


def test_random_overlap_jaccard_matches_formula():
    # k_a=10, k_b=10, N=1000 -> E[inter]=0.1, E[union]=19.9 -> ~0.005
    assert random_overlap_jaccard(10, 10, 1000) == pytest.approx(0.1 / 19.9, rel=1e-9)
    assert random_overlap_jaccard(0, 0, 1000) == 0.0


# --- the known-good: a persistent block survives -> GO ----------------------


def _block_panel(n, n_block=6, n_noise=8, seed=0, block_strength=0.85):
    """A panel where one within-sector block shares a persistent latent factor across the
    whole span, plus independent noise names. Two sectors so the within-sector restriction
    has cross-sector pairs to (correctly) exclude."""
    rng = np.random.default_rng(seed)
    latent = rng.standard_normal(n)
    cols, sectors = {}, {}
    for i in range(n_block):
        cols[f"BLK{i}"] = block_strength * latent + np.sqrt(1 - block_strength**2) * rng.standard_normal(n)
        sectors[f"BLK{i}"] = "bank"
    for i in range(n_noise):
        cols[f"NZ{i}"] = rng.standard_normal(n)
        sectors[f"NZ{i}"] = "noise"
    return pd.DataFrame(cols, index=_dates(n)), sectors


def test_persistent_block_is_GO():
    panel, sectors = _block_panel(480, seed=1)
    roll = rolling_stability(panel, sectors=sectors, window=120, min_obs=80, alpha=0.05)
    summ = stability_summary(roll)
    decision = decide_gate(summ)
    assert decision["decision"] == "GO", (summ, decision)
    assert summ["median_jaccard"] >= 0.10
    assert summ["median_lift"] >= 3.0 or summ["n_inf_lift"] > 0
    # weight rank-stability is reported but does NOT gate (a homogeneous stable block has
    # tie-noise ranks; vetoing it would be wrong) — it appears under diagnostics.
    assert "weight_rank_rho" in decision["diagnostics"]


def test_persistent_block_edges_are_within_block():
    panel, sectors = _block_panel(360, seed=2)
    from tmkg.signals.stability import window_edge_set
    eset, _ = window_edge_set(panel, sectors=sectors, min_obs=80)
    assert eset, "expected some surviving edges in the persistent block"
    for e in eset:
        a, b = tuple(e)
        assert sectors[a] == sectors[b] == "bank"  # only within the linked sector


# --- the known-null: structureless residuals -> NO-GO ----------------------


def test_structureless_residuals_are_NO_GO():
    rng = np.random.default_rng(3)
    n, p = 480, 14
    panel = pd.DataFrame(
        rng.standard_normal((n, p)), index=_dates(n), columns=[f"S{i}" for i in range(p)]
    )
    sectors = {f"S{i}": ("bank" if i % 2 == 0 else "steel") for i in range(p)}
    roll = rolling_stability(panel, sectors=sectors, window=120, min_obs=80, alpha=0.05)
    summ = stability_summary(roll)
    decision = decide_gate(summ)
    assert decision["decision"] == "NO-GO", (summ, decision)


def test_decide_gate_requires_all_gating_checks():
    good = {"n_window_pairs": 4, "median_n_edges": 12.0, "median_lift": 8.0,
            "median_jaccard": 0.4, "median_weight_rank_rho": 0.7}
    assert decide_gate(good)["decision"] == "GO"
    # flip any single GATING check -> NO-GO
    for k, bad in [("median_lift", 1.0), ("median_jaccard", 0.01),
                   ("n_window_pairs", 1), ("median_n_edges", 1.0)]:
        d = {**good, k: bad}
        out = decide_gate(d)
        assert out["decision"] == "NO-GO" and out["failed_checks"], k


def test_decide_gate_rank_rho_does_not_gate():
    # low weight-rank-rho alone must NOT flip a GO -> it is a reported diagnostic only.
    good = {"n_window_pairs": 4, "median_n_edges": 12.0, "median_lift": 8.0,
            "median_jaccard": 0.4, "median_weight_rank_rho": 0.0}
    out = decide_gate(good)
    assert out["decision"] == "GO"
    assert out["diagnostics"]["weight_rank_stable"] is False


def test_decide_gate_nan_metrics_fail_closed():
    summ = {"n_window_pairs": 4, "median_n_edges": 12.0, "median_lift": float("nan"),
            "median_jaccard": float("nan"), "median_weight_rank_rho": float("nan")}
    assert decide_gate(summ)["decision"] == "NO-GO"


def test_rolling_stability_too_few_windows_is_empty():
    panel, sectors = _block_panel(150, seed=4)
    roll = rolling_stability(panel, sectors=sectors, window=120)  # only 1 window fits
    assert roll.empty
    assert decide_gate(stability_summary(roll))["decision"] == "NO-GO"

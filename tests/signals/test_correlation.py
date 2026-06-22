"""M3 residual-correlation engine (tmkg.signals.correlation) — synthetic, deterministic.

The [STOP] gate must reject noise before it can honestly accept structure, so the core
checks are a **known-null** (independent residuals ⇒ FDR yields ~no edges) paired with a
**known-good** (a planted latent block ⇒ those edges survive). Plus exact known-answers for
the Benjamini–Hochberg procedure and the panel/shrinkage plumbing.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.correlation import (
    benjamini_hochberg,
    fdr_edges,
    pairwise_correlation,
    residual_panel,
    shrunk_residual_correlation,
)


def _dates(n, start=dt.date(2024, 1, 1)):
    return [start + dt.timedelta(days=i) for i in range(n)]


def _long(panel: pd.DataFrame) -> pd.DataFrame:
    """Wide (date × symbol) -> the long L2 residuals shape."""
    return (
        panel.reset_index()
        .melt(id_vars="index", var_name="symbol", value_name="residual")
        .rename(columns={"index": "bar_date"})
        .dropna(subset=["residual"])
    )


# --- residual_panel ---------------------------------------------------------


def test_residual_panel_pivots_and_drops_low_coverage():
    d = _dates(10)
    rows = []
    for i, day in enumerate(d):
        rows.append({"symbol": "AAA", "bar_date": day, "residual": 0.01 * i})
        rows.append({"symbol": "BBB", "bar_date": day, "residual": -0.01 * i})
    # CCC has only 3 obs — below min_obs, must be dropped (not zero-filled)
    for day in d[:3]:
        rows.append({"symbol": "CCC", "bar_date": day, "residual": 0.5})
    panel = residual_panel(pd.DataFrame(rows), min_obs=5)
    assert list(panel.columns) == ["AAA", "BBB"]
    assert "CCC" not in panel.columns
    assert panel.shape == (10, 2)
    assert panel["AAA"].iloc[3] == pytest.approx(0.03)


def test_residual_panel_preserves_symbol_order_and_is_ragged():
    d = _dates(8)
    rows = [{"symbol": "AAA", "bar_date": day, "residual": 1.0} for day in d]
    rows += [{"symbol": "BBB", "bar_date": day, "residual": 2.0} for day in d[2:]]  # ragged
    panel = residual_panel(pd.DataFrame(rows), min_obs=3, symbols=["BBB", "AAA"])
    assert list(panel.columns) == ["BBB", "AAA"]
    assert panel["BBB"].isna().sum() == 2  # ragged NaNs preserved, not filled


def test_residual_panel_requires_columns():
    with pytest.raises(ValueError, match="missing"):
        residual_panel(pd.DataFrame({"symbol": ["A"], "bar_date": [dt.date(2024, 1, 1)]}))


# --- pairwise_correlation ---------------------------------------------------


def test_pairwise_correlation_known_value_and_counts():
    rng = np.random.default_rng(0)
    n = 200
    z = rng.standard_normal(n)
    panel = pd.DataFrame(
        {"AAA": z, "BBB": z + 0.01 * rng.standard_normal(n), "CCC": rng.standard_normal(n)},
        index=_dates(n),
    )
    corr, n_obs = pairwise_correlation(panel, min_obs=50)
    assert corr.loc["AAA", "BBB"] > 0.95  # near-duplicate
    assert abs(corr.loc["AAA", "CCC"]) < 0.3  # independent
    assert n_obs.loc["AAA", "BBB"] == n


def test_pairwise_correlation_below_min_obs_is_nan_not_zero():
    d = _dates(40)
    panel = pd.DataFrame({"AAA": np.arange(40.0)}, index=d)
    panel["BBB"] = np.nan
    panel.loc[d[:10], "BBB"] = np.arange(10.0)  # only 10 joint obs
    corr, n_obs = pairwise_correlation(panel, min_obs=30)
    assert np.isnan(corr.loc["AAA", "BBB"])  # unmeasured, never a measured zero
    assert n_obs.loc["AAA", "BBB"] == 10


# --- shrunk_residual_correlation -------------------------------------------


def test_shrunk_correlation_is_valid_and_shrinks_toward_identity():
    rng = np.random.default_rng(1)
    n, p = 80, 12
    f = rng.standard_normal(n)
    X = np.column_stack([0.6 * f + rng.standard_normal(n) for _ in range(p)])
    panel = pd.DataFrame(X, index=_dates(n), columns=[f"S{i}" for i in range(p)])
    corr, n_used = shrunk_residual_correlation(panel, min_obs=60)
    assert n_used == n
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)
    assert np.allclose(corr.to_numpy(), corr.to_numpy().T)
    eig = np.linalg.eigvalsh(corr.to_numpy())
    assert eig.min() > 0  # PSD / invertible — the whole point of shrinkage near p≈n
    sample = panel.corr().to_numpy()
    off = ~np.eye(p, dtype=bool)
    # shrinkage pulls off-diagonals toward 0 (toward the identity target)
    assert np.abs(corr.to_numpy()[off]).mean() < np.abs(sample[off]).mean()


def test_shrunk_correlation_refuses_too_few_complete_rows():
    d = _dates(40)
    panel = pd.DataFrame({"AAA": np.arange(40.0), "BBB": np.arange(40.0)}, index=d)
    panel.loc[d[20:], "BBB"] = np.nan  # only 20 complete-case rows
    with pytest.raises(ValueError, match="complete-case"):
        shrunk_residual_correlation(panel, min_obs=30)


# --- Benjamini–Hochberg -----------------------------------------------------


def test_bh_classic_example_rejects_four():
    # Benjamini & Hochberg (1995) worked example: m=15, alpha=0.05 -> 4 rejections.
    p = np.array([0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298,
                  0.0344, 0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000])
    reject, q = benjamini_hochberg(p, alpha=0.05)
    assert reject.sum() == 4
    assert reject[:4].all() and not reject[4:].any()
    assert np.all(np.diff(q[np.argsort(p)]) >= -1e-12)  # q monotone in sorted-p order


def test_bh_handles_nan_as_untested():
    p = np.array([0.001, np.nan, 0.002, np.nan])
    reject, q = benjamini_hochberg(p, alpha=0.05)
    assert not reject[1] and not reject[3]
    assert np.isnan(q[1]) and np.isnan(q[3])
    assert reject[0] and reject[2]


# --- fdr_edges: the known-null / known-good pair (the gate's teeth) ---------


def test_fdr_edges_known_good_recovers_planted_block():
    rng = np.random.default_rng(7)
    n = 250
    latent = rng.standard_normal(n)
    cols = {}
    for s in ("A1", "A2", "A3"):  # share a latent factor -> genuinely linked
        cols[s] = 0.8 * latent + 0.6 * rng.standard_normal(n)
    for s in ("B1", "B2", "B3"):  # independent noise
        cols[s] = rng.standard_normal(n)
    panel = pd.DataFrame(cols, index=_dates(n))
    corr, n_obs = pairwise_correlation(panel, min_obs=60)
    edges = fdr_edges(corr, n_obs, alpha=0.05)
    found = {frozenset((r.src, r.dst)) for r in edges.itertuples()}
    # all three within-block pairs must survive FDR
    for pair in (("A1", "A2"), ("A1", "A3"), ("A2", "A3")):
        assert frozenset(pair) in found, f"planted edge {pair} did not survive FDR"
    # no B–B edge should appear (independent)
    assert not any({"B1", "B2", "B3"} >= set(p) for p in found)


def test_fdr_edges_known_null_yields_almost_no_edges():
    rng = np.random.default_rng(11)
    n, p = 200, 25  # 300 independent pairs
    panel = pd.DataFrame(
        rng.standard_normal((n, p)), index=_dates(n), columns=[f"N{i}" for i in range(p)]
    )
    corr, n_obs = pairwise_correlation(panel, min_obs=60)
    edges = fdr_edges(corr, n_obs, alpha=0.05)
    # all-null: BH controls FDR -> at most a tiny handful of false edges out of 300 pairs.
    n_pairs = p * (p - 1) // 2
    assert len(edges) <= 0.02 * n_pairs  # << the per-test alpha would have leaked


def test_fdr_edges_candidate_pairs_restricts_family():
    rng = np.random.default_rng(7)
    n = 250
    latent = rng.standard_normal(n)
    panel = pd.DataFrame(
        {s: 0.8 * latent + 0.6 * rng.standard_normal(n) for s in ("A1", "A2", "A3")},
        index=_dates(n),
    )
    corr, n_obs = pairwise_correlation(panel, min_obs=60)
    only = {frozenset(("A1", "A2"))}
    edges = fdr_edges(corr, n_obs, alpha=0.05, candidate_pairs=only)
    assert set(edges.itertuples(index=False, name=None)) != set()
    assert {frozenset((r.src, r.dst)) for r in edges.itertuples()} == only


def test_fdr_edges_empty_is_valid_not_error():
    # 2 independent names, no structure -> a legitimately empty edge list (NO-GO signal).
    rng = np.random.default_rng(3)
    panel = pd.DataFrame(
        {"X": rng.standard_normal(120), "Y": rng.standard_normal(120)}, index=_dates(120)
    )
    corr, n_obs = pairwise_correlation(panel, min_obs=60)
    edges = fdr_edges(corr, n_obs, alpha=0.05)
    assert list(edges.columns) == ["src", "dst", "corr", "n_obs", "p_value", "q_value"]
    assert len(edges) == 0


def test_fdr_edges_min_abs_corr_drops_trivial_but_significant():
    rng = np.random.default_rng(5)
    n = 4000  # huge n makes even tiny correlations significant
    base = rng.standard_normal(n)
    panel = pd.DataFrame(
        {"P": base, "Q": 0.05 * base + rng.standard_normal(n)},  # real but ~0.05 corr
        index=_dates(n),
    )
    corr, n_obs = pairwise_correlation(panel, min_obs=60)
    sig = fdr_edges(corr, n_obs, alpha=0.05, min_abs_corr=0.0)
    trivial_dropped = fdr_edges(corr, n_obs, alpha=0.05, min_abs_corr=0.2)
    assert len(sig) == 1  # statistically significant at huge n
    assert len(trivial_dropped) == 0  # but economically trivial -> dropped

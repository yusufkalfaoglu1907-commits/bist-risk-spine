"""M3 sector restriction + topological filtering (tmkg.signals.correlation) — synthetic.

Sector restriction (Alves-style block estimation before inversion) and the MST / PMFG
skeletons the stability metric is computed on. Deterministic known-answer checks.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tmkg.signals.correlation import (
    mst_filter,
    pmfg_filter,
    sector_restricted_correlation,
    within_sector_pairs,
)


def _dates(n, start=dt.date(2024, 1, 1)):
    return [start + dt.timedelta(days=i) for i in range(n)]


# --- within_sector_pairs ----------------------------------------------------


def test_within_sector_pairs_only_same_sector():
    sectors = {"A1": "bank", "A2": "bank", "A3": "bank", "B1": "steel", "B2": "steel"}
    pairs = within_sector_pairs(sectors)
    assert frozenset(("A1", "A2")) in pairs
    assert frozenset(("B1", "B2")) in pairs
    assert frozenset(("A1", "B1")) not in pairs  # cross-sector excluded
    assert len(pairs) == 3 + 1  # C(3,2) bank + C(2,2) steel


def test_within_sector_pairs_drops_unmapped():
    sectors = {"A1": "bank", "A2": "bank", "X": None}
    pairs = within_sector_pairs(sectors)
    assert pairs == {frozenset(("A1", "A2"))}


# --- sector_restricted_correlation ------------------------------------------


def test_sector_restricted_is_block_diagonal():
    rng = np.random.default_rng(2)
    n = 120
    bank = rng.standard_normal(n)
    steel = rng.standard_normal(n)
    panel = pd.DataFrame(
        {
            "A1": 0.8 * bank + 0.6 * rng.standard_normal(n),
            "A2": 0.8 * bank + 0.6 * rng.standard_normal(n),
            "B1": 0.8 * steel + 0.6 * rng.standard_normal(n),
            "B2": 0.8 * steel + 0.6 * rng.standard_normal(n),
        },
        index=_dates(n),
    )
    sectors = {"A1": "bank", "A2": "bank", "B1": "steel", "B2": "steel"}
    corr = sector_restricted_correlation(panel, sectors, min_obs=60)
    # cross-sector entries are exactly zero (not estimated-then-thresholded)
    assert corr.loc["A1", "B1"] == 0.0
    assert corr.loc["A2", "B2"] == 0.0
    # within-sector entries are the real (positive) shrunk correlation
    assert corr.loc["A1", "A2"] > 0.2
    assert corr.loc["B1", "B2"] > 0.2
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)


def test_sector_restricted_singleton_sector_keeps_only_diagonal():
    rng = np.random.default_rng(4)
    n = 100
    panel = pd.DataFrame(
        {"A1": rng.standard_normal(n), "A2": rng.standard_normal(n), "LONE": rng.standard_normal(n)},
        index=_dates(n),
    )
    sectors = {"A1": "bank", "A2": "bank", "LONE": "solo"}
    corr = sector_restricted_correlation(panel, sectors, min_obs=60)
    assert corr.loc["LONE", "A1"] == 0.0
    assert corr.loc["LONE", "LONE"] == 1.0


def test_sector_restricted_drops_unmapped_symbols():
    rng = np.random.default_rng(6)
    n = 100
    panel = pd.DataFrame(
        {"A1": rng.standard_normal(n), "A2": rng.standard_normal(n), "GHOST": rng.standard_normal(n)},
        index=_dates(n),
    )
    sectors = {"A1": "bank", "A2": "bank"}  # GHOST unmapped
    corr = sector_restricted_correlation(panel, sectors, min_obs=60)
    assert "GHOST" not in corr.columns


# --- MST / PMFG -------------------------------------------------------------


def _corr_from_panel(panel):
    return panel.corr()


def test_mst_has_n_minus_one_edges_and_is_a_tree():
    rng = np.random.default_rng(8)
    n = 200
    latent = rng.standard_normal(n)
    cols = {f"S{i}": (0.5 + 0.05 * i) * latent + rng.standard_normal(n) for i in range(6)}
    corr = pd.DataFrame(cols, index=_dates(n)).corr()
    mst = mst_filter(corr)
    assert len(mst) == corr.shape[1] - 1  # tree on N nodes
    # acyclic: nodes touched == edges + components(=1 here, fully connected corr)
    import networkx as nx
    g = nx.from_pandas_edgelist(mst, "src", "dst")
    assert nx.is_tree(g)


def test_mst_keeps_strongest_link():
    # 3 names: A-B very correlated, C weakly tied. MST must include A-B.
    rng = np.random.default_rng(9)
    n = 300
    a = rng.standard_normal(n)
    panel = pd.DataFrame(
        {"A": a, "B": a + 0.05 * rng.standard_normal(n), "C": rng.standard_normal(n)},
        index=_dates(n),
    )
    mst = mst_filter(panel.corr())
    found = {frozenset((r.src, r.dst)) for r in mst.itertuples()}
    assert frozenset(("A", "B")) in found


def test_pmfg_respects_planar_edge_limit_and_contains_mst():
    rng = np.random.default_rng(10)
    n = 250
    latent = rng.standard_normal(n)
    cols = {f"S{i}": (0.4 + 0.04 * i) * latent + rng.standard_normal(n) for i in range(8)}
    corr = pd.DataFrame(cols, index=_dates(n)).corr()
    pmfg = pmfg_filter(corr)
    p = corr.shape[1]
    assert len(pmfg) <= 3 * (p - 2)  # planar limit
    # PMFG must contain the MST as a subgraph
    mst = mst_filter(corr)
    pmfg_edges = {frozenset((r.src, r.dst)) for r in pmfg.itertuples()}
    mst_edges = {frozenset((r.src, r.dst)) for r in mst.itertuples()}
    assert mst_edges <= pmfg_edges


def test_pmfg_candidate_pairs_restricts_edges():
    rng = np.random.default_rng(12)
    n = 200
    latent = rng.standard_normal(n)
    cols = {s: 0.7 * latent + rng.standard_normal(n) for s in ("A1", "A2", "B1", "B2")}
    corr = pd.DataFrame(cols, index=_dates(n)).corr()
    only_a = {frozenset(("A1", "A2"))}
    pmfg = pmfg_filter(corr, candidate_pairs=only_a)
    edges = {frozenset((r.src, r.dst)) for r in pmfg.itertuples()}
    assert edges <= only_a


def test_mst_empty_for_single_name():
    corr = pd.DataFrame([[1.0]], index=["A"], columns=["A"])
    assert mst_filter(corr).empty
    assert pmfg_filter(corr).empty

"""Residual-correlation engine — the M3 residual-survival [STOP] gate (BUILD_PLAN.md M3).

M3 answers the one question the correlation pillar lives or dies on: *does stable residual
linkage survive the factor strip, or was the "alpha" just the foreign-flow factor we
removed?* (system-design-v2.md: never store raw pairwise correlations as edges — only
**residual** linkage after the common channels are stripped.)

This module is the pure machinery for that experiment, built before it is pointed at real
data (the project's sequencing rule: build the judge before the verdict). It operates on the
**neutralized residuals** of the L2 ``residuals`` table — never on raw returns — and is held
to two design invariants:

  - **Never invert the raw sample covariance.** With ``p ≈ n`` (≈500 names, comparable-length
    windows) the sample covariance is ill-conditioned and its inverse is noise. We shrink with
    Ledoit–Wolf on the scale-free correlation before any inversion (§201) — the same posture
    as ``factors.betas``.
  - **FDR control.** ~125k pairs guarantee false discoveries at any fixed per-pair α. Edge
    selection runs Benjamini–Hochberg so the *expected* false-discovery fraction among the
    kept edges is bounded — not the per-test error.

Pure: arrays / DataFrames in, arrays / DataFrames out. No network, no L2, no PIT — those live
in the gate runner that reads residuals through ``PITAccess`` and calls these.
"""
from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.covariance import LedoitWolf

_EPS = 1e-12


def residual_panel(
    residuals: pd.DataFrame,
    *,
    min_obs: int = 60,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """Pivot the long L2 ``residuals`` frame to a wide (date × symbol) panel.

    ``residuals`` : ``[symbol, bar_date, residual, ...]`` (the neutralized series). The panel
    is indexed by ``bar_date`` with one column per symbol; missing (symbol, date) cells are
    NaN (ragged — names list/delist at different times, §5 survivorship). A symbol with fewer
    than ``min_obs`` non-NaN residuals is dropped (too thin to correlate honestly, §4) — its
    absence is a refusal, never a zero-filled column that would fabricate a near-zero linkage.

    ``symbols`` optionally restricts/orders the columns (e.g. an as-of universe). Returns the
    panel sorted by date with low-coverage columns removed.
    """
    need = {"symbol", "bar_date", "residual"}
    if not need <= set(residuals.columns):
        raise ValueError(f"residual_panel: residuals missing {sorted(need - set(residuals.columns))}")
    r = residuals[["symbol", "bar_date", "residual"]].copy()
    r["bar_date"] = pd.to_datetime(r["bar_date"]).dt.date
    if symbols is not None:
        r = r[r["symbol"].isin(symbols)]
    # mean over any duplicate (symbol, date) — there should be none, but never silently pick one
    panel = (
        r.dropna(subset=["residual"])
        .pivot_table(index="bar_date", columns="symbol", values="residual", aggfunc="mean")
        .sort_index()
    )
    panel.columns.name = None
    keep = panel.columns[panel.notna().sum(axis=0) >= min_obs]
    panel = panel[keep]
    if symbols is not None:  # preserve the caller's ordering among the survivors
        panel = panel[[s for s in symbols if s in panel.columns]]
    return panel


def pairwise_correlation(
    panel: pd.DataFrame, *, min_obs: int = 60
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pairwise-complete sample correlation and the per-pair joint observation count.

    Returns ``(corr, n_obs)`` — both symmetric (symbol × symbol) DataFrames. ``corr[i, j]`` is
    the Pearson correlation over the dates where *both* i and j have a residual (raggedness is
    handled per pair, not by dropping every date with any gap); ``n_obs[i, j]`` is that joint
    count. A pair with fewer than ``min_obs`` joint dates gets ``corr = NaN`` — it is not a
    measured zero (the FDR step then skips it; an unmeasurable pair can never become an edge).

    This sample correlation is the **test statistic** for FDR edge selection. The shrunk
    estimate (``shrunk_residual_correlation``) is the **inversion-safe** matrix used downstream
    for the network topology — the two are deliberately separate concerns.
    """
    corr = panel.corr(method="pearson", min_periods=min_obs)
    mask = panel.notna().astype(int)
    n_obs = pd.DataFrame(mask.T.values @ mask.values, index=panel.columns, columns=panel.columns)
    corr = corr.where(n_obs >= min_obs)  # below threshold ⇒ unmeasured, not zero
    return corr, n_obs


def shrunk_residual_correlation(
    panel: pd.DataFrame, *, min_obs: int = 60
) -> tuple[pd.DataFrame, int]:
    """Ledoit–Wolf-shrunk residual **correlation** over the complete-case window.

    With ``p ≈ n`` the raw sample covariance is ill-conditioned and must never be inverted
    directly (§201). LW shrinks toward a scaled identity with an analytically optimal weight;
    we apply it to the standardized (unit-variance) residuals so it acts on the scale-free
    correlation structure (the statistically correct target near ``p ≈ n``) — mirroring
    ``factors.betas._estimate_betas``.

    LW needs a single aligned matrix, so this uses the **complete-case** rows (dates where every
    surviving symbol has a residual). Returns ``(corr, n_used)``; raises if fewer than
    ``min_obs`` complete rows remain (refuse rather than shrink noise into a confident-looking
    matrix, §4). For the ragged full panel the gate runs this per rolling window where the
    common support is dense; the pairwise sample correlation above covers the ragged test.
    """
    if panel.shape[1] < 2:
        raise ValueError("shrunk_residual_correlation: need at least 2 symbols")
    complete = panel.dropna(axis=0, how="any")
    n_used = int(len(complete))
    if n_used < min_obs:
        raise ValueError(
            f"shrunk_residual_correlation: only {n_used} complete-case rows (< min_obs={min_obs}); "
            "refusing to shrink noise into a confident matrix"
        )
    X = complete.to_numpy(dtype=float)
    Xc = X - X.mean(axis=0)
    sx = Xc.std(axis=0, ddof=1)
    sx = np.where(sx > _EPS, sx, 1.0)  # a constant residual column carries no correlation
    Xs = Xc / sx
    cov = LedoitWolf(assume_centered=True).fit(Xs).covariance_
    d = np.sqrt(np.clip(np.diag(cov), _EPS, None))
    corr = cov / np.outer(d, d)
    np.fill_diagonal(corr, 1.0)
    return pd.DataFrame(corr, index=complete.columns, columns=complete.columns), n_used


def benjamini_hochberg(p: np.ndarray, alpha: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    """Benjamini–Hochberg FDR control. Returns ``(reject, qvalue)`` aligned to ``p``.

    Controls the expected false-discovery fraction among the rejected hypotheses at ``alpha``
    (the right error to bound across ~125k pairs — bounding the per-test error would still leak
    thousands of false edges). ``qvalue`` is the standard step-up adjusted p-value (monotone
    from the top). NaNs in ``p`` are treated as untested (never rejected, qvalue NaN).
    """
    p = np.asarray(p, dtype=float)
    out_reject = np.zeros(p.shape, dtype=bool)
    out_q = np.full(p.shape, np.nan)
    finite = np.where(np.isfinite(p))[0]
    m = finite.size
    if m == 0:
        return out_reject, out_q
    pf = p[finite]
    order = np.argsort(pf, kind="mergesort")
    ranks = np.arange(1, m + 1)
    # step-up q-values: enforce monotonicity from the largest p downward
    q_sorted = np.minimum.accumulate((pf[order] * m / ranks)[::-1])[::-1]
    q_sorted = np.clip(q_sorted, 0.0, 1.0)
    q = np.empty(m)
    q[order] = q_sorted
    out_q[finite] = q
    # largest k with p_(k) <= (k/m)*alpha ⇒ reject the k smallest
    below = pf[order] <= (ranks / m) * alpha
    if below.any():
        kmax = np.max(np.where(below)[0])
        out_reject[finite[order[: kmax + 1]]] = True
    return out_reject, out_q


def fdr_edges(
    corr: pd.DataFrame,
    n_obs: pd.DataFrame,
    *,
    alpha: float = 0.05,
    min_abs_corr: float = 0.0,
    candidate_pairs: set[frozenset] | None = None,
) -> pd.DataFrame:
    """FDR-controlled residual-correlation edge list (the upper triangle, undirected).

    For each unordered pair (i, j) with a measured ``corr`` and ``n_obs ≥ 3``, the Pearson
    t-statistic ``t = r·√((n−2)/(1−r²))`` gives a two-sided p-value (``df = n−2``). Benjamini–
    Hochberg at level ``alpha`` selects the surviving edges. ``min_abs_corr`` additionally drops
    statistically-significant but economically-trivial linkages. ``candidate_pairs`` (a set of
    ``frozenset({i, j})``) restricts the tested family up front — e.g. only within-sector pairs
    (slice 2's sector restriction) — which also shrinks the multiple-testing burden.

    Returns ``[src, dst, corr, n_obs, p_value, q_value]`` for the kept edges, sorted by
    ascending q-value then descending |corr|. Empty (no surviving structure) is a valid,
    important result for the [STOP] gate — it is the NO-GO signal, not an error.
    """
    syms = list(corr.columns)
    rows: list[dict] = []
    for a in range(len(syms)):
        for b in range(a + 1, len(syms)):
            si, sj = syms[a], syms[b]
            if candidate_pairs is not None and frozenset((si, sj)) not in candidate_pairs:
                continue
            r = corr.iat[a, b]
            n = n_obs.iat[a, b] if n_obs is not None else np.nan
            if not np.isfinite(r) or not np.isfinite(n) or n < 3:
                continue
            rows.append({"src": si, "dst": sj, "corr": float(r), "n_obs": int(n)})
    if not rows:
        return pd.DataFrame(columns=["src", "dst", "corr", "n_obs", "p_value", "q_value"])
    df = pd.DataFrame(rows)
    r = df["corr"].to_numpy()
    n = df["n_obs"].to_numpy(dtype=float)
    r_clip = np.clip(r, -1 + _EPS, 1 - _EPS)
    t = r_clip * np.sqrt((n - 2) / (1.0 - r_clip**2))
    df["p_value"] = 2.0 * stats.t.sf(np.abs(t), df=n - 2)
    reject, q = benjamini_hochberg(df["p_value"].to_numpy(), alpha=alpha)
    df["q_value"] = q
    keep = reject & (df["corr"].abs() >= min_abs_corr)
    df = df[keep].sort_values(["q_value", "corr"], key=lambda s: s if s.name != "corr" else -s.abs())
    return df.reset_index(drop=True)[["src", "dst", "corr", "n_obs", "p_value", "q_value"]]


# --- sector restriction (Alves-style, before any inversion) -----------------


def within_sector_pairs(sectors: dict[str, str]) -> set[frozenset]:
    """The set of unordered same-sector pairs — the candidate family for ``fdr_edges``.

    Restricting the tested pairs to within-sector (a) encodes the prior that genuine residual
    linkage is overwhelmingly intra-sector after the market/FX/flow strip, and (b) shrinks the
    multiple-testing burden from ~125k all-pairs to the far smaller within-sector count, which
    sharpens FDR. A name with no sector mapping contributes no pair (refused, not lumped into a
    catch-all sector that would fabricate cross-industry edges)."""
    by_sector: dict[str, list[str]] = {}
    for sym, sec in sectors.items():
        if sec is None or (isinstance(sec, float) and np.isnan(sec)):
            continue
        by_sector.setdefault(sec, []).append(sym)
    pairs: set[frozenset] = set()
    for members in by_sector.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add(frozenset((members[i], members[j])))
    return pairs


def sector_restricted_correlation(
    panel: pd.DataFrame,
    sectors: dict[str, str],
    *,
    min_obs: int = 60,
) -> pd.DataFrame:
    """Block-diagonal residual correlation: Ledoit–Wolf-shrunk **within each sector**, zero
    across sectors (Alves-style sector-restricted estimation, system-design-v2.md M3).

    With ``p ≈ n`` a single full-matrix estimate is unstable; estimating only the within-sector
    blocks (where the prior says the residual linkage lives) keeps each block far from the
    ``p ≈ n`` wall and yields a well-conditioned, block-sparse matrix safe to invert downstream.
    Cross-sector entries are set to exactly 0 — not estimated then thresholded — so no spurious
    cross-industry linkage can survive. A sector with a single name, or too few complete-case
    rows for its block, keeps only its unit diagonal (no fabricated off-diagonal).

    Symbols absent from ``sectors`` are dropped (a name we can't place in a sector can't enter
    a sector-restricted estimate, §4). Returns a symbol × symbol correlation DataFrame ordered
    by sector then symbol."""
    placed = [s for s in panel.columns if sectors.get(s) is not None]
    by_sector: dict[str, list[str]] = {}
    for s in placed:
        by_sector.setdefault(sectors[s], []).append(s)
    order = [s for sec in sorted(by_sector) for s in sorted(by_sector[sec])]
    corr = pd.DataFrame(np.eye(len(order)), index=order, columns=order)
    for sec in sorted(by_sector):
        members = sorted(by_sector[sec])
        if len(members) < 2:
            continue
        block = panel[members].dropna(axis=0, how="any")
        if len(block) < min_obs:
            continue  # too thin to estimate honestly — leave the unit diagonal only
        bc, _ = shrunk_residual_correlation(block, min_obs=min_obs)
        corr.loc[members, members] = bc.loc[members, members].values
    return corr


# --- topological filtering: MST / PMFG --------------------------------------


def _distance(corr: float) -> float:
    """Mantegna metric d = sqrt(2(1−ρ)): strong +corr ⇒ small distance (close)."""
    return float(np.sqrt(max(0.0, 2.0 * (1.0 - corr))))


def mst_filter(corr: pd.DataFrame) -> pd.DataFrame:
    """Mantegna minimum-spanning-tree filter of the correlation network.

    Builds the complete graph on the names with the Mantegna distance ``sqrt(2(1−ρ))`` and keeps
    the MST (the ``N−1`` strongest backbone links that connect every name with no cycles) — the
    classic asset-tree filter. Returns ``[src, dst, corr, distance]`` for the tree edges. The MST
    is the most aggressive skeleton; PMFG retains more structure (loops/cliques) on top of it."""
    syms = list(corr.columns)
    if len(syms) < 2:
        return pd.DataFrame(columns=["src", "dst", "corr", "distance"])
    g = nx.Graph()
    g.add_nodes_from(syms)
    for a in range(len(syms)):
        for b in range(a + 1, len(syms)):
            r = corr.iat[a, b]
            if np.isfinite(r):
                g.add_edge(syms[a], syms[b], distance=_distance(r), corr=float(r))
    mst = nx.minimum_spanning_tree(g, weight="distance")
    rows = [{"src": u, "dst": v, "corr": d["corr"], "distance": d["distance"]}
            for u, v, d in mst.edges(data=True)]
    return (pd.DataFrame(rows, columns=["src", "dst", "corr", "distance"])
            .sort_values("distance").reset_index(drop=True))


def pmfg_filter(corr: pd.DataFrame, *, candidate_pairs: set[frozenset] | None = None) -> pd.DataFrame:
    """Planar Maximally Filtered Graph (Tumminello et al.) of the correlation network.

    Greedily adds edges in **decreasing |ρ|** as long as the graph stays planar, up to the
    planar limit ``3(N−2)`` edges. The PMFG retains the MST as a subgraph but keeps richer
    topology (triangles/cliques), which is what the stability metric needs to be sensitive to.
    ``candidate_pairs`` (e.g. within-sector) restricts which edges may be added. Returns
    ``[src, dst, corr, distance]`` sorted by descending |corr|."""
    syms = list(corr.columns)
    n = len(syms)
    if n < 2:
        return pd.DataFrame(columns=["src", "dst", "corr", "distance"])
    cand = []
    for a in range(n):
        for b in range(a + 1, n):
            si, sj = syms[a], syms[b]
            if candidate_pairs is not None and frozenset((si, sj)) not in candidate_pairs:
                continue
            r = corr.iat[a, b]
            if np.isfinite(r) and abs(r) > _EPS:
                cand.append((abs(float(r)), float(r), si, sj))
    cand.sort(reverse=True)  # strongest |corr| first
    limit = 3 * (n - 2) if n >= 3 else 1
    g = nx.Graph()
    g.add_nodes_from(syms)
    rows: list[dict] = []
    for _, r, si, sj in cand:
        if g.number_of_edges() >= limit:
            break
        g.add_edge(si, sj)
        if nx.check_planarity(g, counterexample=False)[0]:
            rows.append({"src": si, "dst": sj, "corr": r, "distance": _distance(r)})
        else:
            g.remove_edge(si, sj)
    return (pd.DataFrame(rows, columns=["src", "dst", "corr", "distance"])
            .sort_values("corr", key=lambda s: -s.abs()).reset_index(drop=True))

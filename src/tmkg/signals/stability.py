"""Residual-network stability — the M3 [STOP]-gate measurement (BUILD_PLAN.md M3).

The gate's question: *after stripping market + FX + foreign-flow (+ the rest of the ladder),
is the filtered residual network **stable across rolling windows**, or does its "structure"
reshuffle window-to-window like noise?* A pillar built on linkages that don't persist is a
pillar built on sampling noise — it must fail here, cheaply, not later in a backtest.

The measurement, per consecutive window pair:
  - **edge Jaccard** of the FDR-significant within-sector residual-edge sets — how much of the
    discovered linkage recurs;
  - **weight rank-stability** — Spearman ρ of the shared edges' correlations (do the strong
    links stay strong, or do ranks scramble?);
  - against a **random-overlap baseline** — the Jaccard two independent random edge sets of the
    same sizes would show by chance. Persistence only counts as *signal* if it clears this
    floor (the structure recurs far more than chance), which is the honest GO test.

Pure: operates on a residual **panel** (wide date × symbol) + a sector map. No network, no L2,
no PIT — the gate runner reads residuals through PITAccess and hands them here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from tmkg.signals.correlation import (
    fdr_edges,
    pairwise_correlation,
    within_sector_pairs,
)

_EPS = 1e-12


def rolling_window_bounds(n_dates: int, *, window: int, step: int) -> list[tuple[int, int]]:
    """Half-open ``[lo, hi)`` row-index windows of length ``window`` stepped by ``step``.

    ``step == window`` gives non-overlapping windows (the honest default for stability — the
    estimates are independent samples). ``step < window`` overlaps, which mechanically inflates
    apparent stability (shared rows ⇒ shared estimate); the caller owns that trade-off."""
    if window <= 0 or step <= 0:
        raise ValueError("window and step must be positive")
    out = []
    lo = 0
    while lo + window <= n_dates:
        out.append((lo, lo + window))
        lo += step
    return out


def window_edge_set(
    panel: pd.DataFrame,
    *,
    sectors: dict[str, str] | None = None,
    alpha: float = 0.05,
    min_obs: int = 40,
    min_abs_corr: float = 0.0,
) -> tuple[set[frozenset], dict[frozenset, float]]:
    """The FDR-significant residual-edge set for one window panel.

    Returns ``(edges, weight)`` where ``edges`` is a set of ``frozenset({i, j})`` and ``weight``
    maps each edge to its residual correlation. Within-sector restriction (when ``sectors`` is
    given) both encodes the intra-sector prior and shrinks the FDR family. An empty set is a
    valid result (no surviving linkage in this window)."""
    corr, n_obs = pairwise_correlation(panel, min_obs=min_obs)
    cand = within_sector_pairs(sectors) if sectors is not None else None
    edges = fdr_edges(corr, n_obs, alpha=alpha, min_abs_corr=min_abs_corr, candidate_pairs=cand)
    eset = {frozenset((r.src, r.dst)) for r in edges.itertuples()}
    wmap = {frozenset((r.src, r.dst)): r.corr for r in edges.itertuples()}
    return eset, wmap


def jaccard(a: set, b: set) -> float:
    """|a∩b| / |a∪b|. Two empty sets ⇒ 1.0 (vacuously identical: nothing to disagree on)."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def random_overlap_jaccard(k_a: int, k_b: int, n_candidates: int) -> float:
    """The Jaccard two *independent uniform-random* edge sets of sizes ``k_a``/``k_b`` drawn from
    ``n_candidates`` possible pairs would show by chance — the noise floor a real recurrence must
    clear. Uses expected intersection ``k_a·k_b/N`` over expected union; 0 when nothing could
    overlap. This is the analytic null the GO decision is judged against."""
    if n_candidates <= 0 or (k_a == 0 and k_b == 0):
        return 0.0
    exp_inter = k_a * k_b / n_candidates
    exp_union = k_a + k_b - exp_inter
    return exp_inter / exp_union if exp_union > _EPS else 0.0


def _weight_rank_stability(wa: dict, wb: dict, shared: set) -> float:
    """Spearman ρ of shared edges' correlations across the two windows (NaN if <3 shared)."""
    if len(shared) < 3:
        return float("nan")
    xs = [wa[e] for e in shared]
    ys = [wb[e] for e in shared]
    rho, _ = stats.spearmanr(xs, ys)
    return float(rho)


def rolling_stability(
    panel: pd.DataFrame,
    *,
    sectors: dict[str, str] | None = None,
    window: int = 120,
    step: int | None = None,
    alpha: float = 0.05,
    min_obs: int = 40,
    min_abs_corr: float = 0.0,
) -> pd.DataFrame:
    """Per-consecutive-window-pair stability of the filtered residual network.

    Slides a ``window``-row window over ``panel`` (stepped by ``step``, default = ``window`` →
    non-overlapping), builds each window's FDR-significant within-sector edge set, and compares
    consecutive windows. Returns one row per consecutive pair:
    ``[win_a_end, win_b_end, n_edges_a, n_edges_b, n_shared, jaccard, random_jaccard, lift,
       weight_rank_rho]`` where ``lift = jaccard / random_jaccard`` (the chance-adjusted
    persistence; ``inf`` if the random floor is 0 but real edges recur). The candidate-pair
    count for the null is the within-sector family (or all pairs if no sectors)."""
    step = window if step is None else step
    bounds = rolling_window_bounds(len(panel), window=window, step=step)
    if len(bounds) < 2:
        return pd.DataFrame(columns=[
            "win_a_end", "win_b_end", "n_edges_a", "n_edges_b", "n_shared",
            "jaccard", "random_jaccard", "lift", "weight_rank_rho"])

    if sectors is not None:
        n_candidates = len(within_sector_pairs({s: sectors[s] for s in panel.columns
                                                if sectors.get(s) is not None}))
    else:
        p = panel.shape[1]
        n_candidates = p * (p - 1) // 2

    dates = list(panel.index)
    sets = []
    for lo, hi in bounds:
        win = panel.iloc[lo:hi]
        eset, wmap = window_edge_set(win, sectors=sectors, alpha=alpha,
                                     min_obs=min_obs, min_abs_corr=min_abs_corr)
        sets.append((dates[hi - 1], eset, wmap))

    rows: list[dict] = []
    for (end_a, ea, wa), (end_b, eb, wb) in zip(sets, sets[1:]):
        shared = ea & eb
        jac = jaccard(ea, eb)
        rnd = random_overlap_jaccard(len(ea), len(eb), n_candidates)
        lift = (jac / rnd) if rnd > _EPS else (float("inf") if jac > 0 else 0.0)
        rows.append({
            "win_a_end": end_a, "win_b_end": end_b,
            "n_edges_a": len(ea), "n_edges_b": len(eb), "n_shared": len(shared),
            "jaccard": jac, "random_jaccard": rnd, "lift": lift,
            "weight_rank_rho": _weight_rank_stability(wa, wb, shared),
        })
    return pd.DataFrame(rows)


def stability_summary(rolling: pd.DataFrame) -> dict:
    """Collapse ``rolling_stability`` rows to the gate's headline numbers (medians, robust to a
    single odd window pair). ``lift`` excludes ``inf`` rows from the median but counts them."""
    if rolling.empty:
        return {"n_window_pairs": 0, "median_jaccard": float("nan"),
                "median_random_jaccard": float("nan"), "median_lift": float("nan"),
                "n_inf_lift": 0, "median_weight_rank_rho": float("nan"),
                "median_n_edges": float("nan")}
    lift = rolling["lift"].replace([np.inf, -np.inf], np.nan)
    edges = pd.concat([rolling["n_edges_a"], rolling["n_edges_b"]])
    return {
        "n_window_pairs": int(len(rolling)),
        "median_jaccard": float(rolling["jaccard"].median()),
        "median_random_jaccard": float(rolling["random_jaccard"].median()),
        "median_lift": float(lift.median()) if lift.notna().any() else float("nan"),
        "n_inf_lift": int(np.isinf(rolling["lift"]).sum()),
        "median_weight_rank_rho": float(rolling["weight_rank_rho"].median(skipna=True)),
        "median_n_edges": float(edges.median()),
    }


# Gate decision thresholds (documented, conservative). The correlation pillar is the
# cheapest-to-kill thesis (BUILD_PLAN sequencing rule 1) — bias toward NO-GO. These are the
# defaults; the gate runner records whichever values it used.
GATE_MIN_LIFT = 3.0           # filtered structure must recur ≥3× the random-overlap floor
GATE_MIN_JACCARD = 0.10       # …and in absolute terms ≥10% of edges persist window-to-window
GATE_MIN_WINDOW_PAIRS = 3     # …over enough window pairs to mean anything
GATE_MIN_MEDIAN_EDGES = 5.0   # …with a non-trivial number of edges to be stable about
# Reported, NON-gating: weight rank-stability. A genuinely stable block whose edges are all
# ~equally strong has tie-noise ranks and a low ρ — vetoing it would be wrong. The survival
# question is edge-set *persistence beyond chance*; rank-stability (do the strong links stay
# strongest) is an M5 signal-construction refinement, surfaced here as quality, not a gate.
GATE_DIAG_RANK_RHO = 0.30


def decide_gate(
    summary: dict,
    *,
    min_lift: float = GATE_MIN_LIFT,
    min_jaccard: float = GATE_MIN_JACCARD,
    min_window_pairs: int = GATE_MIN_WINDOW_PAIRS,
    min_median_edges: float = GATE_MIN_MEDIAN_EDGES,
    diag_rank_rho: float = GATE_DIAG_RANK_RHO,
) -> dict:
    """Apply the documented GO/NO-GO rule to a ``stability_summary``. Returns
    ``{decision, failed_checks, checks, diagnostics, thresholds}``. **GO** only if *every*
    gating check passes; any failure (including too-few windows or too-few edges to judge) is
    **NO-GO** — the honest default for a kill-experiment. Weight rank-stability is reported as
    a diagnostic only (see ``GATE_DIAG_RANK_RHO``), never gating. ``decision`` ∈ {"GO","NO-GO"}.
    """
    checks = {
        "enough_window_pairs": summary.get("n_window_pairs", 0) >= min_window_pairs,
        "enough_edges": _ge(summary.get("median_n_edges"), min_median_edges),
        "lift_clears_chance": _ge(summary.get("median_lift"), min_lift),
        "absolute_persistence": _ge(summary.get("median_jaccard"), min_jaccard),
    }
    reasons = [k for k, ok in checks.items() if not ok]
    return {
        "decision": "GO" if all(checks.values()) else "NO-GO",
        "failed_checks": reasons,
        "checks": checks,
        "diagnostics": {
            "weight_rank_rho": summary.get("median_weight_rank_rho"),
            "weight_rank_stable": _ge(summary.get("median_weight_rank_rho"), diag_rank_rho),
        },
        "thresholds": {
            "min_lift": min_lift, "min_jaccard": min_jaccard,
            "min_window_pairs": min_window_pairs, "min_median_edges": min_median_edges,
            "diag_rank_rho": diag_rank_rho,
        },
    }


def _ge(value, threshold) -> bool:
    """True iff ``value`` is a real number ≥ ``threshold`` (NaN/None ⇒ fails — cannot judge)."""
    return value is not None and np.isfinite(value) and value >= threshold

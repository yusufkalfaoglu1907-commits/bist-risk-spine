"""Residual stat-arb — peer-relative residual mean reversion (BUILD_PLAN.md M5).

The first *real* signal through the M4 judge. Premise (system-design-v2.md correlation pillar,
GO at the M3 residual-survival [STOP] gate, ADR-0003): after the market / FX / flow / sector
factors are stripped, the residual co-movement that *survives* is genuine idiosyncratic linkage —
and that linkage mean-reverts. A name whose residual today has jumped away from the residuals of
the peers it is genuinely co-linked with (the surviving M3 edges) tends to revert toward them.

Construction:
  - **Peers come from the M3 surviving residual-corr edges**, re-estimated on a trailing window
    (rolling refit) — never the dense matrix, only the FDR-surviving, sector-restricted edges
    (correlation.py). Using the *graph* is what differentiates this from a naïve own-name
    reversion, which is exactly baseline (c) the candidate must beat to be promoted.
  - For each name i the edges define a **comove-predicted residual**
    ``r̂_i = Σ_j ρ_ij·r_j / Σ_j|ρ_ij|`` (signed ρ — a negatively-linked peer enters flipped).
    The **spread** ``s_i = r_i − r̂_i`` is i's residual *in excess of what its linked peers
    explain*.
  - z-score the spread per name over a trailing window; the bet is **fade the spread** (mean
    reversion): ``w_i ∝ −z(s_i)``. A z-threshold optionally sparsifies the book (only act on a
    real dislocation).

PIT honesty (CLAUDE.md §4/§5): the weights are ``shift(1)``-ed so a weight at date t uses only
residuals/edges known strictly *before* t — exactly like the M4 baselines (promotion.py) — and
the runner (run_statarb.py) earns those weights on the *raw* total-return panel. The rolling
edge schedule only ever estimates an edge set from data on/before its effective date and applies
it strictly afterward, so no edge encodes the future. Pure compute (NumPy/pandas + the M3
correlation primitives) — no L2, no network; enforced by tests/invariants/
test_no_network_in_signal_layer.py. The runner wires PITAccess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from tmkg.signals.correlation import (
    fdr_edges,
    pairwise_correlation,
    within_sector_pairs,
)
from tmkg.signals.promotion import dollar_neutral_unit

_EPS = 1e-12


# --- edges -> comove-predicted residual -> spread ---------------------------


def edge_matrix(edges: pd.DataFrame, symbols: list[str]) -> np.ndarray:
    """Symmetric signed-correlation adjacency ``A`` over ``symbols`` (zero diagonal).

    ``edges`` is the M3 ``[src, dst, corr, ...]`` survivor list; ``A[i, j] = A[j, i] = corr_ij``
    for a surviving edge, 0 otherwise. An edge touching a symbol not in ``symbols`` is dropped
    (it cannot enter this panel's prediction). The zero diagonal stops a name from predicting
    itself — the whole point is the *peer*-relative spread."""
    idx = {s: i for i, s in enumerate(symbols)}
    n = len(symbols)
    A = np.zeros((n, n), dtype=float)
    for src, dst, corr in zip(edges["src"], edges["dst"], edges["corr"]):
        i, j = idx.get(src), idx.get(dst)
        if i is None or j is None or i == j:
            continue
        A[i, j] = A[j, i] = float(corr)
    return A


def comove_predicted(R: np.ndarray, A: np.ndarray) -> np.ndarray:
    """Comove-predicted residual ``r̂_t,i = Σ_j A_ij·r_t,j / Σ_j|A_ij|`` (vectorized).

    ``R`` is a (T × N) residual block, ``A`` the (N × N) signed adjacency. A name with no edges
    (zero row in ``A``) gets a column of NaN — *no* prediction, hence no spread, hence no bet
    (never a fabricated zero spread that would look like "perfectly explained by peers")."""
    denom = np.abs(A).sum(axis=1)             # per-name total edge weight
    Rfill = np.nan_to_num(R, nan=0.0)          # a missing peer residual contributes nothing
    num = Rfill @ A.T
    out = np.full_like(num, np.nan)
    ok = denom > _EPS
    out[:, ok] = num[:, ok] / denom[ok]
    return out


# --- rolling edge schedule (PIT: estimate on/before effective date) ----------


@dataclass(frozen=True)
class EdgeSnapshot:
    """One rolling-refit edge set, effective from ``effective_date`` until the next refit."""
    effective_date: date
    edges: pd.DataFrame
    n_edges: int
    window_start: date
    window_end: date


def build_edge_schedule(
    panel: pd.DataFrame,
    sectors: dict[str, str],
    *,
    edge_window: int = 250,
    refit_step: int = 60,
    alpha: float = 0.05,
    min_obs: int = 60,
    min_abs_corr: float = 0.0,
) -> list[EdgeSnapshot]:
    """Roll the M3 sector-restricted FDR edge estimation forward over ``panel``.

    Every ``refit_step`` rows, estimate edges on the trailing ``edge_window`` rows *ending at and
    including* that row (within-sector candidate pairs only, FDR-controlled — exactly the M3
    survivor set), and make them effective from that date onward. Because the signal is later
    ``shift(1)``-ed, an edge set effective at date d is only ever applied to weights for d+1..,
    so it never sees its own future. Returns the snapshots in chronological order; the first
    spans the warm-up rows where no estimate yet exists (its edge set is empty → no positions)."""
    dates = list(panel.index)
    n = len(dates)
    snaps: list[EdgeSnapshot] = []
    if n == 0:
        return snaps
    # warm-up: no edges until the first full window is available
    first_fit = max(edge_window - 1, min_obs - 1)
    cand = within_sector_pairs(sectors)
    pos = first_fit
    while pos < n:
        lo = max(0, pos - edge_window + 1)
        win = panel.iloc[lo : pos + 1]
        corr, n_obs = pairwise_correlation(win, min_obs=min_obs)
        edges = fdr_edges(corr, n_obs, alpha=alpha, min_abs_corr=min_abs_corr,
                          candidate_pairs=cand)
        snaps.append(EdgeSnapshot(
            effective_date=dates[pos], edges=edges, n_edges=int(len(edges)),
            window_start=win.index[0], window_end=win.index[-1]))
        pos += refit_step
    return snaps


# --- the signal -------------------------------------------------------------


def residual_spread(panel: pd.DataFrame, schedule: list[EdgeSnapshot]) -> pd.DataFrame:
    """Per-(date, name) residual spread ``s = r − r̂`` using the edge set active at each date.

    A date earlier than the first snapshot's effective date (warm-up) gets all-NaN (no edges yet).
    Within each snapshot's reign the comove-predicted residual is computed vectorized; the spread
    is NaN wherever the name has no peers under that snapshot."""
    symbols = list(panel.columns)
    R = panel.to_numpy(dtype=float)
    out = np.full(R.shape, np.nan)
    if not schedule:
        return pd.DataFrame(out, index=panel.index, columns=symbols)
    eff = [s.effective_date for s in schedule]
    date_arr = np.array(list(panel.index))
    for k, snap in enumerate(schedule):
        lo = np.searchsorted(date_arr, eff[k], side="left")
        hi = np.searchsorted(date_arr, eff[k + 1], side="left") if k + 1 < len(schedule) else len(date_arr)
        if hi <= lo:
            continue
        A = edge_matrix(snap.edges, symbols)
        pred = comove_predicted(R[lo:hi], A)
        out[lo:hi] = R[lo:hi] - pred
    return pd.DataFrame(out, index=panel.index, columns=symbols)


@dataclass(frozen=True)
class StatArbParams:
    """One residual-stat-arb variant (the grid the runner haircuts ``n_trials`` against)."""
    edge_window: int = 250
    refit_step: int = 60
    alpha: float = 0.05
    min_abs_corr: float = 0.0
    z_lookback: int = 60
    z_threshold: float = 0.0          # 0 ⇒ trade the full graded spread (no sparsity gate)
    accum_window: int = 5             # days of daily spread accumulated into the dislocation level
    min_obs: int = 60

    def label(self) -> str:
        return (f"ew{self.edge_window}_rs{self.refit_step}_a{self.alpha}"
                f"_mc{self.min_abs_corr}_zl{self.z_lookback}_zt{self.z_threshold}"
                f"_aw{self.accum_window}")


def statarb_signal(
    panel: pd.DataFrame,
    schedule: list[EdgeSnapshot],
    *,
    z_lookback: int = 60,
    z_threshold: float = 0.0,
    accum_window: int = 5,
) -> pd.DataFrame:
    """Raw (pre-normalization) trade signal: ``−z(dislocation)``, ``shift(1)`` for PIT.

    The **dislocation level** is the trailing sum of the daily peer-relative spread over
    ``accum_window`` days (Avellaneda–Lee s-score in spirit: a *level* that mean-reverts over
    several days, not a one-day blip). ``accum_window=1`` recovers the pure daily-reversal signal.
    Accumulating cuts turnover roughly ``accum_window``-fold — the difference between an edge that
    is eaten by costs and one that survives the venue-feasible book. The level is z-scored per
    name over a trailing ``z_lookback`` window, faded (mean reversion ⇒ minus sign), optionally
    gated to fire only on ``|z| ≥ z_threshold``, then ``shift(1)`` so a weight at date t uses only
    the dislocation known strictly before t. NaN (warm-up / peerless names) carry no position."""
    spread = residual_spread(panel, schedule)
    level = (spread.rolling(accum_window, min_periods=max(2, accum_window // 2)).sum()
             if accum_window > 1 else spread)
    mu = level.rolling(z_lookback, min_periods=max(2, z_lookback // 2)).mean()
    sd = level.rolling(z_lookback, min_periods=max(2, z_lookback // 2)).std()
    z = (level - mu) / sd.replace(0.0, np.nan)
    sig = -z
    if z_threshold > 0.0:
        sig = sig.where(z.abs() >= z_threshold, 0.0)
    return sig.shift(1)


def statarb_weights(
    panel: pd.DataFrame,
    sectors: dict[str, str],
    params: StatArbParams,
    *,
    schedule: list[EdgeSnapshot] | None = None,
) -> pd.DataFrame:
    """End-to-end variant weights: build (or reuse) the rolling edge schedule, form the faded
    z-spread signal, and map it to dollar-neutral unit-gross weights (the common book every
    baseline and candidate is expressed in — apples-to-apples net-Sharpe comparison). Pass a
    pre-built ``schedule`` to avoid re-estimating edges when only the z-params vary across the
    grid."""
    sched = schedule if schedule is not None else build_edge_schedule(
        panel, sectors, edge_window=params.edge_window, refit_step=params.refit_step,
        alpha=params.alpha, min_obs=params.min_obs, min_abs_corr=params.min_abs_corr)
    sig = statarb_signal(panel, sched, z_lookback=params.z_lookback,
                         z_threshold=params.z_threshold, accum_window=params.accum_window)
    return dollar_neutral_unit(sig.fillna(0.0))


def default_grid() -> list[StatArbParams]:
    """The honest search family for the data-mining haircut (DSR/PBO ``n_trials``).

    A small, deliberate grid over the axes that matter — peer-selection strictness
    (``min_abs_corr``), the dislocation horizon that governs turnover (``accum_window``), and the
    bet conversion (``z_threshold``) — so ``n_trials`` reflects what was *actually* tried, not 1
    (adversarial finding D1). ``accum_window`` is the axis that decides cost survival, so it is
    searched explicitly rather than hand-picked."""
    grid: list[StatArbParams] = []
    for accum_window in (1, 5, 10):
        for z_threshold in (0.0, 1.0):
            for min_abs_corr in (0.0, 0.2):
                grid.append(StatArbParams(
                    accum_window=accum_window, z_threshold=z_threshold,
                    min_abs_corr=min_abs_corr, z_lookback=60))
    return grid

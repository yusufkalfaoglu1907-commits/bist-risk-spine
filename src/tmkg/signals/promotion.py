"""Naïve-baseline ladder + the promotion gate (BUILD_PLAN.md M4 — the judge).

"Build the judge before the contestants." No candidate signal is trusted until it clears this
gate, and the gate is deliberately hard to flatter:

  1. **Beat the naïve-baseline ladder on the same PIT splits.** A candidate must out-earn three
     dumb-but-real baselines — anything that doesn't is not adding information over what a
     first-year quant would try:
       (a) **persistence / recurrence** — trailing-return momentum (what moved keeps moving);
       (b) **sector + FX differential exposure** — cross-sectional sort on trailing beta to a
           reference channel (FX / market), long high-minus-low;
       (c) **sparse own-factor event study** — react to a name's own outsized residual move
           (mean-reversion after a shock).
  2. **Survive the venue-feasible book**, not just frictionless research (backtest.py).
  3. **Deflated Sharpe Ratio passes** the trial-count haircut (stats.py) — never raw Sharpe.
  4. **PBO below threshold** — the in-sample dominance over the baselines must not be a
     cross-validation artifact.

``promoted`` is the AND of all four. A known-null (shuffled-label) signal fails on DSR / PBO
even if it luckily edges a baseline; a known-good toy clears every rung. That asymmetry is the
M4 exit-gate self-test (tests/signals/test_harness_selftest.py).

Pure compute (NumPy/pandas) — reads no L2 and no network. The caller pulls the residual/return
panels through PITAccess and the registry (registry.py) persists the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tmkg.signals.backtest import (
    VENUE_FEASIBLE,
    BacktestResult,
    BookConfig,
    CostModel,
    run_book,
)
from tmkg.signals.stats import (
    DSRResult,
    PBOResult,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)

_EPS = 1e-12


# --- weight normalization ---------------------------------------------------


def dollar_neutral_unit(scores: pd.DataFrame) -> pd.DataFrame:
    """Per-row: demean cross-sectionally (dollar-neutral long/short) then scale to unit gross
    (Σ|w| = 1). An all-equal or all-zero row maps to all-zero (no bet). This is the common
    book all baselines and candidates are expressed in, so net-Sharpe comparisons are apples
    to apples."""
    s = scores.subtract(scores.mean(axis=1), axis=0)
    gross = s.abs().sum(axis=1)
    w = s.div(gross.where(gross > _EPS, np.nan), axis=0)
    return w.fillna(0.0)


# --- the naïve-baseline ladder ----------------------------------------------


def baseline_persistence(returns: pd.DataFrame, *, lookback: int = 20) -> pd.DataFrame:
    """(a) Trailing-return momentum. Weight ∝ the past-``lookback`` mean return, known strictly
    before the holding period (``shift(1)``) — no lookahead."""
    mom = returns.rolling(lookback, min_periods=max(2, lookback // 2)).mean().shift(1)
    return dollar_neutral_unit(mom.fillna(0.0))


def baseline_differential_exposure(
    returns: pd.DataFrame,
    *,
    factor_returns: pd.Series | None = None,
    lookback: int = 60,
) -> pd.DataFrame:
    """(b) Sector/FX differential exposure: long the names with high trailing beta to a reference
    channel, short the low-beta names. The reference defaults to the cross-sectional mean return
    (a crude market/FX proxy) when no ``factor_returns`` series is supplied. Betas use only
    trailing data (``shift(1)``)."""
    f = returns.mean(axis=1) if factor_returns is None else factor_returns.reindex(returns.index)
    f = f.fillna(0.0)
    # rolling beta_i,t = cov(r_i, f) / var(f) over the trailing window, then shifted.
    fvar = f.rolling(lookback, min_periods=max(2, lookback // 2)).var()
    betas = {}
    for col in returns.columns:
        cov = returns[col].rolling(lookback, min_periods=max(2, lookback // 2)).cov(f)
        betas[col] = cov / fvar.replace(0.0, np.nan)
    beta_df = pd.DataFrame(betas, index=returns.index).shift(1)
    return dollar_neutral_unit(beta_df.fillna(0.0))


def baseline_own_factor_event(
    returns: pd.DataFrame,
    *,
    lookback: int = 20,
    z_threshold: float = 2.0,
) -> pd.DataFrame:
    """(c) Sparse own-factor event study: when a name's own most-recent return is a > ``z_threshold``
    sigma move vs its trailing distribution, fade it (mean-reversion after a shock). Sparse by
    construction — most names carry no position most days. Uses only trailing info (``shift(1)``)."""
    mu = returns.rolling(lookback, min_periods=max(2, lookback // 2)).mean()
    sd = returns.rolling(lookback, min_periods=max(2, lookback // 2)).std()
    z = ((returns - mu) / sd.replace(0.0, np.nan)).shift(1)
    signal = (-np.sign(z)).where(z.abs() >= z_threshold, 0.0)
    return dollar_neutral_unit(signal.fillna(0.0))


def baseline_ladder(
    returns: pd.DataFrame,
    *,
    factor_returns: pd.Series | None = None,
) -> dict[str, pd.DataFrame]:
    """All three naïve baselines as a name → weights map."""
    return {
        "persistence": baseline_persistence(returns),
        "differential_exposure": baseline_differential_exposure(
            returns, factor_returns=factor_returns),
        "own_factor_event": baseline_own_factor_event(returns),
    }


# --- the promotion gate -----------------------------------------------------


@dataclass(frozen=True)
class PromotionResult:
    """A candidate's full promotion verdict — exactly what registry.py logs."""
    promoted: bool
    beats_baselines: bool
    candidate_net_sharpe: float
    best_baseline_net_sharpe: float
    best_baseline: str
    dsr: DSRResult
    pbo: PBOResult
    book: str
    n_trials: int
    capacity_ok: bool
    baseline_net_sharpes: dict[str, float] = field(default_factory=dict)
    failed_checks: tuple[str, ...] = ()

    def summary(self) -> dict:
        return {
            "promoted": self.promoted, "beats_baselines": self.beats_baselines,
            "candidate_net_sharpe": self.candidate_net_sharpe,
            "best_baseline_net_sharpe": self.best_baseline_net_sharpe,
            "best_baseline": self.best_baseline, "book": self.book,
            "n_trials": self.n_trials, "capacity_ok": self.capacity_ok,
            "dsr": self.dsr.as_dict(), "pbo": self.pbo.as_dict(),
            "baseline_net_sharpes": self.baseline_net_sharpes,
            "failed_checks": list(self.failed_checks),
        }


def evaluate_candidate(
    candidate_weights: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    *,
    returns_for_baselines: pd.DataFrame | None = None,
    factor_returns: pd.Series | None = None,
    book: BookConfig = VENUE_FEASIBLE,
    cost_model: CostModel | None = None,
    short_eligible: pd.DataFrame | None = None,
    limit_lock: pd.DataFrame | None = None,
    n_trials: int = 1,
    sr_variance: float | None = None,
    pbo_threshold: float = 0.5,
    pbo_partitions: int = 10,
    capacity_floor: float = 0.0,
) -> PromotionResult:
    """Run a candidate's weights through the full gate and return the verdict.

    The candidate and the three baselines are all run through the **same book** on the **same
    forward returns** (apples to apples). Promotion requires *all* of:
      • the candidate's net Sharpe beats every baseline's;
      • its venue-book net Sharpe clears ``capacity_floor`` (survives frictions);
      • its Deflated Sharpe passes the ``n_trials`` haircut;
      • PBO over {candidate, baselines} is below ``pbo_threshold`` (in-sample dominance is not a
        CV artifact).
    ``returns_for_baselines`` is the (date × symbol) return panel the baselines build their
    signals from (defaults to ``fwd_returns``). ``n_trials`` is the honest count of variants the
    candidate was selected from — pass it large when you searched widely."""
    cm = cost_model or CostModel()
    base_panel = returns_for_baselines if returns_for_baselines is not None else fwd_returns
    baselines = baseline_ladder(base_panel, factor_returns=factor_returns)

    def _net(weights):
        return run_book(weights, fwd_returns, book=book, cost_model=cm,
                        short_eligible=short_eligible, limit_lock=limit_lock)

    cand_res = _net(candidate_weights)
    base_res = {name: _net(w) for name, w in baselines.items()}
    base_sharpes = {name: r.net_sharpe for name, r in base_res.items()}

    # NaN baseline Sharpe (degenerate book) treated as -inf so it cannot be the bar to clear.
    best_baseline = max(base_sharpes, key=lambda k: _nan_to_neg_inf(base_sharpes[k]))
    best_baseline_sharpe = base_sharpes[best_baseline]
    beats = _gt(cand_res.net_sharpe, best_baseline_sharpe)

    # DSR on the candidate's per-period net P&L, haircut by n_trials.
    dsr = deflated_sharpe_ratio(cand_res.pnl.to_numpy(), n_trials=n_trials,
                                sr_variance=sr_variance)

    # PBO over the per-period net P&L of {candidate, *baselines} as the strategy set.
    perf = pd.concat(
        [cand_res.pnl.rename("candidate")] + [base_res[n].pnl.rename(n) for n in baselines],
        axis=1,
    ).fillna(0.0)
    pbo = probability_of_backtest_overfitting(perf.to_numpy(), n_partitions=pbo_partitions)

    capacity_ok = _gt(cand_res.net_sharpe, capacity_floor) or \
        (np.isfinite(cand_res.net_sharpe) and cand_res.net_sharpe >= capacity_floor)

    checks = {
        "beats_baselines": beats,
        "capacity_clears_floor": capacity_ok,
        "dsr_passes": dsr.passes,
        "pbo_below_threshold": np.isfinite(pbo.pbo) and pbo.pbo < pbo_threshold,
    }
    failed = tuple(k for k, ok in checks.items() if not ok)
    return PromotionResult(
        promoted=all(checks.values()),
        beats_baselines=beats,
        candidate_net_sharpe=float(cand_res.net_sharpe) if np.isfinite(cand_res.net_sharpe) else float("nan"),
        best_baseline_net_sharpe=float(best_baseline_sharpe) if np.isfinite(best_baseline_sharpe) else float("nan"),
        best_baseline=best_baseline,
        dsr=dsr, pbo=pbo, book=book.name, n_trials=int(n_trials),
        capacity_ok=capacity_ok,
        baseline_net_sharpes={k: (float(v) if np.isfinite(v) else float("nan"))
                              for k, v in base_sharpes.items()},
        failed_checks=failed,
    )


def _gt(a: float, b: float) -> bool:
    """a > b with NaN-safe semantics (NaN never wins)."""
    return bool(np.isfinite(a) and (not np.isfinite(b) or a > b))


def _nan_to_neg_inf(x: float) -> float:
    return x if np.isfinite(x) else float("-inf")

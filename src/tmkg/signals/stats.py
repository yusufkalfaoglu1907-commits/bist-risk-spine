"""The judge's statistical core — Deflated Sharpe Ratio + PBO (BUILD_PLAN.md M4).

"Build the judge before the contestants." A pretty backtest is the easiest thing in this
project to fool yourself with: run enough variants and one will look great in-sample by pure
luck. These two statistics are the antidote, and they are built (and pinned) *before* the
first real signal (M5) so no signal is ever graded by a harness written to flatter it.

  - **Probabilistic Sharpe Ratio (PSR)** — Bailey & López de Prado (2012): the probability the
    *true* Sharpe exceeds a benchmark ``sr_star``, correcting the naïve Sharpe for sample
    length, skewness, and fat tails (a high Sharpe on 30 fat-tailed observations is not the
    same evidence as on 3000 Gaussian ones).
  - **Deflated Sharpe Ratio (DSR)** — PSR evaluated at the *expected maximum* Sharpe achievable
    by chance across ``n_trials`` independent backtests. This is the trial-count haircut: the
    more variants you tried, the higher the bar a winner must clear to be believed. The
    promotion gate keys on DSR, never raw in-sample Sharpe (VERIFICATION §3).
  - **PBO (Probability of Backtest Overfitting)** — Bailey et al. (2015) via Combinatorially
    Symmetric Cross-Validation (CSCV): across all in/out-of-sample splits, how often does the
    in-sample-best configuration land below the out-of-sample median? PBO ≈ 0.5 means the
    in-sample ranking carries no out-of-sample information — the selection is overfit.

Pure functions: NumPy/SciPy only, no L2, no network (this is L3 — the AST scan in
tests/invariants/test_no_network_in_signal_layer.py enforces it). The backtester (backtest.py)
and the promotion gate (promotion.py) feed real returns / performance matrices in here.

References:
  Bailey, López de Prado, "The Sharpe Ratio Efficient Frontier" (2012).
  Bailey, López de Prado, "The Deflated Sharpe Ratio" (2014), J. Portfolio Management.
  Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting" (2015/2017).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

import numpy as np
from scipy import stats as _ss

# Euler–Mascheroni constant — the order-statistic correction in the expected-max-Sharpe formula.
_EULER_GAMMA = 0.5772156649015329
_EPS = 1e-12

# Default confidence the DSR must clear to be a "pass". DSR is a probability in [0, 1] (the
# chance the true Sharpe beats the trial-count-adjusted benchmark); the design's shorthand
# "DSR > 0 after trial-count adjustment" means the deflated *excess* Sharpe is positive AND
# that positivity is statistically credible. We make the credibility threshold explicit and
# conservative rather than gating on a meaningless ">0 probability". Documented, not weakened.
DSR_CONFIDENCE = 0.95


def sharpe_ratio(returns, *, periods_per_year: int | None = None, ddof: int = 1) -> float:
    """Per-period Sharpe ``mean(r) / std(r)`` (excess over a zero benchmark; pass already-excess
    returns if you want a non-zero one). Annualized by ``sqrt(periods_per_year)`` when given.
    Zero/degenerate variance ⇒ ``nan`` (an undefined Sharpe is never silently 0 or inf)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=ddof)
    if sd <= _EPS:
        return float("nan")
    sr = r.mean() / sd
    if periods_per_year is not None:
        sr *= math.sqrt(periods_per_year)
    return float(sr)


def probabilistic_sharpe_ratio(
    returns,
    *,
    sr_star: float = 0.0,
    sr_observed: float | None = None,
) -> float:
    """PSR: P(true non-annualized Sharpe > ``sr_star``), corrected for sample length, skew and
    kurtosis (Bailey–LdP 2012). Returns a probability in [0, 1].

    Uses the *non-annualized* (per-period) Sharpe throughout — annualization cancels out of the
    test statistic and mixing the two is a classic bug. ``sr_observed`` overrides the Sharpe
    computed from ``returns`` (used by DSR, which supplies the same per-period SR)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 3:
        return float("nan")
    sr = sharpe_ratio(r) if sr_observed is None else float(sr_observed)
    if not np.isfinite(sr):
        return float("nan")
    skew = float(_ss.skew(r, bias=False))
    kurt = float(_ss.kurtosis(r, fisher=False, bias=False))  # non-excess (normal = 3)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= _EPS:
        return float("nan")
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(_ss.norm.cdf(z))


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """Expected maximum (per-period) Sharpe achievable across ``n_trials`` independent strategies
    under the null of *zero* true skill, given the cross-trial variance ``sr_variance`` of the
    estimated Sharpes (Bailey–LdP 2014, eq. for E[max]). This is the benchmark DSR deflates to —
    it grows with both how many variants you tried and how dispersed their luck was.

    ``E[max] ≈ sqrt(V)·[(1-γ)·Z⁻¹(1 - 1/N) + γ·Z⁻¹(1 - 1/(N·e))]`` with γ the Euler constant."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if sr_variance < 0:
        raise ValueError("sr_variance must be >= 0")
    if n_trials == 1 or sr_variance <= _EPS:
        return 0.0
    inv = _ss.norm.ppf
    z1 = inv(1.0 - 1.0 / n_trials)
    z2 = inv(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance) * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2))


@dataclass(frozen=True)
class DSRResult:
    """A deflated-Sharpe verdict (everything the registry logs about the haircut)."""
    dsr: float                # P(true SR > deflated benchmark) ∈ [0, 1]
    sr_observed: float        # per-period observed Sharpe
    sr_benchmark: float       # expected-max Sharpe under the null (the deflation target)
    n_obs: int
    n_trials: int
    skew: float
    kurtosis: float           # non-excess (normal = 3)
    passes: bool              # dsr >= confidence (default 0.95)

    def as_dict(self) -> dict:
        return {
            "dsr": self.dsr, "sr_observed": self.sr_observed,
            "sr_benchmark": self.sr_benchmark, "n_obs": self.n_obs,
            "n_trials": self.n_trials, "skew": self.skew, "kurtosis": self.kurtosis,
            "passes": self.passes,
        }


def deflated_sharpe_ratio(
    returns,
    *,
    n_trials: int,
    sr_variance: float | None = None,
    trial_sharpes=None,
    confidence: float = DSR_CONFIDENCE,
) -> DSRResult:
    """Deflated Sharpe Ratio: PSR evaluated at the expected-max Sharpe of ``n_trials`` (the
    trial-count haircut). The honest replacement for raw in-sample Sharpe in the promotion gate.

    The deflation benchmark needs the cross-trial Sharpe variance: pass ``sr_variance`` directly,
    or ``trial_sharpes`` (the per-trial Sharpes, variance taken with ddof=1), or neither — in
    which case it falls back to the variance of ``returns``-implied Sharpe sampling noise
    ``(1 + sr²/2)/n`` (the analytic single-strategy estimator-variance, a conservative floor).

    A genuinely null strategy (Sharpe ≈ 0) gives ``sr_observed < sr_benchmark`` ⇒ DSR < 0.5 ⇒
    ``passes=False``; a strong strategy clears the deflated bar ⇒ DSR → 1. That asymmetry is the
    whole point — it is what makes the M4 known-null self-test fail and the known-good pass."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    sr = sharpe_ratio(r)
    skew = float(_ss.skew(r, bias=False)) if n >= 3 else float("nan")
    kurt = float(_ss.kurtosis(r, fisher=False, bias=False)) if n >= 4 else float("nan")

    if sr_variance is None:
        if trial_sharpes is not None:
            ts = np.asarray(trial_sharpes, dtype=float)
            ts = ts[np.isfinite(ts)]
            sr_variance = float(ts.var(ddof=1)) if ts.size >= 2 else 0.0
        elif np.isfinite(sr) and n >= 2:
            sr_variance = float((1.0 + 0.5 * sr * sr) / n)  # estimator-variance floor
        else:
            sr_variance = 0.0

    sr_benchmark = expected_max_sharpe(sr_variance, n_trials)
    dsr = probabilistic_sharpe_ratio(r, sr_star=sr_benchmark, sr_observed=sr)
    passes = bool(np.isfinite(dsr) and dsr >= confidence)
    return DSRResult(
        dsr=float(dsr) if np.isfinite(dsr) else float("nan"),
        sr_observed=float(sr) if np.isfinite(sr) else float("nan"),
        sr_benchmark=float(sr_benchmark), n_obs=int(n), n_trials=int(n_trials),
        skew=skew, kurtosis=kurt, passes=passes,
    )


@dataclass(frozen=True)
class PBOResult:
    """A CSCV overfitting verdict."""
    pbo: float                # P(IS-best ranks below OOS median) ∈ [0, 1]
    n_splits: int             # number of CSCV combinations evaluated
    n_strategies: int
    n_partitions: int         # S
    median_logit: float       # median λ; > 0 ⇒ IS-best tends to stay above OOS median
    as_dict = lambda self: {  # noqa: E731 — tiny serializer, mirrors DSRResult.as_dict
        "pbo": self.pbo, "n_splits": self.n_splits, "n_strategies": self.n_strategies,
        "n_partitions": self.n_partitions, "median_logit": self.median_logit,
    }


def _stat_sharpe_cols(M: np.ndarray) -> np.ndarray:
    """Per-column (per-strategy) per-period Sharpe of a (T × N) performance matrix."""
    mu = M.mean(axis=0)
    sd = M.std(axis=0, ddof=1)
    out = np.full(M.shape[1], np.nan)
    ok = sd > _EPS
    out[ok] = mu[ok] / sd[ok]
    return out


def probability_of_backtest_overfitting(
    performance,
    *,
    n_partitions: int = 16,
    performance_fn=None,
) -> PBOResult:
    """PBO via Combinatorially Symmetric Cross-Validation (Bailey et al. 2015).

    ``performance`` is a (T observations × N strategies) matrix of per-period performance
    (returns by default). The rows are split into ``n_partitions`` (S, even) disjoint blocks;
    for every way to choose S/2 blocks as in-sample J (complement = out-of-sample J̄):
      1. pick the strategy maximizing the in-sample statistic (Sharpe) — the one a researcher
         would have selected;
      2. find its *relative rank* ω ∈ (0,1) among all strategies' out-of-sample statistics;
      3. record the logit λ = ln(ω/(1-ω)).
    PBO = fraction of splits with λ ≤ 0 — i.e. how often the in-sample winner is an
    out-of-sample dog. PBO ≈ 0.5 ⇒ in-sample selection is pure overfit; PBO → 0 ⇒ the winner
    generalizes. ``performance_fn`` overrides the per-column statistic (default: Sharpe)."""
    M = np.asarray(performance, dtype=float)
    if M.ndim != 2:
        raise ValueError("performance must be 2-D (T observations x N strategies)")
    T, N = M.shape
    if n_partitions % 2 != 0:
        raise ValueError("n_partitions (S) must be even for symmetric in/out splits")
    if N < 2:
        raise ValueError("PBO needs >= 2 strategies to rank one against the others")
    if T < n_partitions:
        raise ValueError(f"need >= n_partitions ({n_partitions}) rows; have {T}")

    stat = performance_fn if performance_fn is not None else _stat_sharpe_cols

    # Contiguous, near-equal blocks (drop the tail remainder so blocks are equal-sized — CSCV
    # assumes exchangeable equal partitions). Index-based so any row ordering is preserved.
    block_len = T // n_partitions
    blocks = [np.arange(i * block_len, (i + 1) * block_len) for i in range(n_partitions)]
    all_idx = set(range(n_partitions))

    logits: list[float] = []
    for combo in combinations(range(n_partitions), n_partitions // 2):
        is_rows = np.concatenate([blocks[i] for i in combo])
        oos_rows = np.concatenate([blocks[i] for i in sorted(all_idx - set(combo))])
        is_stat = stat(M[is_rows])
        oos_stat = stat(M[oos_rows])
        if not np.isfinite(is_stat).any() or not np.isfinite(oos_stat).any():
            continue
        n_star = int(np.nanargmax(is_stat))
        # Relative rank of the IS-best among OOS stats (1 = worst .. N = best); ω in (0,1).
        finite = np.isfinite(oos_stat)
        order = oos_stat[finite].argsort().argsort()  # 0-based ranks among finite cols
        rank_map = dict(zip(np.where(finite)[0].tolist(), (order + 1).tolist()))
        if n_star not in rank_map:
            continue
        k = finite.sum()
        omega = rank_map[n_star] / (k + 1.0)             # ∈ (0,1), never exactly 0/1
        omega = min(max(omega, _EPS), 1.0 - _EPS)
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        return PBOResult(pbo=float("nan"), n_splits=0, n_strategies=N,
                         n_partitions=n_partitions, median_logit=float("nan"))
    arr = np.asarray(logits)
    pbo = float((arr <= 0).mean())
    return PBOResult(pbo=pbo, n_splits=int(arr.size), n_strategies=N,
                     n_partitions=n_partitions, median_logit=float(np.median(arr)))

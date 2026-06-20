"""Rolling, regime-aware factor betas with Ledoit–Wolf shrinkage (BUILD_PLAN.md M2).

The estimator behind the neutralization ladder. For each name it regresses USD-primary
total returns on the factor-return panel over a trailing window, producing **multiple-
regression (partial) betas** — the right object for neutralization: a partial beta on
USD/TRY already controls for the market, so stripping factors in order does not double-
count shared variance (system-design-v2.md §200).

Three design rules baked in here:

1. **Shrinkage, because we are near ``p ≈ n`` (§201).** With a dozen-plus factors and a
   ~60-day window the sample factor covariance is ill-conditioned; inverting it raw
   gives unstable betas. We shrink the *factor* covariance with Ledoit–Wolf before
   inversion (β = Σ_XX⁻¹ σ_Xy on the shrunk Σ_XX). OLS is offered for comparison/tests.

2. **Regime-aware, no straddle (§65).** Each window is restricted to observations in the
   *same regime as its end date*; a window that would span a structural break (e.g. 19
   Mar 2025) is shrunk below ``min_obs`` and refused, so betas re-estimate cleanly on
   each side instead of being smeared across the shock. This is what makes "betas break
   across the boundary" true by construction.

3. **No fabrication (§4).** A window with any NaN return row drops that row; a window
   left with fewer than ``min_obs`` usable rows emits **no beta** (never a guessed one).
   ``knowledge_date = bar_date`` of the window end — the beta uses only returns whose own
   knowledge_date ≤ that date, so it inherits the PIT gate.

Pure: DataFrames in, a long DataFrame out (matching L2 ``betas`` schema). The L2/PIT
plumbing that feeds it and lands the result lives in ``ingest.pipeline`` (a later slice).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from tmkg.factors.regime import regime_for_date

LEDOIT_WOLF = "ledoit_wolf"
OLS = "ols"
_METHODS = frozenset({LEDOIT_WOLF, OLS})

_BETA_COLUMNS = [
    "symbol", "factor", "bar_date", "beta", "method",
    "window", "regime", "universe_class", "knowledge_date",
]


def factor_panel(factor_returns: pd.DataFrame) -> pd.DataFrame:
    """Pivot long ``[factor, bar_date, ret]`` to a wide ``bar_date × factor`` panel.

    Column order is preserved as first-seen — the neutralization ladder relies on a
    deterministic factor order, so we do not sort columns alphabetically.
    """
    needed = {"factor", "bar_date", "ret"}
    missing = needed - set(factor_returns.columns)
    if missing:
        raise ValueError(f"factor_panel: factor_returns missing {sorted(missing)}")
    fr = factor_returns.copy()
    fr["bar_date"] = pd.to_datetime(fr["bar_date"]).dt.date
    order = list(dict.fromkeys(fr["factor"]))  # first-seen factor order
    wide = fr.pivot_table(index="bar_date", columns="factor", values="ret", aggfunc="last")
    return wide[order].sort_index()


def _estimate_betas(X: np.ndarray, y: np.ndarray, method: str) -> np.ndarray:
    """Partial betas of ``y`` on the columns of ``X`` (both already finite, n×k / n)."""
    Xc = X - X.mean(axis=0)
    yc = y - y.mean()
    n = Xc.shape[0]
    sigma_xy = (Xc.T @ yc) / (n - 1)
    if method == LEDOIT_WOLF:
        sigma_xx = LedoitWolf(assume_centered=True).fit(Xc).covariance_
    elif method == OLS:
        sigma_xx = (Xc.T @ Xc) / (n - 1)
    else:
        raise ValueError(f"unknown beta method {method!r}; expected one of {sorted(_METHODS)}")
    # solve Σ_xx β = σ_xy; lstsq is robust to a residually-singular Σ_xx (k=1 too).
    beta, *_ = np.linalg.lstsq(sigma_xx, sigma_xy, rcond=None)
    return beta


def rolling_factor_betas(
    returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    *,
    symbol: str | None = None,
    window: int = 60,
    min_obs: int | None = None,
    method: str = LEDOIT_WOLF,
    universe_class: str | None = None,
    regime_of: Callable[[date], str] = regime_for_date,
    factors: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Rolling regime-aware partial betas of one name on the factor panel.

    Parameters
    ----------
    returns : one symbol's return series — columns ``[bar_date, ret]`` (+ optional
        ``symbol``). Use the USD-primary ``ret_usd`` upstream; this function is
        base-agnostic and just regresses the ``ret`` column it is given.
    factor_returns : long ``[factor, bar_date, ret]`` (the output of
        ``factors.series.compute_factor_returns``). Pivoted internally.
    window : trailing observation count per estimate (the *maximum* lookback; the
        regime restriction may use fewer).
    min_obs : minimum usable rows after NaN-drop and regime restriction; below this no
        beta is emitted. Defaults to ``window`` (a full in-regime window), which is what
        enforces the no-straddle break — relax it for short histories.
    method : ``ledoit_wolf`` (shrunk, default) or ``ols``.
    regime_of : ``date -> regime label``; each window is confined to the end date's regime.
    factors : restrict/order the panel to these factors (default: all, first-seen order).

    Returns a long frame in L2 ``betas`` column order, one row per (factor, end-date)
    where an estimate was possible. Empty frame (typed columns) if none were.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown beta method {method!r}; expected one of {sorted(_METHODS)}")
    if min_obs is None:
        min_obs = window

    sym = symbol or (returns["symbol"].iloc[0] if "symbol" in returns.columns and len(returns)
                     else None)
    if sym is None:
        raise ValueError("rolling_factor_betas: no symbol (pass symbol= or a column)")

    y = returns[["bar_date", "ret"]].copy()
    y["bar_date"] = pd.to_datetime(y["bar_date"]).dt.date
    y = y.dropna(subset=["ret"]).drop_duplicates("bar_date").set_index("bar_date")["ret"]

    panel = factor_panel(factor_returns)
    if factors is not None:
        panel = panel[list(factors)]
    factor_names = list(panel.columns)

    # Align stock + factors on the shared calendar; a date missing on either side is
    # simply absent (no fabricated point). Sorted ascending for the rolling sweep.
    joined = panel.join(y.rename("__y__"), how="inner").sort_index()
    dates = list(joined.index)
    if not dates or min_obs < 1:
        return pd.DataFrame(columns=_BETA_COLUMNS)

    rows: list[dict] = []
    for i in range(len(dates)):
        end = dates[i]
        end_regime = regime_of(end)
        lo = max(0, i - window + 1)
        win = joined.iloc[lo : i + 1]
        # confine to the end date's regime (no straddle) and drop any NaN row (§4)
        win = win[[regime_of(d) == end_regime for d in win.index]].dropna()
        if len(win) < min_obs:
            continue
        X = win[factor_names].to_numpy(dtype=float)
        yv = win["__y__"].to_numpy(dtype=float)
        beta = _estimate_betas(X, yv, method)
        for fname, b in zip(factor_names, beta):
            rows.append(
                {
                    "symbol": sym,
                    "factor": fname,
                    "bar_date": end,
                    "beta": float(b),
                    "method": method,
                    "window": int(window),
                    "regime": end_regime,
                    "universe_class": universe_class,
                    "knowledge_date": end,  # uses only returns known by `end`
                }
            )

    if not rows:
        return pd.DataFrame(columns=_BETA_COLUMNS)
    return pd.DataFrame(rows)[_BETA_COLUMNS]

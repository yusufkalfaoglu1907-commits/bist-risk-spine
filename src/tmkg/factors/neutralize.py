"""The neutralization ladder — residual returns by explicit hierarchical strip (M2).

system-design-v2.md §200: strip common factors in an **explicit hierarchical order**
so the residual claim is *falsifiable* rather than aspirational —

    market → FX (USD/TRY, EUR/TRY) → rates/CDS → energy/commodity → sector
           → foreign-flow/ownership-tier → holding-group → residual

"A residual linkage is only trusted once it is shown *not* to be a disguised bet on
USD/TRY, Turkey CDS, oil, or a holding-cluster." This module computes the residual by
that exact ladder.

How the order is honoured *and* the residual ends up orthogonal to **every** stripped
factor (the M2 exit-gate guarantee): we orthogonalise the factor panel in the ladder
order (modified Gram–Schmidt), then project the return onto that ordered orthogonal
basis and subtract. The final residual is orthogonal to the whole stripped span — so it
is provably not a disguised bet on any single stripped factor — while the *order*
governs how shared variance is attributed across the rungs (earlier rungs get priority).
Mathematically the residual equals the joint-OLS residual; the ladder makes the
attribution explicit and the claim testable.

Why OLS projection here and not the Ledoit–Wolf betas of ``betas.py``: shrinkage
deliberately biases betas for a stable covariance *inverse* (§201, used for the M3
residual-network), but a shrunk strip leaves the residual slightly correlated with the
factors — which would break the orthogonality claim. Neutralization is the one place
the unshrunk projection is the right tool; the residual *covariance* gets shrinkage
later, in M3.

Pure: arrays / DataFrames in, residuals out. No network, no L2, no PIT.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

import numpy as np
import pandas as pd

from tmkg.factors.betas import factor_panel
from tmkg.factors.regime import regime_for_date

# The canonical ladder, by factor *role*. Concrete factor names (USDTRY, XU100, BRENT…)
# are mapped onto these roles by the caller; the order is what must not change silently.
DEFAULT_LADDER: tuple[str, ...] = (
    "market", "fx", "rates_cds", "energy", "sector", "foreign_flow", "holding",
)

_RESIDUAL_COLUMNS = ["symbol", "bar_date", "residual", "strip_order",
                     "universe_class", "knowledge_date"]
_EPS = 1e-12


def strip_residual(
    y: np.ndarray, F: np.ndarray, *, return_stages: bool = False
):
    """Strip the columns of ``F`` (in their given order) out of ``y`` by ordered
    orthogonalisation, returning the residual orthogonal to every column of ``F``.

    ``y`` : (n,) returns. ``F`` : (n, k) factor panel, columns already in ladder order.
    Both are demeaned internally (an intercept is always removed). A factor column that
    is collinear with earlier ones (zero residual norm) is skipped — it adds no new
    direction, so it strips nothing and cannot fabricate one.

    If ``return_stages``, also returns a list of per-rung variance fractions removed
    (the attribution): fraction of ``var(y)`` taken by each factor *after* the earlier
    rungs, i.e. its marginal contribution given the ladder order.
    """
    y = np.asarray(y, dtype=float)
    F = np.asarray(F, dtype=float)
    if F.ndim == 1:
        F = F.reshape(-1, 1)
    yc = y - y.mean()
    if F.shape[1] == 0:
        return (yc, []) if return_stages else yc

    Fc = F - F.mean(axis=0)
    # Authoritative residual: a stable least-squares projection onto the centred factor
    # span. The normal equations make it exactly orthogonal to every column of Fc (to
    # machine precision) — this is the joint-OLS residual, and the orthogonality claim
    # rests on it rather than on a single-pass Gram–Schmidt (which loses orthogonality
    # when factors are highly collinear, exactly the BIST p≈n case).
    beta, *_ = np.linalg.lstsq(Fc, yc, rcond=None)
    r = yc - Fc @ beta
    if not return_stages:
        return r

    # Attribution only: strip the factors sequentially in ladder order to measure the
    # marginal variance each rung removes *after* the earlier rungs. The final residual
    # equals ``r`` above (same span); the order governs how shared variance is assigned.
    total_var = float(yc @ yc)
    rr = yc.copy()
    basis: list[np.ndarray] = []
    stages: list[float] = []
    for j in range(Fc.shape[1]):
        q = Fc[:, j].copy()
        for qi in basis:  # orthogonalise against directions already stripped
            denom = qi @ qi
            if denom > _EPS:
                q = q - (q @ qi) / denom * qi
        denom = q @ q
        if denom <= _EPS:
            stages.append(0.0)  # collinear factor: no new direction, strips nothing
            continue
        before = float(rr @ rr)
        rr = rr - (rr @ q) / denom * q
        after = float(rr @ rr)
        basis.append(q)
        stages.append((before - after) / total_var if total_var > _EPS else 0.0)
    return r, stages


def neutralize_window(
    y: pd.Series, panel: pd.DataFrame, order: Sequence[str]
) -> pd.Series:
    """Residualise a single aligned window. ``y`` and ``panel`` share a date index;
    ``order`` lists the panel columns in ladder order. Returns the residual Series
    (same index), orthogonal to each column of ``panel[order]``.
    """
    cols = [c for c in order if c in panel.columns]
    F = panel[cols].to_numpy(dtype=float)
    res = strip_residual(y.to_numpy(dtype=float), F)
    return pd.Series(res, index=y.index, name="residual")


def rolling_residuals(
    returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    *,
    order: Sequence[str],
    symbol: str | None = None,
    window: int = 60,
    min_obs: int | None = None,
    universe_class: str | None = None,
    regime_of: Callable[[date], str] = regime_for_date,
) -> pd.DataFrame:
    """Rolling, regime-confined residual-return series for one name (L2 ``residuals``).

    For each end date ``t`` the trailing ``window`` of (return, factor) rows is confined
    to ``t``'s regime (no straddle — same discipline as the beta engine), the ladder is
    applied, and the residual of ``t``'s own observation is taken (rolling in-sample
    residual). A window with fewer than ``min_obs`` usable rows emits nothing (§4).

    ``order`` is the concrete factor-name strip order (a mapping of DEFAULT_LADDER roles
    onto the factor names present). It is recorded verbatim in ``strip_order`` so each
    residual row is self-describing and the ladder is auditable.

    ``knowledge_date = bar_date``: the residual at ``t`` uses only returns known by ``t``.
    """
    if min_obs is None:
        min_obs = window
    sym = symbol or (returns["symbol"].iloc[0] if "symbol" in returns.columns and len(returns)
                     else None)
    if sym is None:
        raise ValueError("rolling_residuals: no symbol (pass symbol= or a column)")

    y = returns[["bar_date", "ret"]].copy()
    y["bar_date"] = pd.to_datetime(y["bar_date"]).dt.date
    y = y.dropna(subset=["ret"]).drop_duplicates("bar_date").set_index("bar_date")["ret"]

    panel = factor_panel(factor_returns)
    cols = [c for c in order if c in panel.columns]
    joined = panel[cols].join(y.rename("__y__"), how="inner").sort_index()
    dates = list(joined.index)
    order_str = ">".join(order)

    rows: list[dict] = []
    for i in range(len(dates)):
        end = dates[i]
        end_regime = regime_of(end)
        lo = max(0, i - window + 1)
        win = joined.iloc[lo : i + 1]
        win = win[[regime_of(d) == end_regime for d in win.index]].dropna()
        if len(win) < min_obs or end not in win.index:
            continue
        res = strip_residual(win["__y__"].to_numpy(dtype=float),
                             win[cols].to_numpy(dtype=float))
        rows.append(
            {
                "symbol": sym,
                "bar_date": end,
                "residual": float(res[list(win.index).index(end)]),
                "strip_order": order_str,
                "universe_class": universe_class,
                "knowledge_date": end,
            }
        )
    if not rows:
        return pd.DataFrame(columns=_RESIDUAL_COLUMNS)
    return pd.DataFrame(rows)[_RESIDUAL_COLUMNS]

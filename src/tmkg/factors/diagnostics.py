"""M2 exit-gate evaluators — the *judge* for the factor model (BUILD_PLAN.md M2).

The M2 exit gate has four criteria. Two are guarded structurally already — residual
orthogonality (``tests/invariants/test_neutralization_orthogonality.py``) and "no factor
silently dropped" (``ingest.pipeline.factor_coverage`` / ``require_all``). The other two are
*measurements over a real fit* and had only synthetic mechanism tests:

  - **"factor model explains a plausible variance share per universe_class"** —
    ``factor_variance_share`` + ``variance_share_by_class``: R² = 1 − var(residual)/var(ret).
  - **"betas are stable within a regime and break across the 2025-03-19 shock"** —
    ``assess_regime_break``: the across-boundary beta shift measured against the
    within-regime wobble (a break only counts if it dominates the noise floor).

Built **before** the live data lands, on purpose (BUILD_PLAN sequencing rule 2: build the
judge before the contestants, or you will rationalise a flattering fit). The live session
fits the model, then ``m2_gate_diagnostics`` reads the landed L2 back through PIT and emits
the go/no-go numbers — no eyeballing.

Pure: the two measurement functions take DataFrames and return DataFrames (no network, no
L2, no PIT). Only ``m2_gate_diagnostics`` touches L2, and only through ``PITAccess``.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

# The exit-gate-named boundary: betas must break across the 19-Mar-2025 İmamoğlu shock.
# These labels come from ``factors.regime.BIST_REGIME_BREAKS`` (the regime on each side).
SHOCK_REGIME_BEFORE = "orthodox_turn_2023"
SHOCK_REGIME_AFTER = "imamoglu_shock_2025"

_EPS = 1e-12


def factor_variance_share(
    returns: pd.DataFrame,
    residuals: pd.DataFrame,
    *,
    min_obs: int = 20,
) -> pd.DataFrame:
    """Per-name explained-variance share of the factor model.

    ``returns``  : ``[symbol, bar_date, ret]`` (USD-primary total returns).
    ``residuals``: ``[symbol, bar_date, residual, universe_class]`` (the neutralized series).

    For each name, on the dates present in **both**, R² = 1 − var(residual)/var(ret): the
    fraction of return variance the stripped factors removed. A name with fewer than
    ``min_obs`` overlapping dates, or a degenerate (~zero) return variance, emits nothing —
    never a fabricated share. R² is reported raw (it can go slightly <0 for a name whose
    rolling in-sample residual variance exceeds its return variance — that is a real signal
    the model fits that name poorly, not something to clip away).

    Returns ``[symbol, universe_class, n_obs, var_ret, var_residual, r2]``.
    """
    need_r = {"symbol", "bar_date", "ret"}
    need_e = {"symbol", "bar_date", "residual"}
    if not need_r <= set(returns.columns):
        raise ValueError(f"factor_variance_share: returns missing {sorted(need_r - set(returns.columns))}")
    if not need_e <= set(residuals.columns):
        raise ValueError(f"factor_variance_share: residuals missing {sorted(need_e - set(residuals.columns))}")

    r = returns[["symbol", "bar_date", "ret"]].copy()
    e_cols = ["symbol", "bar_date", "residual"]
    has_class = "universe_class" in residuals.columns
    if has_class:
        e_cols.append("universe_class")
    e = residuals[e_cols].copy()
    for df in (r, e):
        df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date

    merged = r.merge(e, on=["symbol", "bar_date"], how="inner").dropna(subset=["ret", "residual"])

    rows: list[dict] = []
    for sym, g in merged.groupby("symbol", sort=False):
        if len(g) < min_obs:
            continue
        var_ret = float(np.var(g["ret"].to_numpy(), ddof=1))
        if var_ret <= _EPS:
            continue  # no variance to explain — a share would be meaningless
        var_res = float(np.var(g["residual"].to_numpy(), ddof=1))
        uclass = (g["universe_class"].dropna().iloc[0]
                  if has_class and g["universe_class"].notna().any() else None)
        rows.append({
            "symbol": sym, "universe_class": uclass, "n_obs": int(len(g)),
            "var_ret": var_ret, "var_residual": var_res,
            "r2": 1.0 - var_res / var_ret,
        })
    return pd.DataFrame(
        rows, columns=["symbol", "universe_class", "n_obs", "var_ret", "var_residual", "r2"]
    )


def variance_share_by_class(per_symbol: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ``factor_variance_share`` rows to a per-``universe_class`` summary — the
    exit-gate object ("a plausible variance share *per class*"). Median is the headline
    (robust to a few badly-fit names); IQR shows the spread.

    Returns ``[universe_class, n_names, r2_median, r2_p25, r2_p75]`` (a NULL class is
    reported as its own group, not dropped — coverage must be visible)."""
    if per_symbol.empty:
        return pd.DataFrame(columns=["universe_class", "n_names", "r2_median", "r2_p25", "r2_p75"])
    g = per_symbol.copy()
    g["universe_class"] = g["universe_class"].where(g["universe_class"].notna(), "(unclassified)")
    out = (
        g.groupby("universe_class")["r2"]
        .agg(n_names="count",
             r2_median="median",
             r2_p25=lambda s: s.quantile(0.25),
             r2_p75=lambda s: s.quantile(0.75))
        .reset_index()
        .sort_values("universe_class")
        .reset_index(drop=True)
    )
    return out


def assess_regime_break(
    betas: pd.DataFrame,
    *,
    regime_before: str = SHOCK_REGIME_BEFORE,
    regime_after: str = SHOCK_REGIME_AFTER,
    factors: list[str] | None = None,
    min_per_regime: int = 5,
) -> pd.DataFrame:
    """Quantify how hard betas move across a regime boundary, per factor.

    For each (symbol, factor) the betas in ``regime_before`` and ``regime_after`` each get a
    mean and a within-regime std (the pooled std is the **noise floor** — how much a beta
    wobbles *inside* a stable regime). The shift ``|mean_after − mean_before|`` is measured
    against that floor: ``break_ratio = shift / (pooled_within_std + eps)``. A name needs
    ``min_per_regime`` betas on each side or it is skipped (no straddle-window beta exists at
    the boundary by construction — that gap is expected).

    The aggregate per factor is the **median break_ratio** across names: ≫1 means betas
    genuinely break across the shock (the gate's expectation); ≈1 or below means the model's
    betas are indistinguishable across the boundary (a red flag worth surfacing).

    Returns ``[factor, n_names, median_shift, median_within_std, median_break_ratio]``.
    """
    need = {"symbol", "factor", "beta", "regime"}
    if not need <= set(betas.columns):
        raise ValueError(f"assess_regime_break: betas missing {sorted(need - set(betas.columns))}")
    b = betas[betas["regime"].isin([regime_before, regime_after])].copy()
    if factors is not None:
        b = b[b["factor"].isin(factors)]

    rows: list[dict] = []
    for (sym, fac), g in b.groupby(["symbol", "factor"], sort=False):
        before = g.loc[g["regime"] == regime_before, "beta"].dropna().to_numpy()
        after = g.loc[g["regime"] == regime_after, "beta"].dropna().to_numpy()
        if len(before) < min_per_regime or len(after) < min_per_regime:
            continue
        shift = abs(float(after.mean()) - float(before.mean()))
        # pooled within-regime std: the noise floor a real break must clear
        within = float(np.sqrt(0.5 * (np.var(before, ddof=1) + np.var(after, ddof=1))))
        rows.append({
            "symbol": sym, "factor": fac, "shift": shift, "within_std": within,
            "break_ratio": shift / (within + _EPS),
        })
    per = pd.DataFrame(rows, columns=["symbol", "factor", "shift", "within_std", "break_ratio"])
    if per.empty:
        return pd.DataFrame(columns=["factor", "n_names", "median_shift",
                                     "median_within_std", "median_break_ratio"])
    return (
        per.groupby("factor")
        .agg(n_names=("symbol", "count"),
             median_shift=("shift", "median"),
             median_within_std=("within_std", "median"),
             median_break_ratio=("break_ratio", "median"))
        .reset_index()
        .sort_values("median_break_ratio", ascending=False)
        .reset_index(drop=True)
    )


def m2_gate_diagnostics(
    store,
    *,
    as_of: date,
    break_factors: list[str] | None = None,
    regime_before: str = SHOCK_REGIME_BEFORE,
    regime_after: str = SHOCK_REGIME_AFTER,
    min_obs: int = 20,
    min_per_regime: int = 5,
) -> dict:
    """Read the landed M2 outputs (``total_returns``, ``residuals``, ``betas``) back through
    PIT at ``as_of`` and emit the two exit-gate measurements as a report-ready dict. This is
    the only L2-touching function here, and it reads only through ``PITAccess`` — the same
    gate signal code is held to (§4). Called by the live session after the fit; the pure
    functions above are what the synthetic tests pin.
    """
    from tmkg.pit.access import PITAccess  # local import keeps the module network/L2-free at import

    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        tr = pit.series("total_returns")
        res = pit.series("residuals")
        bet = pit.series("betas")
    finally:
        con.close()

    returns = (tr[["symbol", "bar_date", "ret_usd"]].rename(columns={"ret_usd": "ret"})
               if not tr.empty else pd.DataFrame(columns=["symbol", "bar_date", "ret"]))
    per_symbol = factor_variance_share(returns, res, min_obs=min_obs) if not res.empty \
        else pd.DataFrame(columns=["symbol", "universe_class", "n_obs", "var_ret", "var_residual", "r2"])
    by_class = variance_share_by_class(per_symbol)
    breaks = (assess_regime_break(bet, regime_before=regime_before, regime_after=regime_after,
                                  factors=break_factors, min_per_regime=min_per_regime)
              if not bet.empty else
              pd.DataFrame(columns=["factor", "n_names", "median_shift",
                                    "median_within_std", "median_break_ratio"]))
    return {
        "as_of": str(as_of),
        "variance_share_by_class": by_class.to_dict("records"),
        "n_names_scored": int(len(per_symbol)),
        "regime_break": {
            "regime_before": regime_before, "regime_after": regime_after,
            "by_factor": breaks.to_dict("records"),
        },
    }

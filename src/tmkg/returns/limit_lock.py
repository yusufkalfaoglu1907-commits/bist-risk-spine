"""Limit-lock censoring (CLAUDE.md §5, design §3, line 68).

BIST imposes daily price limits (±10%, widened for market-maker names). A name
that "wants" to move 30% prints +10% three days running — a **censored** return.
Raw daily returns on limit-lock days are not true returns; treating them as such
corrupts betas, correlations and event CARs.

Two pure steps here:

1. ``flag_limit_lock`` — mark each bar whose close-to-close move is pinned at the
   band as ``is_limit_lock`` (detection, run at ingestion on the price series).
2. ``censor_lock_windows`` — replace the censored daily returns in a maximal lock
   run with a single **cumulative return across the lock window** (the run plus the
   resolving day on which the true price is revealed), and flag every touched row
   ``limit_lock_adj``. The compounded return over the series is preserved; the
   intra-lock daily noise is removed (NaN), so it never enters an estimate.

Detection is on the back-adjusted close: on a normal day the back-adjustment is
multiplicative on both legs so the ratio equals the raw ±band move. (A bar that is
BOTH a corporate-action ex-date and a limit day is a rare edge left to M2.)
"""
from __future__ import annotations

import math

import pandas as pd

DEFAULT_BAND = 0.10   # BIST standard daily price limit
DEFAULT_TOL = 0.003   # tick-rounding slack around the band


def flag_limit_lock(
    prices: pd.DataFrame, *, band: float = DEFAULT_BAND, tol: float = DEFAULT_TOL
) -> pd.DataFrame:
    """Return ``prices`` with ``is_limit_lock`` set where the close is pinned at the
    ±band daily limit (``|close_t/close_{t-1} - 1|`` within ``tol`` of ``band``).

    Sorted by ``bar_date`` on the way through. The first bar has no prior close, so
    it is never a lock. ``band`` is per-name (widened bands pass a larger value)."""
    p = prices.sort_values("bar_date").reset_index(drop=True).copy()
    ratio = p["close"].pct_change().abs()
    p["is_limit_lock"] = (ratio - band).abs() <= tol
    p.loc[ratio.isna(), "is_limit_lock"] = False
    return p


def _is_null(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA


def censor_lock_windows(
    df: pd.DataFrame,
    ret_cols,
    *,
    lock_col: str = "is_limit_lock",
    adj_col: str = "limit_lock_adj",
) -> pd.DataFrame:
    """Collapse each maximal run of ``lock_col`` days into one cumulative return.

    For a run of lock days at positions ``[i, j]`` with resolving day ``j+1``:
      * the censored daily returns at ``i..j`` become NaN (not a true return);
      * the resolving day ``j+1`` carries the cumulative return across the window
        ``prod(1 + r) over [i .. j+1] - 1`` (the real economic move, finally
        revealed) — computed per column in ``ret_cols``;
      * every touched row (``i .. j+1``) is flagged ``adj_col = True``.
    A run that reaches the end with no resolving day stays NaN + flagged (the true
    return is not yet knowable — never fabricated). ``df`` must be date-sorted.
    Returns ``df`` with ``ret_cols`` rewritten and ``adj_col`` added.
    """
    n = len(df)
    lock = df[lock_col].fillna(False).tolist() if lock_col in df.columns else [False] * n
    adj = [False] * n
    cols = {c: df[c].tolist() for c in ret_cols}

    i = 0
    while i < n:
        if not lock[i]:
            i += 1
            continue
        j = i
        while j < n and lock[j]:
            j += 1
        resolve = j if j < n else None  # first non-lock day after the run
        last = resolve if resolve is not None else j - 1
        for c in ret_cols:
            vals = cols[c]
            if resolve is not None:
                window = vals[i : resolve + 1]
                if any(_is_null(v) for v in window):
                    cum = float("nan")  # an unknown leg -> refuse to fabricate
                else:
                    prod = 1.0
                    for v in window:
                        prod *= 1.0 + float(v)
                    cum = prod - 1.0
                for k in range(i, resolve):
                    vals[k] = float("nan")
                vals[resolve] = cum
            else:
                for k in range(i, j):
                    vals[k] = float("nan")
        for k in range(i, last + 1):
            adj[k] = True
        i = j

    out = df.copy()
    for c in ret_cols:
        out[c] = cols[c]
    out[adj_col] = adj
    return out

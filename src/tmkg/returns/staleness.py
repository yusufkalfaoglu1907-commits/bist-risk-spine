"""Staleness flags (CLAUDE.md §5, design §3, line 72).

Single-stock halts and thin trading produce **stale prices** — the last trade is
carried forward. Stale prices fake low volatility and low correlation, so the M2
estimators need them flagged (to screen them out, or to apply a lagged-beta
Dimson / Scholes–Williams correction). This is pure detection only; the correction
itself is M2.

A bar is stale when **no shares changed hands** that day (``quantity`` is 0 or
absent): the printed close is then a carry-forward, not a fresh trade. We key on
``quantity`` rather than ``volume_try`` because some legitimate series have zero
TRY turnover by construction (FX has no exchange turnover) — so this helper is for
equity ``prices`` only, not the ``factors`` table.
"""
from __future__ import annotations

import pandas as pd


def flag_staleness(prices: pd.DataFrame, *, qty_col: str = "quantity") -> pd.DataFrame:
    """Return ``prices`` with ``is_stale`` set on bars with no traded quantity.

    Stale = ``quantity`` is null or 0 (no trade → carried-forward close). If the
    quantity column is absent the flag is left all-False (cannot assert staleness
    without trade data — never guessed)."""
    p = prices.copy()
    if qty_col not in p.columns:
        p["is_stale"] = False
        return p
    q = pd.to_numeric(p[qty_col], errors="coerce")
    p["is_stale"] = q.isna() | (q == 0)
    return p

"""Factor-return construction (BUILD_PLAN.md M2).

M1 lands factor *levels* into L2 ``factors.value`` and leaves ``ret`` NULL ("an M2
concern" — ``ingest.pipeline.ingest_factor_series``). This module turns those levels
into the factor *return* series the beta regression and the neutralization ladder
consume. It is **pure** (DataFrame in, DataFrame out, no network, no L2, no PIT):
ingestion lands the levels, this constructs the returns, and the signal layer reads
the result back through ``tmkg.pit.PITAccess`` only.

Two correctness rules baked in here:

1. **The factor set is heterogeneous — one return rule does not fit all.** A price /
   index / FX / commodity level (XU100, USDTRY, Brent, gold, EEM) has a multiplicative
   return; a *rate* level (TRY 2y/10y yield, Turkey CDS, VIX) has an additive change.
   Computing a percentage return on a yield is a basis error — a move from 40%→42% is
   +200bps, not +5%. So ``method`` is per-factor: ``simple`` / ``log`` (multiplicative)
   vs ``diff`` (a level change in the factor's own units).

2. **No fabrication on gaps (§4).** Returns are computed between *consecutive available
   observations* only — the series is sorted and differenced, never reindexed onto a
   calendar and forward-filled. The first observation of each factor has no prior, so
   its ``ret`` is NaN (never 0, never invented). A single-observation factor yields an
   all-NaN ``ret`` rather than a guessed number.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

# Factors whose "return" is a multiplicative change vs an additive level change.
SIMPLE = "simple"  # value_t / value_{t-1} - 1     (prices, indices, FX, commodities)
LOG = "log"        # ln(value_t) - ln(value_{t-1}) (same, log scale)
DIFF = "diff"      # value_t - value_{t-1}          (rates: yields, CDS, VIX)
LEVEL = "level"    # ret_t = value_t                (a series already a per-period FLOW /
#                    innovation — e.g. weekly non-resident net equity flow; differencing
#                    or pct-changing a flow is a basis error, the flow IS the factor return)
_METHODS = frozenset({SIMPLE, LOG, DIFF, LEVEL})


def _factor_ret(values: pd.Series, method: str) -> pd.Series:
    if method == SIMPLE:
        return values.pct_change()
    if method == LOG:
        # ln(v_t) - ln(v_{t-1}); a non-positive level has no log return (NaN), not a guess.
        return np.log(values.where(values > 0)).diff()
    if method == DIFF:
        return values.diff()
    if method == LEVEL:
        # the observation is already a flow/innovation: use it verbatim as the return.
        return values.astype(float)
    raise ValueError(f"unknown factor-return method {method!r}; expected one of {sorted(_METHODS)}")


def compute_factor_returns(
    values: pd.DataFrame,
    *,
    method: str | Mapping[str, str] = SIMPLE,
) -> pd.DataFrame:
    """Add a ``ret`` column to a factor-*level* frame, per factor.

    Parameters
    ----------
    values : columns ``[factor, bar_date, value]`` (+ any extras such as
        ``knowledge_date`` / ``source``, which are carried through unchanged so the
        result is directly landable to L2 ``factors``). May hold one or many factors.
    method : either a single method applied to every factor, or a
        ``{factor -> method}`` map (a factor absent from the map falls back to
        ``simple``). ``simple`` / ``log`` for multiplicative levels (prices, FX,
        indices, commodities); ``diff`` for additive rate levels (yields, CDS, VIX).

    Returns
    -------
    The input frame sorted by ``(factor, bar_date)`` with ``ret`` populated. Each
    factor's first (earliest) row has ``ret = NaN`` — it has no prior level. Rows are
    not dropped: the level series is retained alongside its return.
    """
    required = {"factor", "bar_date", "value"}
    missing = required - set(values.columns)
    if missing:
        raise ValueError(f"compute_factor_returns: values missing columns {sorted(missing)}")
    if values.empty:
        return values.assign(ret=pd.Series(dtype=float))

    out = values.copy()
    out["bar_date"] = pd.to_datetime(out["bar_date"]).dt.date
    out["value"] = pd.to_numeric(out["value"], errors="coerce")

    def _method_for(factor: str) -> str:
        if isinstance(method, Mapping):
            return method.get(factor, SIMPLE)
        return method

    pieces = []
    for factor, grp in out.groupby("factor", sort=False):
        g = grp.sort_values("bar_date").drop_duplicates("bar_date")
        g["ret"] = _factor_ret(g["value"], _method_for(factor))
        pieces.append(g)

    return (
        pd.concat(pieces)
        .sort_values(["factor", "bar_date"])
        .reset_index(drop=True)
    )

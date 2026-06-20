"""tmkg.returns — clean USD-primary total-return construction (M1).

Corporate-action-adjusted total returns (bedelsiz/bonus, rights, splits,
dividends), USD-primary with CPI-real-TRY cross-check, limit-lock censoring,
staleness flags, accounting_regime tagging. All pure (DataFrame in / out, no IO):
ingestion lands the inputs, these build the clean series, signal code reads the
result back through tmkg.pit.PITAccess only.
"""
from tmkg.returns.accounting_regime import (
    REGIMES,
    guarded_growth,
    regime_for_period,
    same_regime,
)
from tmkg.returns.limit_lock import censor_lock_windows, flag_limit_lock
from tmkg.returns.staleness import flag_staleness
from tmkg.returns.total_return import compute_total_returns, dividend_yields_from_raw

__all__ = [
    "compute_total_returns",
    "dividend_yields_from_raw",
    "flag_limit_lock",
    "censor_lock_windows",
    "flag_staleness",
    "regime_for_period",
    "same_regime",
    "guarded_growth",
    "REGIMES",
]

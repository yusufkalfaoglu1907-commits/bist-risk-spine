"""tmkg.returns — clean USD-primary total-return construction (M1).

Corporate-action-adjusted total returns (bedelsiz/bonus, rights, splits,
dividends), USD-primary with CPI-real-TRY cross-check, limit-lock censoring,
staleness flags, accounting_regime tagging.

Not yet implemented — see BUILD_PLAN.md M1 and the back-adjustment golden masters
in tests/golden/matriks/ohlcv_*.json + accounting_regime_*.json.
"""

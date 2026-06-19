"""tmkg.signals — L3 derivation / signal layer (M3+).

correlation.py  residual covariance -> glasso/PMFG -> filtered RESIDUAL_CORR (M3, the kill-switch gate)
promotion.py    naive-baseline ladder + Deflated Sharpe + PBO gate (M4, the judge)
registry.py     signal registry (M4)
backtest.py     PIT backtester, three books: research | venue-feasible | stress (M4)

Build the judge (M4) before the first real signal (M5). Not yet implemented —
see BUILD_PLAN.md M3/M4.
"""

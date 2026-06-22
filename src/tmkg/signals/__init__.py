"""tmkg.signals — L3 derivation / signal layer (M3+).

correlation.py  residual covariance -> shrinkage/FDR -> MST/PMFG -> filtered RESIDUAL_CORR
                (M3, the residual-survival [STOP] gate) — IN PROGRESS
promotion.py    naive-baseline ladder + Deflated Sharpe + PBO gate (M4, the judge)
registry.py     signal registry (M4)
backtest.py     PIT backtester, three books: research | venue-feasible | stress (M4)

This layer reads L1/L2 only (never the network, §4 rule 1) — enforced by the AST scan in
tests/invariants/test_no_network_in_signal_layer.py. Build the judge (M4) before the first
real signal (M5). See BUILD_PLAN.md M3/M4.
"""

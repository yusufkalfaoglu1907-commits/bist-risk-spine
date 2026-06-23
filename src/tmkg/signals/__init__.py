"""tmkg.signals — L3 derivation / signal layer (M3+).

correlation.py  residual covariance -> shrinkage/FDR -> MST/PMFG -> filtered RESIDUAL_CORR
stability.py    rolling-window residual-network stability (M3 residual-survival [STOP] gate)
gate.py         M3 gate runner (reads residuals via PITAccess, emits GO/NO-GO) — DONE ✅ GO
stats.py        Deflated Sharpe Ratio + PBO (CSCV) — the judge's anti-overfit core (M4)
backtest.py     PIT backtester: purge/embargo splits, cost+borrow, three books
                research | venue_feasible | stress; capacity curve (M4)
promotion.py    naïve-baseline ladder (persistence / differential-exposure / own-factor event)
                + the promotion gate (beat-the-ladder · DSR · PBO · venue-feasible) (M4)
registry.py     signal registry — every candidate logged bitemporal into L2 (M4)

This layer reads L1/L2 only (never the network, §4 rule 1) — enforced by the AST scan in
tests/invariants/test_no_network_in_signal_layer.py. The judge (M4) is built and self-tested
(known-null rejected, known-good promoted) before the first real signal (M5). See BUILD_PLAN.
"""

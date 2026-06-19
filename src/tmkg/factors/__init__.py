"""tmkg.factors — factor model + explicit neutralization ladder (M2).

Core factor set (market, FX, rates/CDS, energy, sector, foreign-flow,
holding-group, ...), rolling regime-aware betas with Ledoit-Wolf shrinkage,
fit per universe_class. Neutralization order is explicit and falsifiable:
market -> FX -> rates/CDS -> energy -> sector -> foreign-flow -> holding -> residual.

Not yet implemented — see BUILD_PLAN.md M2.
"""

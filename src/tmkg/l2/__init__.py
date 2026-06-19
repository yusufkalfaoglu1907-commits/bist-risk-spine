"""tmkg.l2 — DuckDB + Parquet quant store (M0).

Prices, total returns, volume, factor series, betas, residuals, CARs and
filtered correlation snapshots live here — never as graph properties (design §5).
All rows are bitemporal; signal code reads them through tmkg.pit.PITAccess.
"""
from tmkg.l2.store import L2Store

__all__ = ["L2Store"]

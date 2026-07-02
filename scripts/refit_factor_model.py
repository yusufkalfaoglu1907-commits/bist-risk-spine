"""M2/M3 factor refit — rebuild betas + neutralized residuals over the landed universe.

This is the *serving* refit (no gate): after prices/total_returns and the factor series have
been topped up (``daily_update.py`` + ``ingest_factors.py``), this recomputes the full rolling
beta/residual history as of ``as_of`` so downstream reads see the latest close. It mirrors the
refit step of ``run_m3_gate.py`` but **does not** run the go/no-go gate (§8) — it just lands fit.

Safety (the reason this is its own script): the refit is a DELETE-then-rebuild (``write_parquet``
is ON CONFLICT DO NOTHING, so stale rows must be cleared first). An unattended daily job must never
wipe betas/residuals and then abort on a transient missing factor. So we **validate the factor panel
first** (``require_all`` present as of ``as_of``); if any configured factor is missing we abort
*without clearing*, leaving yesterday's fit intact (stale but honest) and exit non-zero.

    PYTHONPATH=src python scripts/refit_factor_model.py [AS_OF]

Reads only L2 (no network). run_m2_factor_model writes its own §4 audit report.
"""
from __future__ import annotations

import sys
from datetime import date

import tmkg.config  # noqa: F401  -- load_dotenv() before adapters read env
from tmkg.factors import registry
from tmkg.ingest.pipeline import build_factor_return_panel, factor_coverage, run_m2_factor_model
from tmkg.l2.store import L2Store

# Fit knobs — match run_m3_gate so the served residuals are the same series the gate evaluated.
FIT_WINDOW = 60
FIT_MIN_OBS = 40


def _symbols_with_total_returns(store: L2Store, as_of: date) -> list[str]:
    """Names carrying a USD total-return series knowable by ``as_of`` (the fit universe)."""
    con = store.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM total_returns WHERE knowledge_date <= ? ORDER BY symbol",
            [as_of],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _clear_fit(store: L2Store) -> None:
    """Drop betas/residuals so the refit writes fresh values (write is ON CONFLICT DO NOTHING)."""
    con = store.connect()
    try:
        for tbl in ("betas", "residuals"):
            con.execute(f"DELETE FROM {tbl}")
    finally:
        con.close()


def main(as_of: date) -> int:
    store = L2Store()
    store.bootstrap_schema()

    symbols = _symbols_with_total_returns(store, as_of)
    print(f"fit universe: {len(symbols)} names with total_returns as of {as_of}")
    if not symbols:
        print("NO total_returns landed — run scripts/ingest_universe.py / daily_update.py first.")
        return 1

    # VALIDATE BEFORE CLEARING: build the panel and require every configured factor present.
    specs = registry.specs()
    try:
        panel = build_factor_return_panel(store, as_of=as_of, specs=specs, require_all=True)
    except Exception as e:  # missing/unreachable factor → abort without touching the existing fit
        print(f"ABORT (fit left intact): factor panel incomplete as of {as_of}: {str(e)[:200]}")
        return 2
    present, missing = factor_coverage(panel, specs)
    if missing:
        print(f"ABORT (fit left intact): missing factors {missing} as of {as_of}")
        return 2
    print(f"factor panel OK: {len(present)} factors present, none missing")

    print("clearing stale betas/residuals for a clean refit ...")
    _clear_fit(store)

    print(f"M2 refit (window={FIT_WINDOW}, min_obs={FIT_MIN_OBS}, require_all_factors=True) ...")
    m2 = run_m2_factor_model(
        store, symbols=symbols, as_of=as_of, specs=specs,
        window=FIT_WINDOW, min_obs=FIT_MIN_OBS, require_all_factors=True,
    )
    n_betas = sum(1 for r in m2["betas"] if r.get("n_betas", 0))
    n_res = sum(1 for r in m2["residuals"] if r.get("n_residuals", r.get("n_rows", 0)))
    print(f"  ladder: {m2['ladder']}")
    print(f"  betas built for {n_betas}/{len(symbols)} names; residuals for {n_res}/{len(symbols)}")
    print(f"  missing_factors={m2['missing_factors']}")
    return 0


if __name__ == "__main__":
    as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    raise SystemExit(main(as_of))

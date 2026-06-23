"""M3 residual-survival [STOP] gate — clean refit + gate over the landed universe.

Run AFTER ``scripts/ingest_universe.py`` has landed prices + total_returns for the wide
universe. Three steps, all L2/PIT-honest (no network):

  1. CLEAN REFIT — delete the stale M2 (BIST-30) betas/residuals, then
     ``run_m2_factor_model`` over every name with total_returns as of ``as_of`` (write=60d
     rolling betas + neutralized residuals; ``require_all_factors`` so no factor is silently
     dropped — the "was it just the flow factor?" question needs FFLOW in the strip).
  2. SECTOR MAP — ticker→sector from the v1 identity graph (the within-sector candidate
     family for the FDR + stability test).
  3. GATE — ``m3_residual_survival_gate`` over the refit residuals → ``data/cache/m3_gate_report.json``.

This is a project-level go/no-go (§8): the script PRINTS the verdict; it does NOT auto-advance.

    PYTHONPATH=src python scripts/run_m3_gate.py [AS_OF] [WINDOW] [MIN_OBS]
"""
from __future__ import annotations

import sys
from datetime import date

import tmkg.config  # noqa: F401  -- load_dotenv() before adapters read env
from tmkg.factors import registry
from tmkg.graph.connection import connect as graph_connect
from tmkg.ingest.pipeline import run_m2_factor_model
from tmkg.ingest.universe import graph_sector_resolver
from tmkg.l2.store import L2Store
from tmkg.signals.gate import m3_residual_survival_gate, write_gate_report

# M2 fit knobs (match the BIST-30 dry-run so the only change is universe WIDTH).
FIT_WINDOW = 60
FIT_MIN_OBS = 40


def _symbols_with_total_returns(store: L2Store, as_of: date) -> list[str]:
    """Names carrying a USD total-return series knowable by ``as_of`` (the fit universe)."""
    con = store.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM total_returns "
            "WHERE knowledge_date <= ? ORDER BY symbol",
            [as_of],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _clear_stale_fit(store: L2Store) -> None:
    """Drop stale betas/residuals — write_parquet is ON CONFLICT DO NOTHING, so a refit
    over existing PKs would otherwise keep the OLD (BIST-30-only) values."""
    con = store.connect()
    try:
        for tbl in ("betas", "residuals"):
            con.execute(f"DELETE FROM {tbl}")
    finally:
        con.close()


def main(as_of: date, gate_window: int, gate_min_obs: int) -> int:
    store = L2Store()
    store.bootstrap_schema()

    symbols = _symbols_with_total_returns(store, as_of)
    print(f"fit universe: {len(symbols)} names with total_returns as of {as_of}")
    if not symbols:
        print("NO total_returns landed — run scripts/ingest_universe.py first.")
        return 1

    print("clearing stale betas/residuals for a clean refit ...")
    _clear_stale_fit(store)

    print(f"M2 refit (window={FIT_WINDOW}, min_obs={FIT_MIN_OBS}, require_all_factors=True) ...")
    m2 = run_m2_factor_model(
        store, symbols=symbols, as_of=as_of, specs=registry.specs(),
        window=FIT_WINDOW, min_obs=FIT_MIN_OBS, require_all_factors=True,
    )
    n_res = sum(1 for r in m2["residuals"] if r.get("n_residuals", r.get("n_rows", 0)))
    print(f"  ladder: {m2['ladder']}")
    print(f"  residuals built for {n_res}/{len(symbols)} names; "
          f"missing_factors={m2['missing_factors']}")

    con = graph_connect()
    resolve = graph_sector_resolver(con)
    sectors = {s: resolve(s) for s in symbols}
    sectors = {k: v for k, v in sectors.items() if v is not None}
    print(f"sector map: {len(sectors)} names placed into {len(set(sectors.values()))} sectors")

    print(f"M3 gate (window={gate_window}, min_obs={gate_min_obs}) ...")
    report = m3_residual_survival_gate(
        store, as_of=as_of, sectors=sectors,
        window=gate_window, min_obs=gate_min_obs, panel_min_obs=gate_min_obs,
    )
    out = write_gate_report(report, "data/cache/m3_gate_report.json")

    d = report["decision"]
    s = report["summary"]
    inp = report["inputs"]
    print("\n" + "=" * 64)
    print(f"  M3 RESIDUAL-SURVIVAL GATE  [STOP]  ->  {d.get('decision')}")
    print("=" * 64)
    print(f"  panel: {inp['n_symbols_panel']} names ({inp['n_symbols_with_sector']} w/ sector), "
          f"{inp['n_dates_panel']} dates")
    print(f"  summary: {s}")
    print(f"  checks:  {d.get('checks')}")
    print(f"  failed:  {d.get('failed_checks')}")
    print(f"  report -> {out}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    gate_window = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    gate_min_obs = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    raise SystemExit(main(as_of, gate_window, gate_min_obs))

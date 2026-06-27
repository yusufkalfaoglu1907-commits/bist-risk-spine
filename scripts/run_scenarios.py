"""M8.1 — re-price the current exposure tensor against the stylized scenario library (+ optionally a
real historical window) and write data/cache/m8_scenario_report.json.

A RISK tool, not a signal: it prints worst-exposed names + portfolio-neutral stress per scenario and
does NOT touch signal_registry. PIT-honest (reads betas/factors through PITAccess at AS_OF; no network).

    PYTHONPATH=src python scripts/run_scenarios.py [AS_OF] [WINDOW_START WINDOW_END]
    # e.g. PYTHONPATH=src python scripts/run_scenarios.py 2026-06-15 2025-03-18 2025-03-25
    #   adds an empirical, unit-correct re-pricing of the real 2025-03 İmamoğlu-shock window.
"""
from __future__ import annotations

import sys
from datetime import date

import tmkg.config  # noqa: F401  -- load_dotenv() before anything reads env
from tmkg.l2.store import L2Store
from tmkg.risk.run_scenarios import run_scenario_analysis


def _d(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str]) -> int:
    as_of = _d(argv[1]) if len(argv) > 1 else date.today()
    window = None
    name = "historical_window"
    if len(argv) >= 4:
        window = (_d(argv[2]), _d(argv[3]))
        name = argv[4] if len(argv) > 4 else "historical_window"

    store = L2Store()
    store.bootstrap_schema()
    rep = run_scenario_analysis(
        store, as_of=as_of, empirical_window=window, empirical_name=name,
        report_dir="data/cache")

    if rep.get("reason"):
        print(f"NO re-pricing: {rep['reason']}")
        return 1

    et = rep["exposure_tensor"]
    print(f"\n=== M8 SCENARIO RE-PRICING @ {as_of} (risk spine, not a signal) ===")
    print(f"  exposure tensor : {et['n_names']} names × channels {et['channels']}")
    if rep.get("empirical") and rep["empirical"].get("shock"):
        print(f"  empirical shock : {rep['empirical']['window']}  {rep['empirical']['shock']}")
    for s in rep["scenarios"]:
        worst = ", ".join(f"{k} {v:+.3f}" for k, v in list(s["worst_exposed"].items())[:3])
        pnl = s.get("portfolio_pnl")
        print(f"\n  [{s['tier']}] {s['scenario']}")
        print(f"      shocks    : {s['shocks']}")
        print(f"      worst-hit : {worst}")
        if s.get("unmodelled_channels"):
            print(f"      unmodelled: {s['unmodelled_channels']}  (no exposure column — surfaced)")
        print(f"      min cover : {s['min_coverage']}"
              + (f"   portfolio P&L: {pnl:+.4f}" if pnl is not None else ""))
    for sk in rep.get("skipped_scenarios", []):
        print(f"\n  [skipped] {sk['name']}: {sk['reason']}")
    print("\nReport: data/cache/m8_scenario_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

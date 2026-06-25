"""M6 — geopolitical-event differential-exposure signal through the M4 judge (BUILD_PLAN.md M6).

Reads the landed GKG ``events`` + ``event_targets`` (from scripts/ingest_gdelt.py) and the M2/M3
``betas`` + ``total_returns`` (PIT-honest, no network), builds the cross-sectional differential-
exposure spread, and runs it through the purged walk-forward / three-book / DSR-PBO promotion gate
plus the §240 channel-stress second output. Lands the verdict into L2 ``signal_registry`` and
writes data/cache/m6_event_diffexp_report.json.

This is the M6 exit gate — a project-level result (§8): the script PRINTS the verdict (incl. the
§238 control-survival diagnostic) and does NOT auto-advance. ``panel_start`` floors the evaluation
to the event-active window (an event signal is zero off event days).

    PYTHONPATH=src python scripts/run_m6_gate.py [AS_OF] [PANEL_START]
    # pilot default: AS_OF=2025-03-31  PANEL_START=2024-10-01
"""
from __future__ import annotations

import sys
from datetime import date

import tmkg.config  # noqa: F401  -- load_dotenv() before adapters read env
from tmkg.events.run_event_signal import run_m6_event_signal
from tmkg.l2.store import L2Store


def _count(store: L2Store, table: str, as_of: date) -> int:
    con = store.connect()
    try:
        return con.execute(
            f"SELECT count(*) FROM {table} WHERE knowledge_date <= ?", [as_of]).fetchone()[0]
    finally:
        con.close()


def main(as_of: date, panel_start: date) -> int:
    store = L2Store()
    store.bootstrap_schema()

    n_ev = _count(store, "events", as_of)
    n_tg = _count(store, "event_targets", as_of)
    if n_ev == 0:
        print("NO events landed at as_of — run scripts/ingest_gdelt.py first.")
        return 1
    print(f"M6 event signal: {n_ev} events / {n_tg} targets visible at {as_of}; "
          f"panel_start={panel_start}")

    rep = run_m6_event_signal(
        store, as_of=as_of, panel_start=panel_start, write_l2=True, report_dir="data/cache")

    g = rep["exit_gate"]
    inp = rep.get("inputs", {})
    print("\n=== M6 EXIT GATE ===")
    print(f"  reason            : {rep.get('reason', '-')}")
    print(f"  OOS test dates    : {inp.get('n_oos_test_dates')}  "
          f"(panel {inp.get('n_dates')}d × {inp.get('n_names')} names)")
    print(f"  channels w/ expo  : {inp.get('channels_with_exposure')}")
    print(f"  event-channels    : {inp.get('n_events_channels')}  trials={inp.get('n_trials_grid')}")
    print(f"  median control    : {g.get('median_control_fraction')}   (§238 thin-cross-section)")
    print(f"  venue net Sharpe  : {rep.get('venue_net_sharpe')}")
    print(f"  stress net Sharpe : {rep.get('stress_net_sharpe')}")
    print(f"  beats baselines   : {g.get('beats_baselines')}")
    print(f"  DSR passes        : {g.get('dsr_passes')}")
    print(f"  clears capacity   : {g.get('clears_capacity_floor')}")
    print(f"  >>> PROMOTED      : {g.get('promoted')}")
    cs = rep.get("channel_stress", {}).get("scenarios", [])
    if cs:
        print(f"  channel-stress    : {len(cs)} scenarios; e.g. "
              f"{cs[0].get('event_type')} worst={list(cs[0].get('worst_exposed', {}))[:3]}")
    print("\n(M6 exit gate is a project-level result — surfaced, not auto-advanced.)")
    return 0


if __name__ == "__main__":
    as_of = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2025, 3, 31)
    panel_start = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date(2024, 10, 1)
    raise SystemExit(main(as_of, panel_start))

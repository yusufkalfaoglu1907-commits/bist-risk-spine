"""M3 residual-survival [STOP] gate runner (BUILD_PLAN.md M3).

The one L2-touching piece of M3: read the landed neutralized ``residuals`` back through PIT,
build the residual panel, run the rolling-window stability measurement, and emit the go/no-go
report. Everything statistical lives in the pure ``correlation`` / ``stability`` modules (built
and pinned before the data); this orchestrates them over the real store and records a verdict.

Held to §4: the only data access is through ``PITAccess`` (no raw SELECT, no network — this is
signal-layer code, enforced by ``tests/invariants/test_no_network_in_signal_layer.py``). The
gate is a **project-level go/no-go (§8)** — the caller surfaces the decision to the user and
does not auto-advance to M4/M5.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from tmkg.signals.correlation import residual_panel
from tmkg.signals.stability import decide_gate, rolling_stability, stability_summary


def _jsonable(obj):
    """Recursively coerce dates/NumPy scalars to JSON-native types."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (date, pd.Timestamp)):
        return str(obj)
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):  # numpy scalar
        try:
            return obj.item()
        except (ValueError, AttributeError):
            return obj
    return obj


def m3_residual_survival_gate(
    store,
    *,
    as_of: date,
    sectors: dict[str, str],
    universe: list[str] | None = None,
    window: int = 120,
    step: int | None = None,
    alpha: float = 0.05,
    min_obs: int = 60,
    panel_min_obs: int = 60,
    min_abs_corr: float = 0.0,
) -> dict:
    """Run the residual-survival gate over the landed L2 ``residuals`` and return the verdict.

    Reads residuals through ``PITAccess`` at ``as_of`` (optionally restricted to ``universe``),
    pivots to a panel (names with < ``panel_min_obs`` residuals dropped), and runs the
    rolling-window stability measurement with the within-sector restriction defined by
    ``sectors`` (ticker → sector, resolved by the caller from the L1 identity graph). Emits a
    report dict: inputs, coverage, the per-window-pair table, the summary, and the GO/NO-GO
    decision. Empty/insufficient residuals yield an honest NO-GO (cannot judge), never a fake GO.
    """
    from tmkg.pit.access import PITAccess  # local import keeps the module network/L2-free at import

    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        res = pit.series("residuals")
    finally:
        con.close()

    syms = universe
    panel = (residual_panel(res, min_obs=panel_min_obs, symbols=syms)
             if not res.empty else pd.DataFrame())

    rolling = (rolling_stability(panel, sectors=sectors, window=window, step=step,
                                 alpha=alpha, min_obs=min_obs, min_abs_corr=min_abs_corr)
               if not panel.empty else
               pd.DataFrame(columns=["win_a_end", "win_b_end", "n_edges_a", "n_edges_b",
                                     "n_shared", "jaccard", "random_jaccard", "lift",
                                     "weight_rank_rho"]))
    summary = stability_summary(rolling)
    decision = decide_gate(summary)

    # how many of the panel's names actually have a sector (entered the within-sector family)
    placed = [s for s in panel.columns if sectors.get(s) is not None] if not panel.empty else []
    report = {
        "milestone": "M3",
        "gate": "residual_survival",
        "stop_gate": True,
        "as_of": str(as_of),
        "inputs": {
            "n_residual_rows": int(len(res)),
            "n_symbols_panel": int(panel.shape[1]) if not panel.empty else 0,
            "n_symbols_with_sector": len(placed),
            "n_dates_panel": int(panel.shape[0]) if not panel.empty else 0,
            "window": window, "step": step if step is not None else window,
            "alpha": alpha, "min_obs": min_obs, "panel_min_obs": panel_min_obs,
            "min_abs_corr": min_abs_corr,
        },
        "rolling": rolling.to_dict("records"),
        "summary": summary,
        "decision": decision,
    }
    return _jsonable(report)


def write_gate_report(report: dict, path: str | Path) -> Path:
    """Persist the gate report as JSON (the audit artifact; §4 every run writes a report)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
    return path

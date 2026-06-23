"""M5 residual stat-arb runner — the first real signal through the M4 judge (BUILD_PLAN.md M5).

The one L2/L1-touching piece of M5. Everything statistical lives in the pure modules built and
pinned before the data (statarb.py / correlation.py / promotion.py / backtest.py / stats.py);
this orchestrates them over the real store and records an honest verdict:

  1. read the neutralized ``residuals`` and the raw ``total_returns`` (+ limit-lock / short flags)
     through ``PITAccess`` at ``as_of`` (no raw SELECT, no network — §4);
  2. restrict to a liquid, continuously-listed sub-universe (the venue-feasible book needs names
     you could actually trade — the tradability illusion is a named risk, design §10 / W?);
  3. build a **grid** of stat-arb variants and run each through the venue-feasible book to get its
     per-period net P&L — the honest ``n_trials`` family for the DSR/PBO haircut (finding D1);
  4. **purged walk-forward selection** (López de Prado): in each fold pick the variant that is
     best on the *train* block and apply it on the *test* block, assembling a genuinely
     out-of-sample weight panel — wiring ``purged_walk_forward_splits`` into a real train/test
     split (the M5 entry condition carried from the M4 adversarial review, D2/W1);
  5. judge that OOS candidate through the full promotion gate (beat-the-ladder · DSR · PBO ·
     venue-feasible) across all three books + a capacity curve;
  6. land the filtered residual-corr snapshot into L2 ``residual_corr`` (never the dense matrix,
     §design) and the verdict into ``signal_registry``; write the §4 JSON audit report.

The M5 exit gate (BUILD_PLAN): survives the **venue-feasible** book · DSR passes · clears a
**stated capacity floor** · registry entry complete. A signal that lives only in frictionless
research is **not real** — the runner reports it and the gate fails it.

NOTE — ``short_eligible`` is currently empty in L2 (M2 left the venue short-feasibility map
blocked on the Matriks foreign-custodian list). With it empty the venue-feasible book cannot
police per-name short bans, so the **stress book** (a blanket short-ban) is the binding
short-constraint test until that map lands. Surfaced in the report, not silently ignored.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tmkg.signals.backtest import (
    STRESS,
    VENUE_FEASIBLE,
    CostModel,
    capacity_curve,
    purged_walk_forward_splits,
    run_all_books,
    run_book,
)
from tmkg.signals.correlation import residual_panel
from tmkg.signals.promotion import evaluate_candidate
from tmkg.signals.stats import sharpe_ratio
from tmkg.signals.statarb import StatArbParams, build_edge_schedule, default_grid, statarb_weights


# --- L2 reads (all through PITAccess) ---------------------------------------


def _load_panels(store, as_of: date, *, panel_min_obs: int):
    """Read residuals + total_returns (+ limit-lock) through PITAccess; return aligned panels.

    Returns ``(resid_panel, ret_panel, limit_lock_panel)`` on a common (date × symbol) frame.
    ``ret_panel`` is USD-primary total return reindexed onto the residual panel's axes (the P&L
    is earned on real returns; the *signal* is built on residuals). ``limit_lock_panel`` is the
    ``limit_lock_adj`` flag; a name/date with no flag is treated as tradable (NaN → not locked)."""
    from tmkg.pit.access import PITAccess

    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        res = pit.series("residuals")
        rets = pit.series("total_returns")
        se = pit.series("short_eligible")
    finally:
        con.close()

    panel = residual_panel(res, min_obs=panel_min_obs) if not res.empty else pd.DataFrame()
    if panel.empty:
        return panel, pd.DataFrame(), pd.DataFrame(), se

    rets = rets.copy()
    rets["bar_date"] = pd.to_datetime(rets["bar_date"]).dt.date
    ret_panel = (rets.pivot_table(index="bar_date", columns="symbol", values="ret_usd",
                                  aggfunc="mean").sort_index())
    ret_panel.columns.name = None
    ll_panel = (rets.pivot_table(index="bar_date", columns="symbol", values="limit_lock_adj",
                                 aggfunc="max").sort_index())
    ll_panel.columns.name = None
    return panel, ret_panel, ll_panel, se


def liquid_universe(panel: pd.DataFrame, *, top_by_coverage: int | None) -> list[str]:
    """A continuously-listed / liquid sub-universe: the ``top_by_coverage`` names with the most
    non-NaN residuals (a coverage proxy for continuous listing + tradability — the venue-feasible
    book must be run on names you could actually hold, design §10). ``None`` keeps every name."""
    cov = panel.notna().sum(axis=0).sort_values(ascending=False)
    names = list(cov.index) if top_by_coverage is None else list(cov.index[:top_by_coverage])
    return sorted(names)


# --- purged walk-forward variant selection ----------------------------------


def walk_forward_oos_weights(
    variant_weights: dict[str, pd.DataFrame],
    variant_pnls: pd.DataFrame,
    *,
    n_splits: int = 5,
    purge: int = 5,
    embargo: int = 5,
):
    """Assemble an out-of-sample weight panel by purged walk-forward variant selection.

    For each fold, the variant with the best *train*-block net Sharpe is applied on the *test*
    block — so every test date's weights came from a choice made on strictly prior data. Returns
    ``(oos_weights, test_index, picks)`` where ``oos_weights`` is zero outside the union of test
    blocks and ``picks`` records the per-fold winner (the audit trail of what was selected when).
    """
    labels = list(variant_weights)
    dates = variant_pnls.index
    n = len(dates)
    splits = purged_walk_forward_splits(n, n_splits=n_splits, purge=purge, embargo=embargo)
    template = next(iter(variant_weights.values()))
    oos = pd.DataFrame(0.0, index=template.index, columns=template.columns)
    test_pos: list[int] = []
    picks: list[dict] = []
    P = variant_pnls.to_numpy()
    for sp in splits:
        tr, te = sp.train, sp.test
        if tr.size == 0:
            continue
        train_sr = {lab: sharpe_ratio(P[tr, j]) for j, lab in enumerate(labels)}
        best = max(train_sr, key=lambda k: train_sr[k] if np.isfinite(train_sr[k]) else -np.inf)
        oos.iloc[te] = variant_weights[best].iloc[te].to_numpy()
        test_pos.extend(te.tolist())
        picks.append({"train_end": str(dates[tr[-1]]), "test_start": str(dates[te[0]]),
                      "test_end": str(dates[te[-1]]), "chosen": best,
                      "train_sharpe": float(train_sr[best])})
    test_index = template.index[sorted(set(test_pos))]
    return oos, test_index, picks


# --- the runner -------------------------------------------------------------


def run_m5_statarb(
    store,
    *,
    as_of: date,
    sectors: dict[str, str],
    grid: list[StatArbParams] | None = None,
    universe: list[str] | None = None,
    top_by_coverage: int | None = 120,
    panel_min_obs: int = 120,
    n_splits: int = 5,
    purge: int = 5,
    embargo: int = 5,
    cost_model: CostModel | None = None,
    capacity_floor: float = 0.0,
    pbo_threshold: float = 0.5,
    signal_id: str = "m5_residual_statarb",
    write_l2: bool = True,
    report_dir: str | Path | None = None,
) -> dict:
    """Run the M5 residual-stat-arb signal end-to-end and return the verdict report.

    ``sectors`` is the ticker→sector map (resolved by the caller from the L1 identity graph) used
    for the M3 within-sector edge restriction. The grid defaults to ``statarb.default_grid()``;
    its length is the honest ``n_trials`` the candidate is haircut against. Promotion keys on the
    out-of-sample (walk-forward-selected) candidate; the report carries all three books, the
    capacity curve and the per-fold picks. Lands the latest edge snapshot into L2 ``residual_corr``
    and the verdict into ``signal_registry`` when ``write_l2`` (the M5 "write filtered snapshots
    back" build step), and writes a JSON audit report (§4)."""
    grid = grid or default_grid()
    cm = cost_model or CostModel()

    panel, ret_panel, ll_panel, se = _load_panels(store, as_of, panel_min_obs=panel_min_obs)
    if panel.empty:
        return _empty_report(as_of, signal_id, reason="no residuals visible at as_of")

    names = universe if universe is not None else liquid_universe(panel, top_by_coverage=top_by_coverage)
    names = [s for s in names if s in panel.columns]
    panel = panel[names]
    fwd = ret_panel.reindex(index=panel.index, columns=names)
    limit_lock = ll_panel.reindex(index=panel.index, columns=names).astype("boolean")
    short_eligible = None  # L2 short_eligible empty (see module docstring) — stress book binds

    # ---- one edge schedule per unique edge-estimation param set (z-params filter post-hoc) ----
    sched_cache: dict[tuple, list] = {}

    def schedule_for(p: StatArbParams):
        key = (p.edge_window, p.refit_step, p.alpha, p.min_abs_corr, p.min_obs)
        if key not in sched_cache:
            sched_cache[key] = build_edge_schedule(
                panel, sectors, edge_window=p.edge_window, refit_step=p.refit_step,
                alpha=p.alpha, min_obs=p.min_obs, min_abs_corr=p.min_abs_corr)
        return sched_cache[key]

    # ---- run every variant through the venue-feasible book -> trial P&L family ----
    variant_weights: dict[str, pd.DataFrame] = {}
    variant_pnls: dict[str, pd.Series] = {}
    for p in grid:
        w = statarb_weights(panel, sectors, p, schedule=schedule_for(p))
        res = run_book(w, fwd, book=VENUE_FEASIBLE, cost_model=cm,
                       short_eligible=short_eligible, limit_lock=limit_lock)
        variant_weights[p.label()] = w
        variant_pnls[p.label()] = res.pnl
    trial_pnls = pd.DataFrame(variant_pnls)

    # ---- purged walk-forward OOS selection ----
    oos_w, test_index, picks = walk_forward_oos_weights(
        variant_weights, trial_pnls, n_splits=n_splits, purge=purge, embargo=embargo)
    if len(test_index) < 3:
        return _empty_report(as_of, signal_id, reason="insufficient OOS test history")

    # candidate / baselines / trial family all judged on the SAME out-of-sample test dates
    fwd_oos = fwd.loc[test_index]
    cand_oos = oos_w.loc[test_index]
    ll_oos = limit_lock.loc[test_index]
    trial_oos = trial_pnls.loc[test_index]

    verdict = evaluate_candidate(
        cand_oos, fwd_oos, n_trials=len(grid),
        returns_for_baselines=fwd_oos, book=VENUE_FEASIBLE, cost_model=cm,
        short_eligible=short_eligible, limit_lock=ll_oos,
        trial_pnls=trial_oos, pbo_threshold=pbo_threshold, capacity_floor=capacity_floor,
    )

    # all three books + capacity curve on the OOS candidate (the tradability-illusion check)
    books = run_all_books(cand_oos, fwd_oos, cost_model=cm,
                          short_eligible=short_eligible, limit_lock=ll_oos)
    cap = capacity_curve(cand_oos, fwd_oos, book=VENUE_FEASIBLE, cost_model=cm,
                         short_eligible=short_eligible, limit_lock=ll_oos)
    stress_res = books[STRESS.name]

    latest_snap = max(sched_cache.values(), key=lambda s: (s[-1].effective_date if s else date.min)) \
        if sched_cache else []
    latest_edges = latest_snap[-1] if latest_snap else None

    report = _build_report(
        as_of, signal_id, names, panel, test_index, grid, picks, verdict, books, cap,
        latest_edges, capacity_floor, cm)

    if write_l2 and latest_edges is not None and not latest_edges.edges.empty:
        _land_residual_corr(store, latest_edges, as_of)
        _land_registry(store, verdict, signal_id, test_index, grid, picks, report_dir)

    if report_dir is not None:
        _write_report(report, Path(report_dir) / f"{signal_id}_report.json")
    return report


# --- persistence -------------------------------------------------------------


def _land_residual_corr(store, snap, as_of: date) -> None:
    """Land the latest surviving edge snapshot into L2 ``residual_corr`` — the *filtered* snapshot
    only (FDR + sector-restricted survivors), never the dense matrix (system-design-v2.md).
    ``window_end`` = the snapshot's window end; ``knowledge_date`` = as_of (when we judged it)."""
    e = snap.edges
    df = pd.DataFrame({
        "symbol_a": e["src"].to_numpy(),
        "symbol_b": e["dst"].to_numpy(),
        "window_end": snap.window_end,
        "window": int((pd.Timestamp(snap.window_end) - pd.Timestamp(snap.window_start)).days),
        "value": e["corr"].to_numpy(),
        "p_value": e["p_value"].to_numpy(),
        "sign": np.sign(e["corr"].to_numpy()).astype(int),
        "method": "fdr_sector",
        "fdr_passed": True,
        "knowledge_date": as_of,
    })
    store.write_parquet("residual_corr", df)


def _land_registry(store, verdict, signal_id, test_index, grid, picks, report_dir) -> None:
    from tmkg.signals.registry import (
        build_registry_entry,
        write_registry_entry,
        write_registry_report,
    )
    train_end = picks[0]["train_end"] if picks else None
    entry = build_registry_entry(
        verdict, signal_id=signal_id,
        hypothesis=("peer-relative residual mean reversion: fade a name's residual in excess of "
                    "its M3 surviving-edge peers (correlation pillar, ADR-0003)"),
        feature_family="residual_statarb",
        knowledge_date=verdict_knowledge_date(test_index),
        train_start=None, train_end=pd.to_datetime(train_end).date() if train_end else None,
        test_start=pd.to_datetime(str(test_index[0])).date(),
        test_end=pd.to_datetime(str(test_index[-1])).date(),
        cost_model="CostModel(default 10bps + 100bps/yr borrow)",
        purge_embargo="purged_walk_forward n_splits=5 purge=5 embargo=5",
    )
    write_registry_entry(store, entry)
    if report_dir is not None:
        write_registry_report(entry, Path(report_dir) / f"{signal_id}_registry_report.json")


def verdict_knowledge_date(test_index) -> date:
    return pd.to_datetime(str(test_index[-1])).date()


# --- report -------------------------------------------------------------------


def _book_summaries(books) -> dict:
    return {name: r.summary() for name, r in books.items()}


def _build_report(as_of, signal_id, names, panel, test_index, grid, picks, verdict, books,
                  cap, latest_edges, capacity_floor, cm) -> dict:
    survives_venue = books[VENUE_FEASIBLE.name].net_sharpe
    survives_stress = books[STRESS.name].net_sharpe
    rep = {
        "milestone": "M5",
        "signal_id": signal_id,
        "feature_family": "residual_statarb",
        "as_of": str(as_of),
        "exit_gate": {
            # BUILD_PLAN M5: venue-feasible survival · DSR>0 · capacity floor · registry complete
            "survives_venue_feasible": bool(verdict.promoted),
            "dsr_passes": bool(verdict.dsr.passes),
            "beats_baselines": bool(verdict.beats_baselines),
            "pbo_below_threshold": bool(np.isfinite(verdict.pbo.pbo) and verdict.pbo.pbo < 0.5),
            "clears_capacity_floor": bool(verdict.capacity_ok),
            "capacity_floor": capacity_floor,
            "promoted": bool(verdict.promoted),
        },
        "inputs": {
            "n_names": len(names),
            "n_dates_panel": int(panel.shape[0]),
            "n_oos_test_dates": int(len(test_index)),
            "n_trials_grid": len(grid),
            "oos_test_start": str(test_index[0]),
            "oos_test_end": str(test_index[-1]),
            "cost_model": {"cost_bps": cm.cost_bps, "borrow_bps_annual": cm.borrow_bps_annual},
            "short_eligible_available": False,  # L2 empty — stress book is the binding short test
        },
        "verdict": verdict.summary(),
        "books": _book_summaries(books),
        "capacity_curve": [{"notional_scale": c.notional_scale, "net_sharpe": c.net_sharpe,
                            "total_net_return": c.total_net_return} for c in cap],
        "walk_forward_picks": picks,
        "latest_edge_snapshot": (
            {"effective_date": str(latest_edges.effective_date),
             "n_edges": latest_edges.n_edges,
             "window_start": str(latest_edges.window_start),
             "window_end": str(latest_edges.window_end)}
            if latest_edges is not None else None),
        "venue_net_sharpe": float(survives_venue) if np.isfinite(survives_venue) else None,
        "stress_net_sharpe": float(survives_stress) if np.isfinite(survives_stress) else None,
    }
    return rep


def _empty_report(as_of, signal_id, *, reason: str) -> dict:
    return {"milestone": "M5", "signal_id": signal_id, "as_of": str(as_of),
            "exit_gate": {"promoted": False}, "reason": reason}


def _write_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
    return path

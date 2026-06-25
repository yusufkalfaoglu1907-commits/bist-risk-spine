"""M6 geopolitical-event signal runner — differential exposure through the M4 judge (BUILD_PLAN M6).

The one L2-touching piece of M6. All the statistics live in the pure modules built and pinned
before the data (``differential_exposure`` / ``channel_stress`` / ``taxonomy`` + the M4
``signals`` judge); this orchestrates them over the real store and records an honest verdict:

  1. PIT-read ``events`` + ``event_targets`` (the modeled ``TARGETS`` edge) + ``betas`` + raw
     ``total_returns`` at ``as_of`` (no raw SELECT, no network — §4);
  2. assemble the per-(event, channel) ``EventSpec`` list and the per-channel exposure panels
     (a name's exposure to a channel **is** its M2 beta to that channel's factor — the exposure
     tensor reuse, §8);
  3. build a small **grid** of differential-exposure variants (quantile × drift window × exposure
     lag) → per-variant venue-feasible P&L = the honest ``n_trials`` family for the DSR/PBO haircut;
  4. **purged walk-forward selection** (López de Prado): per fold, apply the variant best on the
     *train* block to the *test* block → a genuinely out-of-sample spread;
  5. judge that OOS candidate through the full promotion gate (beat-the-ladder · DSR · PBO ·
     venue-feasible) across all three books + a capacity curve;
  6. emit the **channel-stress second output** (§240): each event's prior shock re-priced through
     the exposure tensor → worst-exposed names + stress P&L (the risk spine);
  7. land the verdict into ``signal_registry`` and write the §4 JSON audit report.

**M6 exit gate** (BUILD_PLAN): the spread clears M4 in the **venue-feasible** book · enough
**control names survive a typical event** (§238 — a thin cross-section is a down-weight, reported,
never fabricated dispersion) · the channel-stress P&L reconciles against a hand-checked shock.

Inputs are **injectable** (``inputs=``) so the exit-gate self-test runs on a synthetic world with
no L2/network. ``short_eligible`` is empty in L2 (M2 Matriks blocker) ⇒ the **stress book** is the
binding short test until that map lands — surfaced in the report, not silently ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tmkg.events.channel_stress import channel_stress_pnl, shock_from_prior
from tmkg.events.differential_exposure import EventSpec, differential_exposure_weights
from tmkg.signals.backtest import (
    STRESS,
    VENUE_FEASIBLE,
    CostModel,
    capacity_curve,
    purged_walk_forward_splits,
    run_all_books,
    run_book,
)
from tmkg.signals.promotion import evaluate_candidate
from tmkg.signals.stats import sharpe_ratio

# Representative factor per channel (role). A name's exposure to a channel = its beta to this
# factor. 'sector' is intentionally absent — exposure there is name-specific (each name's own
# sector index), so an event targeting 'sector' is SURFACED as skipped by differential-exposure
# rather than mapped to one index (honest §4). Override via the ``channel_factor`` argument.
DEFAULT_CHANNEL_FACTOR: dict[str, str] = {
    "market": "XU100",
    "fx": "USDTRY",
    "rates_cds": "TRCDS5Y",
    "energy": "BRENT",
    "foreign_flow": "FFLOW",
    "holding": "XHOLD",
}


@dataclass(frozen=True)
class EventVariant:
    """One differential-exposure design point — the grid the DSR/PBO haircut counts as a trial."""
    quantile: float
    drift_window: int
    exposure_lag: int

    def label(self) -> str:
        return f"q{self.quantile}_d{self.drift_window}_l{self.exposure_lag}"


def default_event_grid() -> list[EventVariant]:
    """A small honest grid over the spread's design choices (quantile × drift window × lag)."""
    return [
        EventVariant(q, d, lag)
        for q in (0.2, 0.3)
        for d in (3, 5, 10)
        for lag in (1, 2)
    ]


# --- L2 reads (all through PITAccess) ---------------------------------------


def build_betas_by_channel(
    betas_df: pd.DataFrame, channel_factor: dict[str, str], *, index: pd.Index | None = None
) -> dict[str, pd.DataFrame]:
    """Pivot L2 ``betas`` into one (date × symbol) exposure panel per channel.

    For each channel, selects the rows for its representative factor and pivots to date×symbol.
    A channel whose factor has no betas is **omitted** (differential-exposure then skips events
    targeting it with a zeroed diagnostic — surfaced, not a fabricated zero exposure)."""
    out: dict[str, pd.DataFrame] = {}
    if betas_df.empty:
        return out
    b = betas_df.copy()
    b["bar_date"] = pd.to_datetime(b["bar_date"]).dt.date
    for channel, factor in channel_factor.items():
        sub = b[b["factor"] == factor]
        if sub.empty:
            continue
        panel = sub.pivot_table(index="bar_date", columns="symbol", values="beta", aggfunc="mean")
        panel.columns.name = None
        panel = panel.sort_index()
        if index is not None:
            panel = panel.reindex(index)
        out[channel] = panel
    return out


def event_specs_from_l2(events_df: pd.DataFrame, targets_df: pd.DataFrame) -> list[EventSpec]:
    """Join ``events`` (event_date) with ``event_targets`` (channel, shock_sign) into EventSpecs —
    one per (event, channel). An event with no target rows contributes nothing (no modeled
    channel ⇒ no cross-section). Rows are deduped on the latest knowledge_date per event."""
    if events_df.empty or targets_df.empty:
        return []
    ev = events_df.copy()
    ev["event_date"] = pd.to_datetime(ev["event_date"]).dt.date
    # latest-known event row per event_id (bitemporal: PITAccess already filtered knowledge_date)
    ev = ev.sort_values("knowledge_date").drop_duplicates("event_id", keep="last")
    date_by_id = dict(zip(ev["event_id"], ev["event_date"]))
    tg = targets_df.sort_values("knowledge_date").drop_duplicates(
        ["event_id", "channel"], keep="last")
    specs: list[EventSpec] = []
    for _, r in tg.iterrows():
        d = date_by_id.get(r["event_id"])
        if d is None:
            continue  # a target with no visible parent event -> skip (never invent a date)
        specs.append(EventSpec(
            event_id=str(r["event_id"]), event_date=d,
            channel=str(r["channel"]), shock_sign=int(r["shock_sign"])))
    return specs


def _load_event_inputs(store, as_of: date, *, channel_factor: dict[str, str]):
    """Read events/event_targets/betas/total_returns through PITAccess and assemble the inputs."""
    from tmkg.pit.access import PITAccess

    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        events_df = pit.series("events")
        targets_df = pit.series("event_targets")
        betas_df = pit.series("betas")
        rets = pit.series("total_returns")
    finally:
        con.close()

    specs = event_specs_from_l2(events_df, targets_df)
    if rets.empty:
        return specs, {}, pd.DataFrame(), events_df
    rets = rets.copy()
    rets["bar_date"] = pd.to_datetime(rets["bar_date"]).dt.date
    ret_panel = rets.pivot_table(index="bar_date", columns="symbol", values="ret_usd",
                                 aggfunc="mean").sort_index()
    ret_panel.columns.name = None
    betas_by_channel = build_betas_by_channel(betas_df, channel_factor, index=ret_panel.index)
    return specs, betas_by_channel, ret_panel, events_df


# --- purged walk-forward variant selection (shared shape with the M5 runner) ---


def _walk_forward_oos(variant_weights, variant_pnls, *, n_splits, purge, embargo):
    labels = list(variant_weights)
    dates = variant_pnls.index
    splits = purged_walk_forward_splits(len(dates), n_splits=n_splits, purge=purge, embargo=embargo)
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
        picks.append({"test_start": str(dates[te[0]]), "test_end": str(dates[te[-1]]),
                      "chosen": best, "train_sharpe": float(train_sr[best])})
    test_index = template.index[sorted(set(test_pos))]
    return oos, test_index, picks


# --- the runner -------------------------------------------------------------


def run_m6_event_signal(
    store=None,
    *,
    as_of: date,
    inputs=None,
    channel_factor: dict[str, str] | None = None,
    grid: list[EventVariant] | None = None,
    control_threshold: float = 0.5,
    n_splits: int = 5,
    purge: int = 5,
    embargo: int = 5,
    cost_model: CostModel | None = None,
    capacity_floor: float = 0.0,
    pbo_threshold: float = 0.5,
    signal_id: str = "m6_event_diffexp",
    write_l2: bool = True,
    report_dir: str | Path | None = None,
) -> dict:
    """Run the M6 differential-exposure event signal end-to-end and return the verdict report.

    ``inputs`` = ``(event_specs, betas_by_channel, ret_panel, events_df)`` may be injected (the
    exit-gate self-test path); otherwise they are read from ``store`` through PITAccess. The grid
    length is the honest ``n_trials`` haircut. Promotion keys on the walk-forward-selected OOS
    candidate across all three books + capacity. Also emits the channel-stress second output and,
    when ``write_l2``, lands the verdict into ``signal_registry``."""
    channel_factor = channel_factor or DEFAULT_CHANNEL_FACTOR
    grid = grid or default_event_grid()
    cm = cost_model or CostModel()

    if inputs is None:
        if store is None:
            raise ValueError("run_m6_event_signal needs either `store` or injected `inputs`")
        specs, betas_by_channel, ret_panel, events_df = _load_event_inputs(
            store, as_of, channel_factor=channel_factor)
    else:
        specs, betas_by_channel, ret_panel, events_df = inputs

    if not specs or not betas_by_channel or ret_panel.empty:
        return _empty_report(as_of, signal_id, reason="no events / exposures / returns at as_of")

    # align every exposure panel + the returns onto one date index
    idx = ret_panel.index
    betas_by_channel = {ch: p.reindex(idx) for ch, p in betas_by_channel.items()}
    cols = ret_panel.columns
    betas_by_channel = {ch: p.reindex(columns=cols) for ch, p in betas_by_channel.items()}

    # ---- one weight panel per variant -> venue-feasible P&L (the trial family) ----
    variant_weights: dict[str, pd.DataFrame] = {}
    variant_pnls: dict[str, pd.Series] = {}
    control_diags = None
    for v in grid:
        w, diags = differential_exposure_weights(
            betas_by_channel, specs, drift_window=v.drift_window, quantile=v.quantile,
            exposure_lag=v.exposure_lag, control_threshold=control_threshold)
        res = run_book(w, ret_panel, book=VENUE_FEASIBLE, cost_model=cm,
                       short_eligible=None, limit_lock=None)
        variant_weights[v.label()] = w
        variant_pnls[v.label()] = res.pnl
        control_diags = diags  # diagnostics are variant-invariant (same events/exposure)
    trial_pnls = pd.DataFrame(variant_pnls)

    # ---- purged walk-forward OOS selection ----
    oos_w, test_index, picks = _walk_forward_oos(
        variant_weights, trial_pnls, n_splits=n_splits, purge=purge, embargo=embargo)
    if len(test_index) < 3:
        return _empty_report(as_of, signal_id, reason="insufficient OOS test history")

    fwd_oos = ret_panel.loc[test_index]
    cand_oos = oos_w.loc[test_index]
    trial_oos = trial_pnls.loc[test_index]

    verdict = evaluate_candidate(
        cand_oos, fwd_oos, n_trials=len(grid),
        returns_for_baselines=fwd_oos, book=VENUE_FEASIBLE, cost_model=cm,
        short_eligible=None, limit_lock=None,
        trial_pnls=trial_oos, pbo_threshold=pbo_threshold, capacity_floor=capacity_floor,
    )
    books = run_all_books(cand_oos, fwd_oos, cost_model=cm, short_eligible=None, limit_lock=None)
    cap = capacity_curve(cand_oos, fwd_oos, book=VENUE_FEASIBLE, cost_model=cm,
                         short_eligible=None, limit_lock=None)

    # ---- channel-stress second output (§240): re-price the latest exposure tensor ----
    stress = _channel_stress_report(betas_by_channel, specs, events_df)

    report = _build_report(as_of, signal_id, ret_panel, betas_by_channel, test_index, grid,
                           picks, verdict, books, cap, control_diags, stress, capacity_floor, cm)

    if write_l2 and store is not None:
        _land_registry(store, verdict, signal_id, test_index, grid, picks, report_dir)
    if report_dir is not None:
        _write_report(report, Path(report_dir) / f"{signal_id}_report.json")
    return report


# --- channel-stress second output -------------------------------------------


def _exposure_matrix(betas_by_channel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The latest symbol × channel exposure tensor slice (each channel's last beta row)."""
    cols = {ch: p.iloc[-1] for ch, p in betas_by_channel.items() if not p.empty}
    return pd.DataFrame(cols)


def _channel_stress_report(betas_by_channel, specs, events_df) -> dict:
    """For each event type present, re-price the latest exposure tensor against its prior shock and
    report the worst-exposed names + portfolio-neutral stress (§240 risk spine, inferred tier)."""
    exposures = _exposure_matrix(betas_by_channel)
    if exposures.empty or events_df is None or events_df.empty:
        return {"scenarios": [], "note": "no exposures/events to re-price"}
    ev = events_df.drop_duplicates("event_id")
    scenarios = []
    for etype, grp in ev.groupby("event_type"):
        sev = float(grp["severity"].dropna().mean()) if grp["severity"].notna().any() else 1.0
        try:
            shock = shock_from_prior(str(etype), severity=sev)
            res = channel_stress_pnl(exposures, shock)
        except (ValueError, KeyError):
            continue  # an event type with no shockable exposure column -> surfaced by omission
        s = res.summary()
        s.update({"event_type": etype, "n_events": int(len(grp)), "mean_severity": sev,
                  "tier": "inferred"})
        scenarios.append(s)
    return {"scenarios": scenarios}


# --- persistence + report ---------------------------------------------------


def _land_registry(store, verdict, signal_id, test_index, grid, picks, report_dir) -> None:
    from tmkg.signals.registry import (
        build_registry_entry,
        write_registry_entry,
        write_registry_report,
    )
    entry = build_registry_entry(
        verdict, signal_id=signal_id,
        hypothesis=("cross-sectional differential exposure: within an event window, the spread "
                    "between high- and low-exposure names to the shocked channel under-reacts "
                    "and drifts (geopolitical-event pillar, §236)"),
        feature_family="event_diffexp",
        knowledge_date=pd.to_datetime(str(test_index[-1])).date(),
        train_start=None, train_end=None,
        test_start=pd.to_datetime(str(test_index[0])).date(),
        test_end=pd.to_datetime(str(test_index[-1])).date(),
        cost_model="CostModel(default 10bps + 100bps/yr borrow)",
        purge_embargo=f"purged_walk_forward n_splits={len(picks)} purge=5 embargo=5",
    )
    write_registry_entry(store, entry)
    if report_dir is not None:
        write_registry_report(entry, Path(report_dir) / f"{signal_id}_registry_report.json")


def _build_report(as_of, signal_id, ret_panel, betas_by_channel, test_index, grid, picks,
                  verdict, books, cap, control_diags, stress, capacity_floor, cm) -> dict:
    venue = books[VENUE_FEASIBLE.name].net_sharpe
    stress_sr = books[STRESS.name].net_sharpe
    cf = [d.control_fraction for d in (control_diags or [])]
    return {
        "milestone": "M6",
        "signal_id": signal_id,
        "feature_family": "event_diffexp",
        "as_of": str(as_of),
        "exit_gate": {
            "spread_clears_venue_feasible": bool(verdict.promoted),
            "dsr_passes": bool(verdict.dsr.passes),
            "beats_baselines": bool(verdict.beats_baselines),
            "clears_capacity_floor": bool(verdict.capacity_ok),
            # §238: enough usable controls survive a typical event (median control fraction)
            "median_control_fraction": float(np.median(cf)) if cf else None,
            "promoted": bool(verdict.promoted),
        },
        "inputs": {
            "n_dates": int(ret_panel.shape[0]),
            "n_names": int(ret_panel.shape[1]),
            "channels_with_exposure": sorted(betas_by_channel),
            "n_events_channels": len(control_diags or []),
            "n_trials_grid": len(grid),
            "n_oos_test_dates": int(len(test_index)),
            "short_eligible_available": False,  # L2 empty — stress book is the binding short test
        },
        "verdict": verdict.summary(),
        "books": {name: r.summary() for name, r in books.items()},
        "capacity_curve": [{"notional_scale": c.notional_scale, "net_sharpe": c.net_sharpe}
                           for c in cap],
        "control_survival": [d.as_dict() for d in (control_diags or [])][:50],
        "channel_stress": stress,
        "walk_forward_picks": picks,
        "venue_net_sharpe": float(venue) if np.isfinite(venue) else None,
        "stress_net_sharpe": float(stress_sr) if np.isfinite(stress_sr) else None,
    }


def _empty_report(as_of, signal_id, *, reason: str) -> dict:
    return {"milestone": "M6", "signal_id": signal_id, "as_of": str(as_of),
            "exit_gate": {"promoted": False}, "reason": reason}


def _write_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
    return path

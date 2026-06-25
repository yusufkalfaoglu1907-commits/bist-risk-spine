"""M6 event-signal runner — the exit-gate self-test (BUILD_PLAN.md M6).

Drives ``run_m6_event_signal`` end-to-end on an injected synthetic world (no L2/network):
a planted **persistent** exposure-drift edge is **promoted** through the venue-feasible book +
walk-forward OOS selection + the M4 gate, while a shuffled-exposure **null is rejected**. Also
asserts the §238 control-survival diagnostic and the §240 channel-stress second output are
produced. The pure spread/gate math is pinned in test_differential_exposure.py / the M4 suite;
this proves the *orchestration* (grid → walk-forward → 3 books → stress → verdict) holds together.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tmkg.events.differential_exposure import EventSpec
from tmkg.events.run_event_signal import EventVariant, run_m6_event_signal


def _const_exposure_panel(betas: pd.Series, index) -> pd.DataFrame:
    return pd.DataFrame(np.tile(betas.to_numpy(), (len(index), 1)),
                        index=index, columns=betas.index)


def _planted_event_world(*, drift: float, shuffle: bool, seed: int = 0):
    """On each event's ENTRY day the cross-section moves with exposure: return_i = drift·beta_i
    (+ noise); every other day is pure noise. The move is concentrated on the entry day and the
    events are well separated, so it is NOT time-series autocorrelated — the high-minus-low
    fx-exposure spread captures it while the persistence / own-event / market baselines cannot
    (symmetric betas ⇒ ~zero market mean). ``shuffle`` breaks the exposure↔drift link → a null.
    Returns the runner's injected ``(specs, betas_by_channel, ret_panel, events_df)``."""
    rng = np.random.default_rng(seed)
    T, N = 340, 40
    index = pd.date_range("2025-01-01", periods=T, freq="B").date
    names = [f"S{i}" for i in range(N)]
    betas = pd.Series(np.linspace(-2.0, 2.0, N), index=names)
    panel = _const_exposure_panel(betas, index)

    event_pos = list(range(20, T - 15, 13))  # ~24 well-separated events (low turnover)
    specs = [EventSpec(f"E{k}", index[p], "fx", +1) for k, p in enumerate(event_pos)]

    drive = (betas.sample(frac=1.0, random_state=seed).set_axis(names) if shuffle else betas)
    rets = pd.DataFrame(rng.normal(0, 0.002, size=(T, N)), index=index, columns=names)
    for p in event_pos:
        rets.iloc[p] += drift * drive.to_numpy()  # concentrated entry-day cross-sectional move

    events_df = pd.DataFrame({
        "event_id": [s.event_id for s in specs],
        "event_type": "fx_monetary_shock",
        "severity": 0.8,
        "knowledge_date": [s.event_date for s in specs],
    })
    return specs, {"fx": panel}, rets, events_df


_GRID = [EventVariant(0.3, 1, 1), EventVariant(0.2, 1, 1), EventVariant(0.3, 3, 1)]


def test_planted_event_drift_is_promoted_through_runner():
    inputs = _planted_event_world(drift=0.02, shuffle=False)
    rep = run_m6_event_signal(
        as_of=date(2026, 6, 25), inputs=inputs, grid=_GRID, write_l2=False, capacity_floor=0.0)
    gate = rep["exit_gate"]
    assert gate["beats_baselines"], rep["verdict"]
    assert gate["dsr_passes"], rep["verdict"]
    assert gate["promoted"], rep["verdict"]
    # §238 control diagnostic present; §240 stress second output produced
    assert gate["median_control_fraction"] is not None
    scen = rep["channel_stress"]["scenarios"]
    assert scen and scen[0]["event_type"] == "fx_monetary_shock"
    assert "fx" in scen[0]["shocked_channels"]
    assert scen[0]["worst_exposed"]                     # worst-exposed names listed


def test_shuffled_event_null_is_rejected_through_runner():
    inputs = _planted_event_world(drift=0.02, shuffle=True)
    rep = run_m6_event_signal(
        as_of=date(2026, 6, 25), inputs=inputs, grid=_GRID, write_l2=False, capacity_floor=0.0)
    assert not rep["exit_gate"]["promoted"], rep["verdict"]


def test_no_inputs_returns_empty_report_not_crash():
    empty = ([], {}, pd.DataFrame(), pd.DataFrame())
    rep = run_m6_event_signal(as_of=date(2026, 6, 25), inputs=empty, write_l2=False)
    assert rep["exit_gate"]["promoted"] is False
    assert "reason" in rep

"""Cross-sectional differential-exposure event signal (system-design-v2.md §236, M6 alpha).

Classical single-name event studies "break in Turkey because events cluster — FX, politics, and
regional shocks overlap constantly, so you can rarely attribute a return move to one event in a
time-series." The defensible design is **cross-sectional differential exposure**: within an event
window, sort firms by their *exposure to the shocked channel* and trade the **spread** between
high- and low-exposure portfolios (a difference-in-differences that nets out the market-wide move
and isolates the channel). The **under-reaction drift** in that spread is the tradable signal.

A name's exposure to a channel is its M2 beta to that channel's factor (``taxonomy.CHANNELS`` ≡
the factor-ladder roles) — so this module consumes the existing exposure tensor and emits a
(date × symbol) weight panel that goes straight through the M4 promotion gate. No new estimation.

The §238 caveat is first-class, not a footnote: when the dominant channel shocks the whole tape
(19 Mar 2025), clean low-exposure **control** names barely exist and the spread thins out in
exactly the events that matter most. ``control_survival`` measures, per event, how many usable
controls there are; a thin cross-section is reported as a **down-weight**, never papered over with
fabricated exposure dispersion.

PIT: exposure is read with an ``exposure_lag`` (known strictly before entry) and the position is
held over the post-event drift window only — the caller pre-aligns ``fwd_returns`` so the weights
at date *d* earn the return realised at *d* (the gate trusts that alignment; §promotion docstring).

Pure: panels in, a weight panel + per-event diagnostics out. No network, no L2, no PIT object.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from tmkg.events.taxonomy import CHANNELS
from tmkg.signals.promotion import dollar_neutral_unit


@dataclass(frozen=True)
class EventSpec:
    """One event's incidence on a single channel (the per-(event,channel) `event_targets` row).

    ``event_date`` is when it occurred; ``channel`` is the shocked factor-ladder role; ``shock_sign``
    is the signed direction (±1). An event that targets several channels is several ``EventSpec``s —
    the cross-section is built per channel, so a multi-channel event contributes one sort per channel.
    """
    event_id: str
    event_date: date
    channel: str
    shock_sign: int


@dataclass(frozen=True)
class ControlSurvival:
    """The §238 diagnostic for one event: how thin is the usable cross-section."""
    event_id: str
    n_names: int            # names with a known exposure at entry
    n_high: int             # high-exposure leg size (the shocked names)
    n_control: int          # low-|exposure| control names (the clean comparison leg)
    control_fraction: float  # n_control / n_names — a low value ⇒ down-weight, not fabricate

    def as_dict(self) -> dict:
        return {"event_id": self.event_id, "n_names": self.n_names, "n_high": self.n_high,
                "n_control": self.n_control, "control_fraction": self.control_fraction}


def event_cross_section_weights(
    exposure: pd.Series, shock_sign: int, *, quantile: float = 0.3
) -> pd.Series:
    """Long the top-``quantile`` names by shock-aligned exposure, short the bottom — the high-minus-low
    differential-exposure spread for one event's cross-section.

    ``exposure`` is the signed beta to the shocked channel (one value per name; NaN = unknown, dropped
    — never treated as zero exposure). The legs are dollar-neutral and unit-gross (``Σ|w| = 1``). A
    degenerate cross-section (no exposure dispersion / fewer than two distinct values) maps to all-zero
    (no bet) rather than a fabricated tilt."""
    if not 0.0 < quantile < 0.5:
        raise ValueError(f"quantile must be in (0, 0.5), got {quantile!r}")
    e = (shock_sign * exposure).dropna()
    w = pd.Series(0.0, index=exposure.index)
    if e.nunique() < 2:
        return w  # no dispersion -> no differential exposure -> flat
    hi = e.quantile(1.0 - quantile)
    lo = e.quantile(quantile)
    if not (hi > lo):
        return w
    w.loc[e.index[e >= hi]] = 1.0
    w.loc[e.index[e <= lo]] = -1.0
    return dollar_neutral_unit(w.to_frame().T).iloc[0]


def control_survival(
    exposure: pd.Series, *, control_threshold: float = 0.5, event_id: str = ""
) -> ControlSurvival:
    """Count usable low-exposure **control** names for one event (§238 thin-cross-section guard).

    A control is a name whose **absolute** exposure to the shocked channel is below
    ``control_threshold`` — the clean, barely-shocked comparison leg the diff-in-diff needs. This is
    deliberately an *absolute* threshold, not a quantile: a quantile always returns the same fraction
    and would hide the very failure §238 warns about — when the channel moves the *whole* tape, every
    name is highly exposed and there are **no** low-exposure controls. A low ``control_fraction`` then
    tells the runner to down-weight the spread, never to fabricate exposure dispersion."""
    e = exposure.dropna()
    n = int(e.shape[0])
    if n == 0:
        return ControlSurvival(event_id, 0, 0, 0, 0.0)
    abs_e = e.abs()
    n_control = int((abs_e <= control_threshold).sum())
    n_high = int((abs_e > control_threshold).sum())
    return ControlSurvival(event_id, n, n_high, n_control, n_control / n)


def _entry_pos(index: pd.Index, event_date: date) -> int | None:
    """First index position on/after ``event_date`` (the earliest date we could enter the spread)."""
    dates = pd.to_datetime(pd.Series(list(index))).dt.date
    after = np.where(dates.to_numpy() >= event_date)[0]
    return int(after[0]) if after.size else None


def differential_exposure_weights(
    betas_by_channel: Mapping[str, pd.DataFrame],
    events: Sequence[EventSpec],
    *,
    drift_window: int = 5,
    quantile: float = 0.3,
    exposure_lag: int = 1,
    control_threshold: float = 0.5,
) -> tuple[pd.DataFrame, list[ControlSurvival]]:
    """Assemble the (date × symbol) differential-exposure weight panel over a set of events.

    For each event: read the shocked channel's exposure **lagged** (``exposure_lag`` days before entry
    — known point-in-time), form the high-minus-low cross-section, and hold it over the post-event
    **drift window** (``[entry .. entry+drift_window-1]``). Overlapping events superpose; each date row
    is then renormalised to dollar-neutral unit gross so the panel is directly gate-comparable. Returns
    the weight panel (zero outside any drift window) and the per-event ``ControlSurvival`` diagnostics.

    ``betas_by_channel`` maps each channel to a (date × symbol) signed-exposure panel sharing one date
    index; an event whose channel is absent (or that lands too early for the lag, or past the index) is
    **skipped with a zeroed diagnostic**, never silently given a fabricated exposure."""
    if not betas_by_channel:
        raise ValueError("no exposure panels supplied")
    bad = [ch for ch in betas_by_channel if ch not in CHANNELS]
    if bad:
        raise ValueError(f"exposure channels not in CHANNELS: {bad}")
    template = next(iter(betas_by_channel.values()))
    index = template.index
    columns = template.columns
    acc = pd.DataFrame(0.0, index=index, columns=columns)
    diags: list[ControlSurvival] = []

    for ev in events:
        panel = betas_by_channel.get(ev.channel)
        if panel is None:
            diags.append(ControlSurvival(ev.event_id, 0, 0, 0, 0.0))
            continue
        pos = _entry_pos(index, ev.event_date)
        if pos is None or pos - exposure_lag < 0:
            diags.append(ControlSurvival(ev.event_id, 0, 0, 0, 0.0))
            continue
        exposure = panel.iloc[pos - exposure_lag].reindex(columns)
        diags.append(control_survival(
            exposure, control_threshold=control_threshold, event_id=ev.event_id))
        w = event_cross_section_weights(exposure, ev.shock_sign, quantile=quantile)
        last = min(pos + drift_window, len(index))
        acc.iloc[pos:last] = acc.iloc[pos:last].add(w, axis=1)

    weights = dollar_neutral_unit(acc)
    return weights, diags

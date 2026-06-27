"""Scenario re-pricing core — build the exposure tensor and re-price scenarios against it.

Pure (DataFrames in, results out): no network, no L2, no PIT. The arithmetic is delegated to the
audited ``events.channel_stress`` engine (a single exposure·shock dot product over shared
channels); this module is the thin layer that (a) pivots L2 ``betas`` into the latest symbol×channel
exposure tensor and (b) maps a ``Scenario`` onto it, carrying the scenario's identity/tier and the
engine's coverage honesty (unmodelled channels surfaced, NaN exposure → no modelled impact, empty
intersection raises) into a structured result.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tmkg.events.channel_stress import StressResult, channel_stress_pnl
from tmkg.risk.scenarios import Scenario

# Channel → representative L2 factor. A name's exposure to a channel **is** its M2 beta to this
# factor (the exposure-tensor reuse, system-design §8). Mirrors the M6 runner's mapping but kept
# local so the risk tool does not depend on the (concluded) alpha event runner. 'sector' is absent
# by design — exposure there is name-specific, surfaced as an unmodelled channel rather than mapped
# to one index.
CHANNEL_FACTOR: dict[str, str] = {
    "market": "XU100",
    "fx": "USDTRY",
    "rates_cds": "TRCDS5Y",
    "energy": "BRENT",
    "foreign_flow": "FFLOW",
    "holding": "XHOLD",
}


def latest_exposure_tensor(
    betas_df: pd.DataFrame, *, channel_factor: dict[str, str] | None = None
) -> pd.DataFrame:
    """Pivot L2 ``betas`` into the **latest** symbol × channel exposure tensor.

    For each channel, take each symbol's most-recent beta to that channel's representative factor.
    A channel whose factor has no betas is omitted (its column simply does not exist → it surfaces
    later as an *unmodelled* channel for any scenario that shocks it, never a fabricated zero
    exposure, §4). Returns an empty frame if there are no betas."""
    channel_factor = channel_factor or CHANNEL_FACTOR
    if betas_df is None or betas_df.empty:
        return pd.DataFrame()
    b = betas_df.copy()
    b["bar_date"] = pd.to_datetime(b["bar_date"]).dt.date
    cols: dict[str, pd.Series] = {}
    for channel, factor in channel_factor.items():
        sub = b[b["factor"] == factor]
        if sub.empty:
            continue
        latest = (sub.sort_values("bar_date").groupby("symbol")["beta"].last())
        cols[channel] = latest
    if not cols:
        return pd.DataFrame()
    tensor = pd.DataFrame(cols).sort_index()
    tensor.index.name = "symbol"
    return tensor


def realized_channel_shock(
    factors_df: pd.DataFrame,
    *,
    start,
    end,
    channel_factor: dict[str, str] | None = None,
    methods: dict[str, str] | None = None,
) -> dict[str, float]:
    """Compute a real, unit-correct channel-shock vector from factor **levels** over [start, end].

    For each channel's factor, the realized move is taken from the factor levels (``factors.value``)
    consistently with how its M2 betas were fit (registry ``method``):

      * ``simple`` / ``log`` (multiplicative: XU100/USDTRY/BRENT/XHOLD) → ``value_end/value_start − 1``;
      * ``diff`` (rate/CDS level, TRCDS5Y) → ``value_end − value_start`` (the level change the betas saw);
      * ``level`` (FFLOW, already an innovation) → the **mean** flow level over the window.

    This is the trustworthy scenario source: real realized numbers in native units, no fabrication
    (§4). A channel with no factor data in the window is omitted (surfaced, not zero-filled).
    ``methods`` maps factor → method (default ``simple`` for any factor not listed)."""
    channel_factor = channel_factor or CHANNEL_FACTOR
    methods = methods or {}
    if factors_df is None or factors_df.empty:
        return {}
    f = factors_df.copy()
    f["bar_date"] = pd.to_datetime(f["bar_date"]).dt.date
    win = f[(f["bar_date"] >= start) & (f["bar_date"] <= end)]
    out: dict[str, float] = {}
    for channel, factor in channel_factor.items():
        sub = win[win["factor"] == factor].sort_values("bar_date")
        vals = sub["value"].dropna()
        if vals.empty:
            continue
        method = methods.get(factor, "simple")
        if method in ("simple", "log"):
            if vals.iloc[0] == 0:
                continue
            out[channel] = float(vals.iloc[-1] / vals.iloc[0] - 1.0)
        elif method == "diff":
            out[channel] = float(vals.iloc[-1] - vals.iloc[0])
        elif method == "level":
            out[channel] = float(vals.mean())
        else:  # unknown method -> treat as simple, the conservative default
            if vals.iloc[0] == 0:
                continue
            out[channel] = float(vals.iloc[-1] / vals.iloc[0] - 1.0)
    return out


@dataclass(frozen=True)
class ScenarioResult:
    """A scenario re-priced against an exposure tensor: the StressResult plus scenario identity."""
    scenario: Scenario
    stress: StressResult

    def summary(self) -> dict:
        s = self.stress.summary()
        s.update({
            "scenario": self.scenario.name,
            "tier": self.scenario.tier,
            "description": self.scenario.description,
            "shocks": {k: float(v) for k, v in self.scenario.shocks.items()},
        })
        return s


def reprice_scenario(
    exposures: pd.DataFrame, scenario: Scenario, *, weights: pd.Series | None = None
) -> ScenarioResult:
    """Re-price ``exposures`` (symbol × channel betas) against one ``Scenario``.

    Delegates to ``channel_stress_pnl`` (exact dot product, coverage tracking, loud-fail on an
    empty channel intersection). When ``weights`` is given (a symbol→weight book), the result also
    carries the portfolio stress P&L."""
    stress = channel_stress_pnl(exposures, dict(scenario.shocks), weights=weights)
    return ScenarioResult(scenario=scenario, stress=stress)


def reprice_suite(
    exposures: pd.DataFrame, scenarios: dict[str, Scenario], *, weights: pd.Series | None = None
) -> dict[str, ScenarioResult]:
    """Re-price a whole library against the same exposure tensor.

    A scenario that shocks **only** unmodelled channels (no exposure column intersects) is surfaced
    by ``channel_stress_pnl`` raising — caught here and skipped from the results with the reason
    carried on the report side, never returned as a misleading all-zero stress (§4)."""
    out: dict[str, ScenarioResult] = {}
    for name, sc in scenarios.items():
        try:
            out[name] = reprice_scenario(exposures, sc, weights=weights)
        except ValueError:
            continue  # no shockable channel for this scenario on this tensor — reported as skipped
    return out

"""Channel-stress scenarios — the event engine's *second output* (system-design-v2.md §240).

"Every major Event also emits a channel shock vector (signed shocks to FX/CDS/oil/gas/rates and
the affected geographies), independent of any alpha signal. The portfolio is re-priced against
that vector via the exposure tensor (§8) to produce a stress P&L and a worst-exposed-names list.
This gives the system a risk spine it otherwise lacks, and it reuses the exact same exposure
machinery — the event engine serves alpha **and** resilience from one model."

The arithmetic is a single dot product: a name's stress return under a shock is its exposure row
(betas to each channel's factor) dotted with the signed shock vector, over the channels they
share. No statistics, no fitting — this is *re-pricing*, so the test is a hand-checked
reconciliation to the penny (the M6 exit-gate "stress P&L reconciles against a hand-checked
shock"), not a Sharpe.

Honesty rules (§4): a channel in the shock with **no exposure column** is recorded as
unmodelled, never invented; a name with an unknown (NaN) exposure to a shocked channel
contributes nothing on that channel (no modelled exposure → no modelled impact), and its
coverage is reported so a thin row is visible rather than masquerading as resilient; an empty
channel intersection **raises** rather than returning an all-zero stress that would look real.

Pure: DataFrames in, a StressResult out. No network, no L2, no PIT.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tmkg.events.taxonomy import CHANNELS, prior_shock_vector


@dataclass(frozen=True)
class StressResult:
    """The re-pricing of a book/universe against one channel-shock vector.

    ``per_name`` — stress return per symbol (Σ exposure·shock over shared channels).
    ``portfolio_pnl`` — Σ wᵢ·per_nameᵢ when weights were given, else ``None``.
    ``shocked_channels`` — channels actually applied (present in both shock and exposures).
    ``unmodelled_channels`` — shock channels with no exposure column (surfaced, not dropped).
    ``coverage`` — per name, the fraction of shocked channels on which it had a known exposure.
    """
    per_name: pd.Series
    portfolio_pnl: float | None
    shocked_channels: tuple[str, ...]
    unmodelled_channels: tuple[str, ...]
    coverage: pd.Series

    def worst_exposed(self, n: int = 10) -> pd.Series:
        """The ``n`` names with the most negative (most damaging) stress return."""
        return self.per_name.sort_values(ascending=True).head(n)

    def best_exposed(self, n: int = 10) -> pd.Series:
        """The ``n`` names that benefit most under the shock."""
        return self.per_name.sort_values(ascending=False).head(n)

    def summary(self) -> dict:
        return {
            "n_names": int(self.per_name.shape[0]),
            "shocked_channels": list(self.shocked_channels),
            "unmodelled_channels": list(self.unmodelled_channels),
            "portfolio_pnl": (float(self.portfolio_pnl)
                              if self.portfolio_pnl is not None else None),
            "worst_exposed": {k: float(v) for k, v in self.worst_exposed(5).items()},
            "best_exposed": {k: float(v) for k, v in self.best_exposed(5).items()},
            "min_coverage": float(self.coverage.min()) if not self.coverage.empty else None,
        }


def channel_stress_pnl(
    exposures: pd.DataFrame,
    shock: Mapping[str, float],
    *,
    weights: pd.Series | None = None,
) -> StressResult:
    """Re-price ``exposures`` (symbol × channel signed betas) against a signed ``shock`` vector.

    ``shock`` maps channel → signed magnitude in channel-shock units (e.g. ``{"fx": +0.10}`` =
    a 10% TRY depreciation on the USDTRY factor). The per-name stress return is the dot of the
    name's exposure row with the shock over the channels they share; an unknown (NaN) exposure
    contributes 0 on that channel (and lowers the name's reported coverage). When ``weights`` is
    given (a symbol→weight Series, e.g. a book), the portfolio stress P&L is Σ wᵢ·per_nameᵢ over
    the names present in both.

    Raises ``ValueError`` on an empty shock, an out-of-vocabulary channel, or an empty channel
    intersection (no exposure column matches any shocked channel) — failing loud rather than
    returning an all-zero stress that would read as "resilient" (§4)."""
    if not shock:
        raise ValueError("empty shock vector — refusing to return a null stress")
    bad = [ch for ch in shock if ch not in CHANNELS]
    if bad:
        raise ValueError(f"shock channels not in CHANNELS: {bad}")

    shocked = [ch for ch in shock if ch in exposures.columns]
    unmodelled = tuple(ch for ch in shock if ch not in exposures.columns)
    if not shocked:
        raise ValueError(
            f"none of the shocked channels {list(shock)} have an exposure column "
            f"{list(exposures.columns)} — cannot re-price, refusing a fabricated zero stress"
        )

    sub = exposures[shocked]
    shock_vec = pd.Series({ch: float(shock[ch]) for ch in shocked})
    # NaN exposure -> 0 contribution on that channel (no modelled exposure -> no modelled impact);
    # coverage records how much of the shock each name was actually exposed-modelled for.
    contrib = sub.mul(shock_vec, axis=1)
    per_name = contrib.sum(axis=1, skipna=True)
    coverage = sub.notna().sum(axis=1) / float(len(shocked))

    portfolio_pnl: float | None = None
    if weights is not None:
        common = per_name.index.intersection(weights.index)
        portfolio_pnl = float((per_name.loc[common] * weights.loc[common]).sum())

    return StressResult(
        per_name=per_name,
        portfolio_pnl=portfolio_pnl,
        shocked_channels=tuple(shocked),
        unmodelled_channels=unmodelled,
        coverage=coverage,
    )


def shock_from_prior(event_type: str, *, severity: float = 1.0) -> dict[str, float]:
    """Build a signed channel-shock vector from the modeled taxonomy prior, scaled by ``severity``.

    Turns ``taxonomy.TYPE_CHANNEL_PRIOR[event_type]`` (signs) into a magnitude vector
    ``{channel: sign*severity}`` — a coarse, ``inferred``-tier stress scenario for an event whose
    per-event channel mapping has not been extracted. ``severity`` is the event's 0..1 magnitude
    (``events.severity``); the resulting stress P&L is only as trustworthy as that prior, which is
    why the registry/report tags it inferred. Raises ``KeyError`` on an unknown event type."""
    if not np.isfinite(severity):
        raise ValueError(f"severity must be finite, got {severity!r}")
    return {ch: sign * float(severity) for ch, sign in prior_shock_vector(event_type).items()}

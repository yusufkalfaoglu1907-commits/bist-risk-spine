"""Scenario definitions — signed channel-shock vectors to re-price the exposure tensor against.

A ``Scenario`` is a named, signed shock over the channel vocabulary (``taxonomy.CHANNELS`` = the
factor-ladder roles). The sign convention is the taxonomy's (taxonomy.py lines 22-24):

    market (XU100)   − = broad selloff
    fx (USDTRY)      + = TRY depreciation
    rates_cds        + = stress / spread widening
    energy (oil/gas) + = price up
    holding (XHOLD)  + = holding index up
    foreign_flow     − = net non-resident outflow   (see UNITS note)
    sector           +/− = the sector index move    (name-specific — no single factor; see note)

**Units.** A shock magnitude is in the *same units as that channel's factor return* — for the
fractional-return channels (market/fx/rates_cds/energy/holding) that is a return fraction
(``fx: +0.10`` = USDTRY +10%). Re-pricing is ``per_name = Σ_channel beta_channel · shock_channel``
(``events.channel_stress``), so the units must match the betas, which are betas to those factor
returns. Two channels are deliberately excluded from the **stylized** library to avoid a unit
mismatch that would fabricate a meaningful-looking but wrong number (§4):

  * ``foreign_flow`` — FFLOW is a weekly net-flow *level* in USD-mn (σ≈249), not a fraction, so a
    "+0.10" there is ~nothing while "+0.10" on fx is a 10% move. A flow shock must be given in its
    native USD-mn scale. The **empirical** path (``scenario_from_factor_returns``) handles it
    correctly because it reads each factor's real realized return in native units.
  * ``sector`` — exposure is name-specific (each name's own sector index), so there is no single
    sector factor to shock uniformly. Surfaced, not silently mapped.

A stylized scenario is ``tier="stylized"``: a documented *hypothetical*, useful for "what if," and
explicitly not a fitted or verified quantity. An empirically-derived scenario is ``tier="empirical"``.
Neither is ever written to L2 as a fact.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from tmkg.events.taxonomy import CHANNELS

# Channels whose factor return is a fraction → safe to express stylized shocks as fractions.
FRACTIONAL_CHANNELS: frozenset[str] = frozenset({"market", "fx", "rates_cds", "energy", "holding"})

SCENARIO_TIERS: frozenset[str] = frozenset({"stylized", "empirical"})


@dataclass(frozen=True)
class Scenario:
    """A named signed channel-shock vector. ``shocks`` maps channel → signed magnitude.

    ``tier`` records provenance trust: ``stylized`` = a documented hypothetical (not fitted),
    ``empirical`` = derived from real realized factor returns. ``provenance`` is a free-text note
    on where the magnitudes come from. Validated on construction: channels ∈ CHANNELS, finite
    non-empty shocks, known tier.
    """
    name: str
    description: str
    shocks: Mapping[str, float]
    tier: str = "stylized"
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("scenario needs a name")
        if not self.shocks:
            raise ValueError(f"scenario {self.name!r} has an empty shock vector")
        if self.tier not in SCENARIO_TIERS:
            raise ValueError(f"scenario {self.name!r} tier {self.tier!r} not in {sorted(SCENARIO_TIERS)}")
        bad = [ch for ch in self.shocks if ch not in CHANNELS]
        if bad:
            raise ValueError(f"scenario {self.name!r} channels not in CHANNELS: {bad}")
        nonfinite = [ch for ch, v in self.shocks.items() if not np.isfinite(v)]
        if nonfinite:
            raise ValueError(f"scenario {self.name!r} has non-finite shocks on {nonfinite}")

    def as_dict(self) -> dict:
        return {"name": self.name, "tier": self.tier, "description": self.description,
                "provenance": self.provenance, "shocks": {k: float(v) for k, v in self.shocks.items()}}


def scenario_from_factor_returns(
    name: str,
    channel_returns: Mapping[str, float],
    *,
    description: str = "",
    provenance: str = "",
) -> Scenario:
    """Build an **empirical** scenario from real realized factor returns per channel.

    ``channel_returns`` maps channel → the factor's actual realized return over the chosen window
    (e.g. the USDTRY return 2025-03-18→03-25 for the channel ``fx``). Because these are real
    native-unit returns, the resulting shock vector is unit-correct for *every* channel, including
    ``foreign_flow`` — this is the trustworthy way to re-price a real historical episode. The PIT
    L2 read that produces ``channel_returns`` lives in the runner; this builder stays pure."""
    return Scenario(name=name, description=description, shocks=dict(channel_returns),
                    tier="empirical", provenance=provenance or "realized factor returns over a window")


# --- The stylized library (documented hypotheticals; tier='stylized') -------------------------
# Magnitudes are round, defensible one-move assumptions on the fractional channels only. They are
# illustrative scenario inputs, NOT fitted or measured market data (§4) — re-pricing them is exact,
# but their realism is only as good as the assumption. For a real episode, prefer the empirical path.

STYLIZED_SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (
        Scenario(
            name="try_depreciation_10",
            description="A 10% one-move TRY depreciation (USDTRY +10%), all other channels held flat.",
            shocks={"fx": +0.10},
            provenance="single-channel stress on the FX rung",
        ),
        Scenario(
            name="rate_cds_widening",
            description="A sharp sovereign-risk repricing: 5Y CDS / rates +20%, broad market −4%.",
            shocks={"rates_cds": +0.20, "market": -0.04},
            provenance="credit-stress rung + a modest equity drawdown",
        ),
        Scenario(
            name="oil_spike",
            description="An energy supply shock: Brent +20%, a small TRY depreciation (importer drag).",
            shocks={"energy": +0.20, "fx": +0.03},
            provenance="energy rung + the TRY's typical co-move on an oil import bill",
        ),
        Scenario(
            name="global_risk_off",
            description=("A broad global risk-off: XU100 −8%, USDTRY +5%, CDS +8%, Brent −6% "
                         "(growth scare). Stylized, multi-channel."),
            shocks={"market": -0.08, "fx": +0.05, "rates_cds": +0.08, "energy": -0.06},
            provenance="stylized risk-off co-move across the macro rungs",
        ),
        Scenario(
            name="imamoglu_shock_stylized",
            description=("A stylized analog of the 2025-03-19 İmamoğlu-detention regime break: "
                         "XU100 −12%, USDTRY +10%, CDS +15%, holding index −10%. STYLIZED — for a "
                         "unit-correct re-pricing of the real episode use the empirical path over "
                         "the actual 2025-03-18→03-25 factor returns."),
            shocks={"market": -0.12, "fx": +0.10, "rates_cds": +0.15, "holding": -0.10},
            provenance="stylized after the Mar-2025 political-shock regime; magnitudes are round assumptions",
        ),
    )
}


def stylized_library() -> dict[str, Scenario]:
    """The named stylized scenario library (a fresh dict copy)."""
    return dict(STYLIZED_SCENARIOS)

"""Event taxonomy + channel vocabulary + the modeled type→channel prior (M6).

system-design-v2.md §234 fixes the **top-level event taxonomy** (11 categories) and §119
the `TARGETS` edge: an `Event` shocks one or more *channels*. We do not re-derive the
design — we implement it. The single decision this module makes concrete is: **a "channel"
is a factor-ladder role** (``tmkg.factors.neutralize.DEFAULT_LADDER``), because a name's
*exposure* to a channel is exactly its beta to the factor at that rung — already estimated
and stored in L2 ``betas`` (M2). That reuse is the whole point of the exposure tensor (§8):
the event engine routes a shock through the same channels the residual machine strips.

Three things live here, all **pure data**, no network / L2 / PIT:
  * ``EVENT_TYPES`` — the 11 §234 categories as stable string keys;
  * ``CHANNELS`` — the channel vocabulary (= the ladder roles);
  * ``TYPE_CHANNEL_PRIOR`` — a **modeled** default signed incidence per event type (which
    channels a typical event of that type shocks, and in which direction). This is a coarse
    ``inferred``-tier prior used (a) as a fallback `TARGETS` when no per-event extraction
    exists and (b) to seed channel-stress scenarios. The actual per-event mapping comes from
    ingestion/LLM extraction and **overrides** the prior; the prior is never presented as a
    verified edge (§5: an inferred edge is not silently promoted into a verified path).

Sign convention — the sign is the direction of the *channel factor's level* move:
  fx (USDTRY) +  = TRY depreciation ·  rates_cds (yield/CDS) + = stress/widening ·
  energy (oil/gas) + = price up ·  foreign_flow (net non-resident inflow) − = outflow ·
  market (XU100) − = broad selloff ·  sector index +/− = the sector index move.
"""
from __future__ import annotations

from tmkg.factors.neutralize import DEFAULT_LADDER

# === The §234 top-level event taxonomy (11 categories) =====================
# Stable keys (snake_case). Faithful to the design list; the per-event TARGETS sign is
# what distinguishes a rupture from a rapprochement, an EM-tightening from an easing, etc.
EVENT_TYPES: tuple[str, ...] = (
    "fx_monetary_shock",            # FX / monetary shock
    "sanctions_export_controls",    # sanctions / export controls
    "armed_conflict",               # armed conflict
    "diplomatic_shift",             # diplomatic rupture / rapprochement (sign = direction)
    "trade_policy_tariff",          # trade-policy / tariff
    "energy_supply_disruption",     # energy-supply disruption
    "cbrt_regulatory_action",       # CBRT / regulatory action
    "elections_political_transition",  # elections / political transition
    "terror_security",              # terror / security
    "natural_disaster",             # natural disaster
    "pandemic",                     # pandemic
)

# === Channels = factor-ladder roles ========================================
# The exposure tensor is built on these; an event's incidence is a signed vector over them.
# (Geography is a §119 TARGETS target too, but there is no geography *factor* in L2 yet, so a
# geography-incident event routes through whichever factor proxies it — e.g. a regional
# conflict via energy/fx — until a geo-exposure layer exists. Surfaced, not silently dropped.)
CHANNELS: frozenset[str] = frozenset(DEFAULT_LADDER)

# Date-precision vocabulary for `events.date_precision` (a month-precision event must not be
# treated as a single-day shock — the differential-exposure window widens with precision).
DATE_PRECISIONS: frozenset[str] = frozenset({"day", "week", "month", "quarter"})

# Evidence tiers for the soft `TARGETS` edge (§5).
EVIDENCE_TIERS: frozenset[str] = frozenset({"verified", "inferred"})

# === The modeled type→channel prior (inferred tier) ========================
# (channel, sign) pairs a *typical* event of each type shocks. Coarse, defensible defaults;
# magnitude is event-specific so the prior carries sign only. `diplomatic_shift` defaults to
# the **rupture** (risk-off) direction — a rapprochement flips every sign at extraction time.
TYPE_CHANNEL_PRIOR: dict[str, tuple[tuple[str, int], ...]] = {
    "fx_monetary_shock":            (("fx", +1), ("rates_cds", +1), ("market", -1)),
    "sanctions_export_controls":    (("fx", +1), ("foreign_flow", -1), ("sector", -1)),
    "armed_conflict":               (("energy", +1), ("fx", +1), ("rates_cds", +1),
                                     ("foreign_flow", -1)),
    "diplomatic_shift":             (("fx", +1), ("foreign_flow", -1), ("rates_cds", +1)),
    "trade_policy_tariff":          (("sector", -1), ("fx", +1)),
    "energy_supply_disruption":     (("energy", +1), ("market", -1)),
    "cbrt_regulatory_action":       (("rates_cds", +1), ("fx", -1)),
    "elections_political_transition": (("fx", +1), ("rates_cds", +1), ("foreign_flow", -1),
                                       ("market", -1)),
    "terror_security":              (("fx", +1), ("foreign_flow", -1), ("market", -1)),
    "natural_disaster":             (("sector", -1), ("market", -1)),
    "pandemic":                     (("market", -1), ("sector", -1), ("energy", -1)),
}


# === Helpers ===============================================================
def prior_shock_vector(event_type: str) -> dict[str, int]:
    """The modeled signed channel incidence for ``event_type`` as a ``{channel: sign}`` dict.

    Raises ``KeyError`` for an unknown type (never returns an empty/zero vector silently —
    an unrecognised event type is a contract problem, not a no-op shock)."""
    if event_type not in TYPE_CHANNEL_PRIOR:
        raise KeyError(f"unknown event_type {event_type!r}; not in EVENT_TYPES")
    return {ch: sign for ch, sign in TYPE_CHANNEL_PRIOR[event_type]}


def validate() -> None:
    """Raise ``ValueError`` if the taxonomy is malformed: a prior key not in ``EVENT_TYPES``,
    a missing prior, a channel not in ``CHANNELS``, a non-±1 sign, or a duplicate channel
    within one type's prior. Keeps the modeled mapping honest to the design (run as a test)."""
    if len(set(EVENT_TYPES)) != len(EVENT_TYPES):
        raise ValueError("duplicate key in EVENT_TYPES")
    if set(TYPE_CHANNEL_PRIOR) != set(EVENT_TYPES):
        missing = set(EVENT_TYPES) - set(TYPE_CHANNEL_PRIOR)
        extra = set(TYPE_CHANNEL_PRIOR) - set(EVENT_TYPES)
        raise ValueError(f"prior/type mismatch: missing={missing} extra={extra}")
    for etype, pairs in TYPE_CHANNEL_PRIOR.items():
        seen: set[str] = set()
        for ch, sign in pairs:
            if ch not in CHANNELS:
                raise ValueError(f"{etype!r}: channel {ch!r} not in CHANNELS {sorted(CHANNELS)}")
            if sign not in (-1, +1):
                raise ValueError(f"{etype!r}: channel {ch!r} sign {sign!r} not in (-1, +1)")
            if ch in seen:
                raise ValueError(f"{etype!r}: duplicate channel {ch!r} in prior")
            seen.add(ch)

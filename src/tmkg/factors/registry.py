"""Canonical core factor set — the M2 manifest (BUILD_PLAN.md M2, system-design-v2.md §200).

The compute engine (``betas.py`` / ``neutralize.py``) is factor-agnostic: it takes a
``specs`` map ({factor name -> return method}) and a factor-name *strip order*. Nothing
encoded *which* factors the model actually has, so the order/specs were assembled by hand
at the call site — and the synthetic tests only worked because their factor *names* happened
to equal the ladder *roles* ("market", "fx"). With real names (XU100, USDTRY, BRENT …) a
role-keyed order silently matches nothing.

This module is the single source of truth: the design's core factor set as data, each
factor tagged with the ladder **role** it is stripped at, its return **method**, and its
**source**. From it we derive the two things the engine needs — ``specs()`` and the
concrete name-ordered ladder ``ladder_order()`` — so a run is config-driven and the ladder
is auditable, not retyped.

Design anchors (do not re-derive — implement):
- Factor set (§ lines 92 / 193 / 291): USD/TRY, EUR/TRY, Brent, gas, TRY 2y/10y,
  Turkey 5y CDS, MSCI-EM, gold, VIX, BIST sector indices, **foreign-flow / ownership-tier**.
- Neutralization ladder (§200): **market → FX → rates/CDS → energy/commodity → sector
  → foreign-flow/ownership-tier → holding-group → residual**. This is ``neutralize.DEFAULT_LADDER``.

Two factors are *not yet sourced* and are marked ``status`` accordingly so they are
**surfaced, never silently dropped** (the M2 exit-gate rule):
- ``foreign_flow`` — **blocked** on the Matriks custodian-code list (BUILD_LOG Q1); the §5
  comovement driver. Its ladder slot is reserved so it slots in the moment Q1 returns.
- the holding-group factor is **derived** in M2 (from the L1 CONTROLS clusters); the BIST
  holding index ``XHOLD`` is encoded as an available proxy until the derived factor lands.

Pure data + helpers. No network, no L2, no PIT.
"""
from __future__ import annotations

from dataclasses import dataclass

from tmkg.factors.neutralize import DEFAULT_LADDER
from tmkg.factors.series import DIFF, LEVEL, LOG, SIMPLE

# Sourcing status of a factor's *series* (not a tradability flag):
AVAILABLE = "available"  # a series is sourced (or directly sourceable) and lands in L2
BLOCKED = "blocked"      # cannot be built yet — an external dependency is outstanding
DERIVED = "derived"      # computed in-house from other L1/L2 inputs, not ingested raw
_STATUSES = frozenset({AVAILABLE, BLOCKED, DERIVED})


@dataclass(frozen=True)
class Factor:
    """One core factor. ``name`` is the L2 ``factors.factor`` key the ingest lands and the
    signal layer reads back; ``role`` is the ladder rung it is stripped at (must be one of
    ``neutralize.DEFAULT_LADDER``); ``method`` is its return rule (``simple``/``log``/``diff``
    — a *rate* level like a yield/CDS/VIX takes ``diff``, a price/index/FX takes ``simple``);
    ``source`` + ``series_id`` say where the live ingest pulls it from."""

    name: str
    role: str
    method: str
    source: str          # matriks | evds | fred | scrape | derived
    series_id: str       # the identifier at `source` (vendor symbol / FRED code / …)
    status: str = AVAILABLE
    frequency: str = "daily"  # daily | weekly — a lower-freq factor is forward-filled onto
    #                           the daily grid at panel-build (transient; L2 keeps true obs)
    note: str = ""


# === The core factor set ===================================================
# Ordering within this tuple is the stable within-rung order; across rungs the ladder
# position comes from DEFAULT_LADDER, not from this list's order. Vendor symbols are the
# best current mapping — the live ingest session confirms exact Matriks tickers and the
# full BIST sector-index roster (the sector rung below is the principal subset).
CORE_FACTORS: tuple[Factor, ...] = (
    # market — the broad/global risk mode, stripped first.
    Factor("XU100", "market", SIMPLE, "matriks", "XU100",
           note="BIST-100 — the domestic market factor"),
    Factor("MSCIEM", "market", SIMPLE, "matriks", "EEM",
           status=BLOCKED,
           note="MSCI-EM/EEM: NOT on Matriks historicalData or symbolSearch, NOT on FRED "
                "(verified 2026-06-22); market rung covered by XU100+VIX meanwhile. "
                "Try Matriks foreignMarkets tool or an alt provider to unblock."),
    Factor("VIX", "market", DIFF, "fred", "VIXCLS",
           note="global volatility; a LEVEL -> diff, not pct (FRED adapter already built)"),
    # fx — USD/TRY, EUR/TRY (§200 names both at this rung).
    Factor("USDTRY", "fx", SIMPLE, "matriks", "USDTRY",
           note="USD-primary base FX; also cross-checkable via EVDS"),
    Factor("EURTRY", "fx", SIMPLE, "matriks", "EURTRY"),
    # rates / CDS — yields and the sovereign CDS are LEVELS in their own units -> diff.
    Factor("TRY2Y", "rates_cds", DIFF, "worldgovbonds", "TRY2Y",
           note="TRY 2y benchmark yield (%, daily) — WorldGovernmentBonds; a rate level -> "
                "diff. Spikes 35->50% across the 2025-03-19 shock"),
    Factor("TRY10Y", "rates_cds", DIFF, "worldgovbonds", "TRY10Y",
           note="TRY 10y benchmark yield (%, daily) — WorldGovernmentBonds; a rate level -> diff"),
    Factor("TRCDS5Y", "rates_cds", DIFF, "worldgovbonds", "TRCDS5Y",
           note="Turkey 5y sovereign CDS (bps, daily) — WorldGovernmentBonds public chart "
                "API (W3-sanctioned, not Investing.com/ToS); a level in bps -> diff. >=2015"),
    # energy / commodity — Brent, natural gas, gold (a commodity, hence this rung).
    Factor("BRENT", "energy", SIMPLE, "matriks", "BRENT"),
    Factor("NATGAS", "energy", SIMPLE, "fred", "DHHNGSP",
           note="natural gas (Henry Hub spot, FRED DHHNGSP) — Turkey imports ~all "
                "hydrocarbons (§64); NOT a Matriks symbol (verified 2026-06-22)"),
    Factor("GOLD", "energy", SIMPLE, "matriks", "XAUUSD",
           note="gold (commodity) — stripped at the energy/commodity rung; see CONFIRM note"),
    # sector — principal BIST sector indices (the live ingest confirms the full roster).
    Factor("XBANK", "sector", SIMPLE, "matriks", "XBANK", note="banks"),
    Factor("XUSIN", "sector", SIMPLE, "matriks", "XUSIN", note="industrials"),
    Factor("XKMYA", "sector", SIMPLE, "matriks", "XKMYA", note="chemicals/petrochem"),
    Factor("XELKT", "sector", SIMPLE, "matriks", "XELKT", note="utilities/electricity"),
    Factor("XGIDA", "sector", SIMPLE, "matriks", "XGIDA", note="food & beverage"),
    Factor("XUTEK", "sector", SIMPLE, "matriks", "XUTEK", note="technology"),
    # foreign-flow / ownership-tier — THE §5 comovement driver. SOURCE DECIDED (user,
    # 2026-06-22): EVDS weekly non-resident net equity flow as the deep, precise, official
    # primary; institutionalFlow broker-netting (~2025+) is a daily overlay/cross-check (not
    # this factor). TP.MKNETHAR.M7 = "2.1.1 Hisse Senedi" net değişim (weekly net non-resident
    # equity flow, USD mn) — verified across the 2025-03-19 shock (flipped +480 -> -444/-652).
    # A FLOW is already an innovation -> method=LEVEL (the value IS the return, not diffed);
    # weekly -> forward-filled onto the daily grid at panel-build. Custody-based reference
    # (foreign_custody.py) backs the AKD overlay, not this primary leg.
    Factor("FFLOW", "foreign_flow", LEVEL, "evds", "TP.MKNETHAR.M7",
           frequency="weekly",
           note="EVDS weekly non-resident net equity flow (USD mn); LEVEL (flow=return); "
                "ffill weekly->daily at panel-build; stock level = TP.MKNETHAR.M1"),
    # holding-group — derived from L1 CONTROLS clusters in M2; XHOLD index as proxy meanwhile.
    Factor("XHOLD", "holding", SIMPLE, "matriks", "XHOLD",
           note="BIST holding index — available proxy for the M2-derived holding-group factor"),
)


# === Well-formedness (run as a test; cheap to call at import in callers too) ===
def validate(factors: tuple[Factor, ...] = CORE_FACTORS) -> None:
    """Raise ``ValueError`` if the manifest is malformed: unknown role/method/status,
    a duplicate factor name, or a ladder rung (other than the reserved-but-blocked
    ``foreign_flow``) with no factor at all. Keeps the manifest honest to the design."""
    seen: set[str] = set()
    for f in factors:
        if f.name in seen:
            raise ValueError(f"duplicate factor name {f.name!r}")
        seen.add(f.name)
        if f.role not in DEFAULT_LADDER:
            raise ValueError(
                f"factor {f.name!r} role {f.role!r} is not a ladder rung {DEFAULT_LADDER}")
        if f.method not in {SIMPLE, LOG, DIFF, LEVEL}:
            raise ValueError(f"factor {f.name!r} has unknown return method {f.method!r}")
        if f.status not in _STATUSES:
            raise ValueError(f"factor {f.name!r} has unknown status {f.status!r}")
    roles_with_factor = {f.role for f in factors}
    for rung in DEFAULT_LADDER:
        if rung not in roles_with_factor:
            raise ValueError(f"ladder rung {rung!r} has no factor in the manifest")


# === Derivations the engine consumes =======================================
def _select(available_only: bool, factors: tuple[Factor, ...]) -> list[Factor]:
    return [f for f in factors if (f.status != BLOCKED) or not available_only]


def specs(
    *, available_only: bool = True, factors: tuple[Factor, ...] = CORE_FACTORS
) -> dict[str, str]:
    """``{factor name -> return method}`` for ``ingest.pipeline.build_factor_return_panel``.

    ``available_only`` (default) excludes ``blocked`` factors (today: ``foreign_flow``) so a
    real run does not demand a series that cannot exist yet. Pass ``False`` to see the full
    intended set (e.g. to assert what is still owed)."""
    return {f.name: f.method for f in _select(available_only, factors)}


def ladder_order(
    *, available_only: bool = True, factors: tuple[Factor, ...] = CORE_FACTORS
) -> tuple[str, ...]:
    """Factor **names** in neutralization-ladder order (rung position from
    ``DEFAULT_LADDER``, then the manifest's within-rung order). This is the concrete strip
    order ``neutralize.rolling_residuals`` / ``ingest.pipeline.build_residuals`` need — the
    fix for the old role-keyed default that matched no real factor name."""
    rung_pos = {rung: i for i, rung in enumerate(DEFAULT_LADDER)}
    selected = _select(available_only, factors)
    ordered = sorted(
        range(len(selected)), key=lambda i: (rung_pos[selected[i].role], i)
    )
    return tuple(selected[i].name for i in ordered)


def order_present(
    present: list[str] | set[str], *, factors: tuple[Factor, ...] = CORE_FACTORS
) -> tuple[str, ...]:
    """Order an arbitrary set of *present* factor names by their ladder rung. Robust to
    whatever actually landed in L2 (a partial run, a vendor-renamed sector index): an
    unknown name keeps its first-seen relative order *after* all known rungs, so it is
    stripped but never silently reordered ahead of a known rung. This is what
    ``build_residuals`` should call on the panel it actually has."""
    by_name = {f.name: f for f in factors}
    rung_pos = {rung: i for i, rung in enumerate(DEFAULT_LADDER)}
    present_list = list(dict.fromkeys(present))  # de-dupe, keep first-seen
    n_rungs = len(DEFAULT_LADDER)

    def key(item: tuple[int, str]) -> tuple[int, int]:
        idx, name = item
        f = by_name.get(name)
        pos = rung_pos[f.role] if f is not None else n_rungs  # unknown -> after all rungs
        return (pos, idx)

    return tuple(name for _, name in sorted(enumerate(present_list), key=key))


def weekly_factor_names(
    *, available_only: bool = True, factors: tuple[Factor, ...] = CORE_FACTORS
) -> frozenset[str]:
    """Names of factors whose native cadence is weekly (today: ``FFLOW``). The factor-return
    panel forward-fills these onto the daily date grid (a transient alignment, never persisted
    to L2 — §4 keeps the true weekly observations). Daily factors are unaffected."""
    return frozenset(
        f.name for f in _select(available_only, factors) if f.frequency == "weekly")


def blocked_factors(factors: tuple[Factor, ...] = CORE_FACTORS) -> list[Factor]:
    """Factors that cannot be built yet (today: the foreign-flow leg on Matriks Q1).
    Surfaced so a run report can record exactly what the model is still missing."""
    return [f for f in factors if f.status == BLOCKED]

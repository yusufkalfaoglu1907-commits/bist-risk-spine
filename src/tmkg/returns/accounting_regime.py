"""accounting_regime state machine + the no-straddle guard (CLAUDE.md §5, design §3).

Turkish inflation accounting is a **regime state**, not a one-time boundary:

    FY <= 2022   nominal_pre2023        (historical-cost nominal TRY)
    FY 2023-2024 ias29_2023_2024        (TMS-29 / IAS-29 inflation-adjusted)
    FY >= 2025   suspended_2025_2027    (requirement suspended by parliament)

Comparability breaks at TWO switches — nominal→IAS-29 at FY2023 and IAS-29→
suspended at FY2025. So a growth / intensity / materiality figure that straddles
*either* switch is comparing inflation-adjusted against nominal numbers: a
fabricated growth rate. The vendor serves BOTH bases per quarter, so the regime
state SELECTS the comparable basis rather than forcing a restatement — and any
cross-regime calculation must declare it has converted to a common basis, or it
is refused here loudly (RegimeStraddle).

This module is pure (no IO). Ingestion tags each fundamental datum with
``regime_for_period(period)``; calc code routes growth through ``guarded_growth``.
"""
from __future__ import annotations

from tmkg.pit.errors import RegimeStraddle

REGIMES = ("nominal_pre2023", "ias29_2023_2024", "suspended_2025_2027")

# The two comparability-breaking switch years (the FY a new regime begins).
SWITCH_YEARS = (2023, 2025)


def regime_for_period(period: str | int) -> str:
    """Map a fundamental period to its accounting_regime state.

    ``period`` is a fiscal year or a YYYYMM / YYYY-Qn string — only the leading
    4-digit year is read. Raises on a non-year-leading value (never guesses).
    """
    s = str(period).strip()
    if len(s) < 4 or not s[:4].isdigit():
        raise ValueError(f"accounting_regime: cannot read a fiscal year from {period!r}")
    year = int(s[:4])
    if year <= 2022:
        return "nominal_pre2023"
    if year <= 2024:
        return "ias29_2023_2024"
    return "suspended_2025_2027"


def same_regime(period_a: str | int, period_b: str | int) -> bool:
    """True iff both periods sit in the same accounting_regime (directly comparable)."""
    return regime_for_period(period_a) == regime_for_period(period_b)


def guarded_growth(
    value_now: float,
    period_now: str | int,
    value_base: float,
    period_base: str | int,
    *,
    basis_converted: bool = False,
) -> float:
    """``value_now / value_base - 1`` — but REFUSE when the two periods straddle a
    regime switch unless the caller asserts a common-basis conversion.

    Set ``basis_converted=True`` only when both inputs are already on one basis
    (e.g. both the vendor's IAS-29-adjusted figures, or both nominal). Passing it
    when they are not is the exact bug this guard exists to catch — so it is the
    caller's explicit, auditable assertion, never a silent default.
    """
    if not same_regime(period_now, period_base) and not basis_converted:
        raise RegimeStraddle(
            f"growth from {period_base} ({regime_for_period(period_base)}) to "
            f"{period_now} ({regime_for_period(period_now)}) straddles a regime "
            f"switch; convert both to a common basis and pass basis_converted=True."
        )
    if value_base in (0, None):
        raise ValueError("guarded_growth: base value is zero/None; growth undefined")
    return value_now / value_base - 1.0

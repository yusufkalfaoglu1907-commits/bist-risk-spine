"""Market-regime labelling for regime-aware beta estimation (BUILD_PLAN.md M2).

system-design-v2.md §65: "2018 and 2021–2023 FX crises, plus episodic CBRT regime
shifts, break the assumption of stable betas. Estimation must be **rolling and
regime-aware**; a single full-sample beta is meaningless here." The M2 exit gate
(BUILD_PLAN.md) requires betas that are stable *within* a regime and **break across**
the 19 Mar 2025 shock boundary as expected.

A regime is a contiguous calendar span opened by a documented structural break. A
date's regime is the label of the most recent break at or before it. The beta engine
restricts each rolling window to a single regime — a window is never allowed to
straddle a break (the same no-straddle discipline the accounting_regime uses), so
betas re-estimate cleanly on each side of a shock instead of being smeared across it.

These breaks are the *market/return* regime — distinct from ``returns.accounting_regime``
(the IAS-29 reporting state on fundamentals). The two are unrelated state machines.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

# Documented BIST structural breaks (system-design-v2.md §65, §238). Each tuple is
# (break_date, regime_label): the date OPENS the named regime. Ordered ascending.
# Dates before the first break fall in the BASELINE regime.
BASELINE = "baseline_pre2018"
BIST_REGIME_BREAKS: tuple[tuple[date, str], ...] = (
    (date(2018, 8, 10), "tl_crisis_2018"),       # Aug-2018 currency crisis
    (date(2021, 11, 22), "tl_crisis_2021_2022"), # Nov-2021 lira crash (low-rate policy)
    (date(2023, 6, 7), "orthodox_turn_2023"),    # post-election CBRT orthodox turn
    (date(2025, 3, 19), "imamoglu_shock_2025"),  # 19-Mar-2025 detention shock (§238)
    (date(2025, 8, 29), "post_shortban_2025"),   # short-ban final lift (§ short_eligible)
)


def regime_for_date(
    d: date,
    *,
    breaks: Sequence[tuple[date, str]] = BIST_REGIME_BREAKS,
    baseline: str = BASELINE,
) -> str:
    """Return the regime label of the span containing ``d``.

    The label is that of the most recent break at or before ``d``; before the first
    break, ``baseline``. ``breaks`` must be ascending by date — injectable so the beta
    tests can use synthetic boundaries without depending on the real calendar.
    """
    label = baseline
    for bdate, blabel in breaks:
        if d >= bdate:
            label = blabel
        else:
            break
    return label

"""Unit tests for the M2 market-regime labeller (tmkg.factors.regime)."""
from __future__ import annotations

from datetime import date

from tmkg.factors.regime import BASELINE, regime_for_date


def test_baseline_before_first_break():
    assert regime_for_date(date(2015, 1, 1)) == BASELINE


def test_label_is_most_recent_break():
    assert regime_for_date(date(2018, 8, 10)) == "tl_crisis_2018"   # on the break date
    assert regime_for_date(date(2019, 1, 1)) == "tl_crisis_2018"
    assert regime_for_date(date(2022, 1, 1)) == "tl_crisis_2021_2022"
    assert regime_for_date(date(2024, 1, 1)) == "orthodox_turn_2023"


def test_imamoglu_shock_boundary_is_a_distinct_regime():
    """The 19-Mar-2025 shock opens its own regime — the exit-gate boundary."""
    assert regime_for_date(date(2025, 3, 18)) == "orthodox_turn_2023"
    assert regime_for_date(date(2025, 3, 19)) == "imamoglu_shock_2025"
    assert regime_for_date(date(2025, 3, 18)) != regime_for_date(date(2025, 3, 19))


def test_injectable_breaks():
    breaks = ((date(2024, 6, 1), "B"),)
    assert regime_for_date(date(2024, 5, 31), breaks=breaks, baseline="A") == "A"
    assert regime_for_date(date(2024, 6, 1), breaks=breaks, baseline="A") == "B"

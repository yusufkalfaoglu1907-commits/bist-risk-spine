"""Tests for the time-aware outstanding model (analytics/outstanding.py)."""
from __future__ import annotations

import datetime as _dt

from tmkg.analytics.outstanding import (
    outstanding_as_of, is_amortizing_default,
)

D = _dt.date
ASOF = D(2026, 6, 8)


def test_class_default_bullet_vs_amortizing():
    assert is_amortizing_default("TRF") is False     # finansman bonosu = bullet
    assert is_amortizing_default("SUKUK") is False
    assert is_amortizing_default("XS") is True        # eurobond = not assumed bullet
    assert is_amortizing_default("WEIRD") is None
    assert is_amortizing_default(None) is None


def test_matured_is_zero():
    amt, basis = outstanding_as_of(1_000.0, D(2026, 1, 1), ASOF, None, "TRF")
    assert basis == "matured" and amt == 0.0


def test_live_bullet_equals_nominal():
    amt, basis = outstanding_as_of(1_000.0, D(2026, 12, 1), ASOF, None, "TRF")
    assert basis == "bullet" and amt == 1_000.0


def test_amortizer_is_upper_bound():
    # explicit amortizing flag → nominal returned but flagged
    amt, basis = outstanding_as_of(1_000.0, D(2027, 1, 1), ASOF, True, "TRF")
    assert basis == "amortizing-upper-bound" and amt == 1_000.0
    # eurobond class default → also upper bound
    _, basis2 = outstanding_as_of(1_000.0, D(2027, 1, 1), ASOF, None, "XS")
    assert basis2 == "amortizing-upper-bound"


def test_unpriced_returns_none():
    amt, basis = outstanding_as_of(None, D(2027, 1, 1), ASOF, None, "TRF")
    assert basis == "unpriced" and amt is None


def test_unknown_repayment_when_class_and_flag_unknown():
    amt, basis = outstanding_as_of(1_000.0, D(2027, 1, 1), ASOF, None, "WEIRD")
    assert basis == "unknown-repayment" and amt == 1_000.0


def test_explicit_flag_overrides_class_default():
    # class says bullet, but the node was marked amortizing → flag wins
    _, basis = outstanding_as_of(1_000.0, D(2027, 1, 1), ASOF, True, "TRF")
    assert basis == "amortizing-upper-bound"

"""Exceptions that enforce the data contract (CLAUDE.md §4/§5).

These are not ordinary errors — they are the engineered guards that keep the
system honest. Prefer raising one of these loudly over any silent fallback.
"""
from __future__ import annotations


class PITViolation(RuntimeError):
    """A read would leak data with knowledge_date > as_of, or was attempted
    without an as_of date. The single most important guard for an honest backtest."""


class SourceUnreachable(RuntimeError):
    """An ingestion adapter cannot reach an upstream source. The adapter FAILS
    LOUDLY and stops — it never returns placeholder/interpolated data."""


class FabricationGuard(RuntimeError):
    """Code would synthesize, mock, or interpolate market data into L2.
    Fabricated quant data that looks real is the most dangerous bug in this project."""


class ContractDrift(RuntimeError):
    """An adapter smoke_check() found the upstream contract no longer matches the
    committed golden sample (tests/golden/)."""


class IdentityAmbiguous(RuntimeError):
    """The id-bridge (ticker ↔ ISIN ↔ kap_oid ↔ LEI) found more than one Company
    for an identifier. The bridge is a single point of failure (CLAUDE.md §5): it
    REFUSES and logs rather than guess which name a signal should resolve to."""


class RegimeStraddle(RuntimeError):
    """A growth / intensity / materiality figure was computed across two fundamental
    periods in DIFFERENT accounting_regime states (the FY2023 nominal→IAS-29 switch
    or the FY2025 IAS-29→suspended switch) without converting to a common basis
    (CLAUDE.md §5). The regime SELECTS the comparable basis; straddling it silently
    compares inflation-adjusted against nominal figures — a fabricated growth rate."""

"""Point-in-time / bitemporal value types shared across L1 and L2.

Every datum and edge carries when it was true in the world (valid_from/valid_to)
and when we learned it (knowledge_date = publication/declaration date). The PIT
access layer is the only sanctioned reader (see access.py). CLAUDE.md §5.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class EvidenceTier(str, Enum):
    """Soft-edge trust tier. An INFERRED edge is never silently promoted into a
    VERIFIED traversal path; the alpha layer weights inferred edges down (design §4.2)."""

    VERIFIED = "verified"
    INFERRED = "inferred"


@dataclass(frozen=True)
class Provenance:
    """Required on every soft edge / derived datum (CLAUDE.md §5).

    The portfolio layer sizes by ``uncertainty``, not just by point ``confidence`` —
    a high-score/high-dispersion edge does not get a full-size bet.
    """

    source: str
    confidence: float  # point estimate in [0, 1]
    evidence_tier: EvidenceTier
    uncertainty: float | None = None  # dispersion around the point estimate
    method: str | None = None  # 'disclosed' | 'regression' | 'structural' | ...


@dataclass(frozen=True)
class Bitemporal:
    """The three timestamps every L1 edge and L2 row carries."""

    valid_from: date
    valid_to: date | None  # None = still valid (open interval)
    knowledge_date: date  # when WE learned it; the as_of comparison key

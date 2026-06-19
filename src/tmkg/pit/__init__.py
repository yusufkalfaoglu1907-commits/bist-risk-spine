"""tmkg.pit — bitemporal point-in-time access layer (the keystone, M0).

Everything that reads L1 (Kuzu) or L2 (DuckDB) in signal code goes through
PITAccess, which requires an as_of date and refuses to return any row with
knowledge_date > as_of. This is the make-or-break for an honest backtest and
cannot be retrofitted (CLAUDE.md §5). See VERIFICATION.md → PIT-leak detector.
"""
from tmkg.pit.access import PITAccess
from tmkg.pit.errors import (
    ContractDrift,
    FabricationGuard,
    IdentityAmbiguous,
    PITViolation,
    SourceUnreachable,
)
from tmkg.pit.idbridge import IdBridge
from tmkg.pit.types import Bitemporal, EvidenceTier, Provenance

__all__ = [
    "PITAccess",
    "IdBridge",
    "Bitemporal",
    "EvidenceTier",
    "Provenance",
    "PITViolation",
    "SourceUnreachable",
    "FabricationGuard",
    "ContractDrift",
    "IdentityAmbiguous",
]

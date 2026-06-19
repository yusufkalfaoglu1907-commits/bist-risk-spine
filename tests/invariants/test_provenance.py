"""Provenance / evidence-tier invariant (CLAUDE.md §5, design §4.2)."""
from __future__ import annotations

import pytest

from tmkg.pit import EvidenceTier, Provenance


@pytest.mark.invariant
def test_provenance_carries_required_fields():
    p = Provenance(source="GLEIF-L2", confidence=0.95, evidence_tier=EvidenceTier.VERIFIED)
    assert p.source and 0.0 <= p.confidence <= 1.0
    assert EvidenceTier.INFERRED != EvidenceTier.VERIFIED


@pytest.mark.invariant
@pytest.mark.skip(reason="M0/M1: unskip once soft edges are written to L1 via the loaders")
def test_no_soft_edge_without_provenance():
    """Every soft edge in L1 must carry source + confidence + evidence_tier +
    uncertainty, and no INFERRED edge may appear in a VERIFIED-only traversal."""
    ...

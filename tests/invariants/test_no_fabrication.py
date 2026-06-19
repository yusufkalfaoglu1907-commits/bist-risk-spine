"""No-fabrication invariant (CLAUDE.md §4 rule 2)."""
from __future__ import annotations

import pytest

from tmkg.ingest import MatriksAdapter
from tmkg.pit import FabricationGuard, SourceUnreachable


@pytest.mark.invariant
def test_fabrication_guards_exist():
    assert issubclass(FabricationGuard, RuntimeError)
    assert issubclass(SourceUnreachable, RuntimeError)


@pytest.mark.invariant
def test_missing_credentials_raise_not_fabricate():
    """No creds = unreachable. The adapter must FAIL LOUD (SourceUnreachable),
    never return a placeholder bar (offline, deterministic)."""
    a = MatriksAdapter()
    a.username, a.api_key = "", ""
    with pytest.raises(SourceUnreachable):
        a.fetch("marketPrice", action="price", symbol="THYAO")


@pytest.mark.invariant
def test_unresolvable_host_raises_not_fabricate():
    """A network failure must surface as SourceUnreachable, not a fabricated value."""
    a = MatriksAdapter(timeout=2.0)
    a.username, a.api_key = "39617", "sk_live_dummy"
    a.rest_url = "https://matriks.invalid/mcp-api/v1"  # .invalid never resolves (RFC 6761)
    with pytest.raises(SourceUnreachable):
        a.fetch("marketPrice", action="price", symbol="THYAO")


@pytest.mark.invariant
@pytest.mark.skip(reason="M1: unskip once L2 loaders exist — assert nothing under fixtures/ reaches L2")
def test_fixtures_never_reach_l2():
    """Nothing under fixtures/ may be loaded into L2 (illustrative fixtures are
    unit-test-only and must never be sourced as if real)."""
    ...

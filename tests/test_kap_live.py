"""Live KAP adapter drift guard.

These tests hit www.kap.org.tr. They SKIP automatically when KAP is unreachable
so the offline Phase-1 suite (test_phase1.py) still passes in CI. Run explicitly:

    PYTHONPATH=src python -m pytest tests/test_kap_live.py -v

If KAP changes its API again, these fail loudly and point at what drifted.
"""
from __future__ import annotations

import pytest

from tmkg.adapters.kap_adapter import KapAdapter


def _adapter_or_skip():
    try:
        import httpx
        httpx.get("https://www.kap.org.tr", timeout=8)
    except Exception:
        pytest.skip("KAP unreachable — skipping live tests")
    return KapAdapter()


def test_member_list_nonempty_and_shaped():
    with _adapter_or_skip() as a:
        members = a.fetch_members(refresh=True)
        assert len(members) > 400, "IGS member list unexpectedly small"
        kchol = a.find("KCHOL", members)
        assert kchol.kap_oid and kchol.mkk_oid
        assert kchol.is_listed and kchol.primary_ticker == "KCHOL"


def test_disclosures_keyed_on_mkk_oid():
    with _adapter_or_skip() as a:
        kchol = a.find("KCHOL")
        disc = a.fetch_disclosures(kchol.mkk_oid, "2025-01-01", "2025-03-31")
        assert disc, "byCriteria returned nothing for a known active issuer"
        d = disc[0]
        for attr in ("index", "subject", "disclosure_type", "url"):
            assert hasattr(d, attr)


def test_smoke_check_passes():
    with _adapter_or_skip() as a:
        result = a.smoke_check()
        assert result["members"] > 400
        assert result["kchol_disclosures_q1_2025"] > 0

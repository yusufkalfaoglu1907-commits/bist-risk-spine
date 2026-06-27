"""New-listing detector (onboarding 3a) — pure diff + KAP reader, deterministic.

The diff is the capability gap (today nothing notices a new IPO appearing upstream). It keys on the
**stable kap_oid**, not the ticker — tickers drift (KAP renames/multi-codes them), so a raw-ticker diff
over-reports massively; the kap_oid diff finds the genuinely new entity and surfaces ticker renames
separately as id-bridge reconciliations. Retirement candidates are kept (survivorship), only flagged.
"""
from __future__ import annotations

import json

import pytest

from tmkg.ingest.new_listings import diff_listings, upstream_listed_from_kap


def _up(*specs):
    """specs: (kap_oid, ticker) tuples -> {kap_oid: record}."""
    return {oid: {"ticker": tk, "name": f"{tk} A.Ş.", "kap_oid": oid, "mkk_oid": f"MKK-{oid}"}
            for oid, tk in specs}


def test_new_listing_detected_with_identity():
    diff = diff_listings(_up(("O1", "AAA"), ("O2", "BBB"), ("O9", "NEWCO")),
                         known={"O1": "AAA", "O2": "BBB"})
    assert [n["ticker"] for n in diff.new_listings] == ["NEWCO"]
    assert diff.new_listings[0]["kap_oid"] == "O9"
    assert diff.new_listings[0]["mkk_oid"] == "MKK-O9"   # onboarding-ready identity
    assert not diff.retired_candidates and not diff.in_sync


def test_retired_candidate_flagged_not_dropped():
    diff = diff_listings(_up(("O1", "AAA")), known={"O1": "AAA", "O7": "GONE"})
    assert diff.retired_candidates == [{"kap_oid": "O7", "ticker": "GONE"}]
    assert not diff.new_listings


def test_ticker_drift_is_NOT_a_new_listing():
    # same kap_oid, ticker multi-coded upstream ('GARAN, TGB') — graph 'GARAN' is still a token => no change
    diff = diff_listings(_up(("O1", "GARAN, TGB")), known={"O1": "GARAN"})
    assert diff.in_sync
    assert not diff.new_listings and not diff.retired_candidates and not diff.ticker_changes


def test_genuine_ticker_rename_surfaced_separately():
    # same kap_oid but graph ticker absent from upstream tokens => a reconciliation, not a new listing
    diff = diff_listings(_up(("O1", "NEWTK")), known={"O1": "OLDTK"})
    assert not diff.new_listings and not diff.retired_candidates
    assert diff.ticker_changes == [{"kap_oid": "O1", "graph_ticker": "OLDTK", "upstream_ticker": "NEWTK"}]
    assert not diff.in_sync


def test_in_sync_when_universes_match():
    diff = diff_listings(_up(("O1", "AAA"), ("O2", "BBB")), known={"O1": "AAA", "O2": "BBB"})
    assert diff.in_sync


def test_kap_reader_keeps_only_listed_igs(tmp_path):
    path = tmp_path / "kap_members.json"
    path.write_text(json.dumps({"fetched_iso": "2026-06-27", "members": [
        {"ticker": "LIST1", "is_listed": True, "member_type": "IGS", "kap_oid": "1", "mkk_oid": "a", "name": "L1"},
        {"ticker": "DELIS", "is_listed": False, "member_type": "IGS", "kap_oid": "2", "mkk_oid": "b", "name": "D"},
        {"ticker": "FUNDX", "is_listed": True, "member_type": "YFON", "kap_oid": "3", "mkk_oid": "c", "name": "F"},
        {"ticker": "NOOID", "is_listed": True, "member_type": "IGS", "kap_oid": None, "mkk_oid": "d", "name": "n"},
    ]}))
    upstream, prov = upstream_listed_from_kap(path)
    assert set(upstream) == {"1"}            # delisted, non-IGS, and kap_oid-less all excluded
    assert "kap_members.json" in prov


def test_kap_reader_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        upstream_listed_from_kap(tmp_path / "nope.json")

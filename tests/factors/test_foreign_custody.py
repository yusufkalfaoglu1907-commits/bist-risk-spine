"""Foreign-custody reference loader (tmkg.factors.foreign_custody).

Pins the Q1 resolution as code: the committed reference parses and validates, the
authoritative ``(YABANCI)`` custody set is exactly the six MKK foreign-custody codes, and
the custody/execution/domestic buckets are disjoint — a code that leaked across buckets
would double-count or mis-sign the §5 foreign-flow leg. Bad references are REJECTED on
load (validate-then-trust), mirroring the bist_isin reference adapter's stance.
"""
from __future__ import annotations

import json

import pytest

from tmkg.factors import foreign_custody as fc

# The six authoritative non-resident custody member codes (BUILD_LOG 2026-06-22, Q1).
EXPECTED_CUSTODY = {"CIY", "DBY", "EBY", "HYA", "OSM", "TYS"}


def test_committed_reference_loads_and_has_the_six_custody_codes():
    ref = fc.load()  # the real committed data/reference/foreign_custody_codes.json
    assert set(ref.custody_codes) == EXPECTED_CUSTODY
    assert ref.fetched_iso and ref.source  # provenance carried


def test_accessors_return_frozensets_consistent_with_load():
    ref = fc.load()
    assert fc.foreign_custody_codes(ref) == frozenset(EXPECTED_CUSTODY)
    assert isinstance(fc.foreign_execution_brokers(ref), frozenset)
    assert isinstance(fc.domestic_exclusions(ref), frozenset)
    # the curated execution overlay carries the canonical foreign IBs
    assert {"MLB", "HSY", "MSI", "JPM"} <= fc.foreign_execution_brokers(ref)


def test_garanti_bbva_rule_is_encoded():
    """GARANTI BBVA (GRM) is DOMESTIC despite a foreign parent; its foreign custody is the
    separate OSM (YABANCI) code. The two must live in different buckets."""
    ref = fc.load()
    assert "GRM" in ref.domestic_exclusions
    assert "GRM" not in ref.custody_codes
    assert "OSM" in ref.custody_codes
    # the foreign-leg set never overlaps the domestic-exclusion set
    assert fc.foreign_custody_codes(ref).isdisjoint(fc.domestic_exclusions(ref))


def _write(tmp_path, obj):
    p = tmp_path / "ref.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def _good_obj():
    return {
        "schema_version": fc.REFERENCE_SCHEMA_VERSION,
        "source": "test",
        "fetched_iso": "2026-06-22",
        "foreign_custody_members": {"codes": {"CIY": "Citibank Yabanci"}},
        "foreign_execution_brokers": {"codes": {"MLB": "BofA"}},
        "domestic_despite_foreign_parent": {"codes": {"GRM": "Garanti"}},
    }


def test_schema_version_mismatch_is_rejected(tmp_path):
    obj = _good_obj()
    obj["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        fc.load(_write(tmp_path, obj))


def test_malformed_code_is_rejected(tmp_path):
    obj = _good_obj()
    obj["foreign_custody_members"]["codes"] = {"citibank": "lowercase/too long"}
    with pytest.raises(ValueError, match="malformed member code"):
        fc.load(_write(tmp_path, obj))


def test_code_in_two_buckets_is_rejected(tmp_path):
    obj = _good_obj()
    # same code as both foreign custody and a domestic exclusion -> contradiction
    obj["domestic_despite_foreign_parent"]["codes"] = {"CIY": "leaked"}
    with pytest.raises(ValueError, match="custody and domestic"):
        fc.load(_write(tmp_path, obj))


def test_empty_custody_set_is_rejected(tmp_path):
    obj = _good_obj()
    obj["foreign_custody_members"]["codes"] = {}
    with pytest.raises(ValueError, match="no foreign-custody codes"):
        fc.load(_write(tmp_path, obj))

"""GDELT events → L2 landing (offline) + the PIT read-back gate (M6).

Lands the ILLUSTRATIVE fixture's records through ``_land_gdelt_records`` into a temp L2 store
and asserts: only typed-Turkey events are written (confidence-tiered §6), the ``event_targets``
prior seed lands with the §5 soft-edge quartet, the write is PK-idempotent, and — the keystone —
a ``PITAccess`` read dated BEFORE the events' knowledge_date sees nothing while a read on/after
sees them (no look-ahead, §5). No network: the records come from the labelled fixture.
"""
from __future__ import annotations

from datetime import date

import pytest

import tmkg.config as config
from tmkg.ingest.gdelt import parse_gkg_csv
from tmkg.ingest.pipeline import _land_gdelt_records
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess

FIXTURE = config.FIXTURES_PATH / "gdelt" / "gkg_sample.csv"


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _land(tmp_path):
    store = _store(tmp_path)
    records = parse_gkg_csv(FIXTURE.read_text())
    report = _land_gdelt_records(store, records, window="fixture")
    return store, report


def test_landing_writes_only_typed_turkey_events(tmp_path):
    store, report = _land(tmp_path)
    # rows 1 (cbrt), 2 (election), 5 (terror) are typed Turkey events; 3 non-TU, 4 untyped.
    assert report["n_events"] == 3
    assert report["skipped"] == {"non_turkey": 1, "untyped": 1}
    ev = store.read_table("events")
    assert set(ev["event_type"]) == {
        "cbrt_regulatory_action", "elections_political_transition", "terror_security"
    }
    # severity is the MODELED tonal magnitude, never price-derived (terror tone -12 -> saturates 1)
    terror = ev[ev["event_type"] == "terror_security"].iloc[0]
    assert terror["severity"] == pytest.approx(1.0)


def test_event_targets_carry_inferred_provenance(tmp_path):
    store, _ = _land(tmp_path)
    tg = store.read_table("event_targets")
    assert not tg.empty
    assert set(tg["evidence_tier"]) == {"inferred"}          # never silently promoted (§5)
    assert set(tg["source"]) == {"taxonomy_prior"}
    assert tg["shock_magnitude"].isna().all()                # prior is sign-only
    assert set(tg["shock_sign"]).issubset({-1, 1})


def test_landing_is_pk_idempotent(tmp_path):
    store, _ = _land(tmp_path)
    n_ev = len(store.read_table("events"))
    # re-land the same records: PK (event_id, knowledge_date) collides -> ignored, not duplicated.
    records = parse_gkg_csv(FIXTURE.read_text())
    _land_gdelt_records(store, records, window="fixture-relaunch")
    assert len(store.read_table("events")) == n_ev


def test_pit_gate_hides_events_until_knowledge_date(tmp_path):
    store, _ = _land(tmp_path)
    con = store.connect()
    try:
        # the fixture events publish on 2025-03-19 -> a read the day before sees nothing...
        before = PITAccess(as_of=date(2025, 3, 18), l2=con).series("events")
        assert before.empty
        # ...a read on the publication date sees all three typed events.
        after = PITAccess(as_of=date(2025, 3, 19), l2=con).series("events")
        assert len(after) == 3
        targets = PITAccess(as_of=date(2025, 3, 19), l2=con).series("event_targets")
        assert not targets.empty
    finally:
        con.close()

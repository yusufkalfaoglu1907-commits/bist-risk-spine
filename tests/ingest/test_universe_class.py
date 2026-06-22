"""universe_class derivation + ingestion (the per-class segmentation the M2 gate measures)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tmkg.ingest.universe import (
    DEFAULT_CLASS,
    derive_universe_class,
    ingest_universe_class,
)
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


# --- the derivation rule (pure) -------------------------------------------------------
@pytest.mark.parametrize("sector,expected", [
    ("GAYRİMENKUL YATIRIM ORTAKLIKLARI", "gyo_reit"),
    ("HOLDİNGLER VE YATIRIM ŞİRKETLERİ", "holding"),
    ("MENKUL KIYMET YATIRIM ORTAKLIKLARI", "investment_trust"),
    ("GİRİŞİM SERMAYESİ YATIRIM ORTAKLIKLARI", "investment_trust"),
    ("BORSA YATIRIM FONU", "etf"),
    ("BANKALAR", "operating"),
    ("ANA METAL SANAYİ", "operating"),
    ("GIDA, İÇECEK VE TÜTÜN", "operating"),
])
def test_derive_class_from_sector(sector, expected):
    assert derive_universe_class(sector) == expected


def test_unknown_sector_refused_not_guessed():
    # an unresolved sector returns None so the caller refuses rather than mislabels
    assert derive_universe_class(None) is None
    assert derive_universe_class("") is None


def test_default_is_operating():
    assert derive_universe_class("SOME BRAND-NEW SECTOR") == DEFAULT_CLASS


# --- ingestion lands universe_membership, PIT-visible, refusing the unresolvable -------
def test_ingest_lands_classes_and_is_pit_visible(tmp_path):
    store = _store(tmp_path)
    sectors = {
        "EKGYO": "GAYRİMENKUL YATIRIM ORTAKLIKLARI",
        "KCHOL": "HOLDİNGLER VE YATIRIM ŞİRKETLERİ",
        "AKBNK": "BANKALAR",
        "GHOST": None,  # not in the graph -> must be refused, never landed
    }
    rep = ingest_universe_class(
        store, list(sectors), universe="bist_30",
        sector_of=lambda s: sectors.get(s),
        valid_from=date(2023, 1, 2), knowledge_date=date(2023, 1, 2),
    )
    assert rep["n_landed"] == 3
    assert rep["n_refused"] == 1
    assert rep["refused"][0]["symbol"] == "GHOST"
    assert rep["by_class"] == {"operating": 1, "gyo_reit": 1, "holding": 1}

    # read back through PIT at a later as_of: each landed name carries its derived class
    con = store.connect()
    try:
        pit = PITAccess(date(2025, 6, 1), l2=con)
        for sym, want in [("EKGYO", "gyo_reit"), ("KCHOL", "holding"), ("AKBNK", "operating")]:
            u = pit.series("universe_membership", symbol=sym, latest_by="valid_from")
            assert u["universe_class"].dropna().tolist() == [want]
        # the refused name was never landed
        assert pit.series("universe_membership", symbol="GHOST").empty
    finally:
        con.close()


def test_membership_invisible_before_knowledge_date(tmp_path):
    store = _store(tmp_path)
    ingest_universe_class(
        store, ["EKGYO"], universe="bist_30",
        sector_of=lambda s: "GAYRİMENKUL YATIRIM ORTAKLIKLARI",
        valid_from=date(2023, 1, 2), knowledge_date=date(2023, 1, 2),
    )
    con = store.connect()
    try:
        # a read dated before the membership was known returns nothing (no look-ahead, §5)
        pit = PITAccess(date(2022, 12, 31), l2=con)
        assert pit.series("universe_membership", symbol="EKGYO").empty
    finally:
        con.close()

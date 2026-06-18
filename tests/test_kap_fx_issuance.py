"""Offline tests for the FX eurobond issue-certificate extractor (Phase 3.1).

The synthetic pages reproduce the real "İhraç Belgesi" / "Tertip İhraç Belgesi"
field run (verified live against KAP idx 1246045 / 1258577): the certificate is
rendered Turkish-then-English, the ISIN may be dual-listed (XS… / US…), and
perpetual notes carry `VADE: Vadesiz` (no maturity date). Real XS ISINs so
ISO-6166 validation passes.

    PYTHONPATH=src python -m pytest tests/test_kap_fx_issuance.py -v
"""
from __future__ import annotations

import datetime as _dt
import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_issuance_adapter import (
    extract_fx_issuances, FX_ISSUE_BASIS,
    write_fx_issuance_reference, load_fx_issuance_reference,
)
from tmkg.loaders.kap_issuance_backfill import backfill_fx_issuances
from tmkg.analytics.outstanding import (
    outstanding_as_of, CONFIDENT_BASES, UPPER_BOUND_BASES,
)

# A dated senior USD note: ISIN, then labelled fields, then the English repeat.
PAGE_USD = (
    "tertip ihraç belgesine ekte yer verilmiştir. "
    "ISIN:  XS2758916063 TÜRÜ: Özel Sektör Tahvili İHRAÇ TARİHİ: 02.02.2024 "
    "VADE: 03.02.2025 GÜN: 367 DÖVİZ: ABD Doları NOMİNAL TUTAR: 15.000.000 "
    "The issuance certificate approved by CMB for notes to be issued is attached. "
    "ISIN:  XS2758916063 TYPE: Senior Unsecured Notes (Eurobond) "
    "ISSUANCE DATE: 02.02.2024 MATURITY DATE: 03.02.2025 DAYS: 367 CURRENCY: USD"
)

# A perpetual AT1 note with a dual XS/US ISIN and VADE: Vadesiz.
PAGE_PERP = (
    "tertip ihraç belgesine ekte yer verilmiştir. "
    "ISIN:  XS2783589844 / US00971YAK64 TÜRÜ: İlave Ana Sermaye "
    "İHRAÇ TARİHİ: 14.03.2024 VADE: Vadesiz DÖVİZ: ABD Doları "
    "NOMİNAL TUTAR: 600.000.000 The issuance certificate approved by CMB for the "
    "Additional Tier I notes in total of USD 600.000.000 is attached. "
    "ISIN:  XS2783589844 / US00971YAK64 TYPE: Additional Tier I Notes"
)

# A euro-denominated note (Avro → EUR).
PAGE_EUR = (
    "ISIN:  XS1551747733 TÜRÜ: Özel Sektör Tahvili İHRAÇ TARİHİ: 01.11.2022 "
    "VADE: 01.11.2027 DÖVİZ: Avro NOMİNAL TUTAR: 500.000.000 attached."
)

# A domestic TL certificate uses the same template but DÖVİZ: TL and a TR ISIN —
# no XS ISIN at all, so nothing is emitted (the TL listing path owns these).
PAGE_TL = (
    "ISIN:  TRSKCTF22726 TÜRÜ: Finansman Bonosu İHRAÇ TARİHİ: 01.01.2026 "
    "VADE: 12.02.2027 DÖVİZ: TL NOMİNAL TUTAR: 620.000.000 attached."
)


def test_dated_usd_note():
    recs = extract_fx_issuances(PAGE_USD, source="KAP:1246045")["records"]
    assert len(recs) == 1
    r = recs[0]
    assert r["isin"] == "XS2758916063"
    assert r["currency"] == "USD"
    assert r["nominal"] == 15_000_000.0
    assert r["basis"] == FX_ISSUE_BASIS
    assert r["instrument_class"] == "XS"
    assert r["issue_date"] == "2024-02-02"
    assert r["maturity_date"] == "2025-02-03"


def test_perpetual_dual_isin_keeps_xs_and_nulls_maturity():
    recs = extract_fx_issuances(PAGE_PERP, source="KAP:1258577")["records"]
    assert len(recs) == 1
    r = recs[0]
    assert r["isin"] == "XS2783589844"          # the XS leg, not the US 144A leg
    assert r["currency"] == "USD"
    assert r["nominal"] == 600_000_000.0
    assert r["maturity_date"] is None           # Vadesiz = perpetual, honestly null
    assert r["issue_date"] == "2024-03-14"


def test_euro_currency_word():
    recs = extract_fx_issuances(PAGE_EUR, source="KAP:x")["records"]
    assert recs[0]["currency"] == "EUR"
    assert recs[0]["nominal"] == 500_000_000.0


def test_domestic_tl_certificate_yields_nothing():
    # no XS ISIN, and DÖVİZ: TL has no ISO mapping — emit nothing, skip nothing wrong
    assert extract_fx_issuances(PAGE_TL, source="x")["records"] == []


def test_page_gate_requires_nominal_label():
    assert extract_fx_issuances(PAGE_USD.replace("NOMİNAL TUTAR", ""), "x")["records"] == []


def test_unrecognised_currency_is_skipped_not_guessed():
    page = PAGE_USD.replace("ABD Doları", "Bir Şey")
    out = extract_fx_issuances(page, source="x")
    assert out["records"] == []
    assert out["skipped"] and out["skipped"][0]["reason"] == "no-iso-currency"


# --- reference round-trip ---------------------------------------------------

def test_reference_round_trip():
    recs = extract_fx_issuances(PAGE_USD, "KAP:1")["records"]
    p = Path(tempfile.mkdtemp()) / "kap_fx_issuance.json"
    write_fx_issuance_reference(recs, path=p)
    back = load_fx_issuance_reference(p)
    assert [r["isin"] for r in back] == ["XS2758916063"]
    assert back[0]["currency"] == "USD" and back[0]["basis"] == FX_ISSUE_BASIS
    assert load_fx_issuance_reference(Path("/no/such/file.json")) == []


# --- loader (ISIN-exact, update-only) ---------------------------------------

def _graph_with_xs(isin: str):
    conn = connect(Path(tempfile.mkdtemp()) / "fx.kuzu")
    apply_schema(conn)
    conn.execute("CREATE (:Company {uuid:'c-akbnk', ticker:'AKBNK', name:'AKBANK', is_listed:true})")
    conn.execute(
        "MATCH (c:Company {uuid:'c-akbnk'}) "
        "CREATE (s:Security {uuid:$u, isin:$i, currency:'FX'}) "
        "CREATE (c)-[:ISSUES {instrument_class:'XS'}]->(s)",
        {"u": "deb-" + isin, "i": isin})
    return conn


def test_loader_prices_existing_xs_isin_exact():
    isin = "XS2758916063"
    conn = _graph_with_xs(isin)
    recs = extract_fx_issuances(PAGE_USD, "KAP:1246045")["records"]
    stats = backfill_fx_issuances(conn, recs)
    assert stats == {"loader_version": 1, "records_in": 1, "priced": 1, "unmatched_isin": 0}
    row = conn.execute(
        "MATCH (s:Security {isin:$i}) RETURN s.nominal, s.nominal_currency, "
        "s.currency, s.nominal_basis", {"i": isin}).get_next()
    assert row[0] == 15_000_000.0
    assert row[1] == "USD"
    assert row[2] == "USD"                      # 'FX' placeholder upgraded
    assert row[3] == FX_ISSUE_BASIS


def test_loader_is_update_only_unknown_isin_not_created():
    conn = _graph_with_xs("XS2758916063")       # graph has this one only
    recs = extract_fx_issuances(PAGE_PERP, "KAP:x")["records"]   # XS2783589844
    stats = backfill_fx_issuances(conn, recs)
    assert stats["priced"] == 0 and stats["unmatched_isin"] == 1
    n = conn.execute("MATCH (s:Security) RETURN count(s)").get_next()[0]
    assert n == 1                               # nothing created


def test_loader_idempotent():
    isin = "XS2758916063"
    conn = _graph_with_xs(isin)
    recs = extract_fx_issuances(PAGE_USD, "KAP:1")["records"]
    backfill_fx_issuances(conn, recs)
    backfill_fx_issuances(conn, recs)
    n = conn.execute("MATCH (s:Security {isin:$i}) RETURN count(s)", {"i": isin}).get_next()[0]
    assert n == 1


# --- outstanding routing ----------------------------------------------------

def test_fx_basis_is_upper_bound_never_confident():
    # a live FX bond priced at issue size → upper bound, even though XS would
    # otherwise route via the amortizing guess; basis must be reported verbatim
    amt, basis = outstanding_as_of(
        nominal=600_000_000.0, maturity_date=_dt.date(2030, 1, 1),
        as_of=_dt.date(2026, 6, 15), is_amortizing=False, instrument_class="XS",
        nominal_basis=FX_ISSUE_BASIS)
    assert amt == 600_000_000.0
    assert basis == FX_ISSUE_BASIS
    assert basis in UPPER_BOUND_BASES and basis not in CONFIDENT_BASES


def test_matured_fx_bond_drops_to_zero():
    amt, basis = outstanding_as_of(
        nominal=15_000_000.0, maturity_date=_dt.date(2025, 2, 3),
        as_of=_dt.date(2026, 6, 15), is_amortizing=None, instrument_class="XS",
        nominal_basis=FX_ISSUE_BASIS)
    assert amt == 0.0 and basis == "matured"

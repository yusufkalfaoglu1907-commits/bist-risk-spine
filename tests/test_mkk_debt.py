"""MKK corporate-debt adapter + back-fill tests.

Fully offline: extraction/parsing are pure functions; the loader writes into a
temp Kuzu DB; issuer matching reuses the GLEIF brand-token helpers. No network.

    PYTHONPATH=src python -m pytest tests/test_mkk_debt.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tmkg.adapters.bist_isin_adapter import is_valid_isin_any
from tmkg.adapters.mkk_debt_adapter import (
    classify_instrument, parse_maturity, parse_currency, extract_debt,
    write_reference, MkkDebtReference, DebtSecurity,
)
from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders.debt_backfill import (
    match_issuer, match_issuer_detailed, backfill_debt, _brand_set,
    _identity_tokens, _entity_type,
)

# Valid ISINs (ISO 6166 check digit verified):
BOND = "TRSAKFK00006"      # corporate bond, issuer AK FAKTORİNG
BILL = "TRFULUS00001"      # financing bill, ULUSAL FAKTORİNG
SUKUK = "TRDDKVK00009"     # lease cert / sukuk, DK VARLIK KİRALAMA (unlisted SPV)
EUROBOND = "XS1AKBNK0001"  # eurobond, AKBANK
BAD = "TRSAKFK00007"       # bond shape, WRONG check digit


# --- ISIN validation + class mapping ---------------------------------------

def test_is_valid_isin_any_accepts_tr_and_xs():
    assert is_valid_isin_any(BOND)
    assert is_valid_isin_any(EUROBOND)
    assert not is_valid_isin_any(BAD)            # bad check digit
    assert not is_valid_isin_any("XS1AKBNK000")  # too short


def test_classify_instrument_debt_classes():
    assert classify_instrument(BOND) == ("TRS", "BOND")
    assert classify_instrument(BILL) == ("TRF", "FINANCING_BILL")
    assert classify_instrument(SUKUK) == ("TRD", "SUKUK")
    assert classify_instrument(EUROBOND) == ("XS", "EUROBOND")


def test_classify_instrument_ignores_non_debt():
    assert classify_instrument("TRATUPRS91E8") == (None, None)  # equity TRA
    assert classify_instrument("TREACSS00017") == (None, None)  # equity TRE
    assert classify_instrument("TRXSOMETHING1") == (None, None)  # ELÜS
    assert classify_instrument("TRWGRAN000001") == (None, None)  # warrant
    assert classify_instrument(None) == (None, None)


# --- maturity + currency parsing -------------------------------------------

def test_parse_maturity_single_high_confidence():
    iso, conf, method = parse_maturity("ÖZEL SEKTÖR TAHVİLİ 25052027 KUPONLU")
    assert iso == "2027-05-25" and conf == 0.9 and method == "ddmmyyyy-single"


def test_parse_maturity_multiple_takes_latest_low_confidence():
    # issue date + maturity date both present -> latest, flagged for review
    iso, conf, method = parse_maturity("İHRAÇ 01012025 İTFA 01012028")
    assert iso == "2028-01-01" and conf == 0.5 and method == "ddmmyyyy-multi"


def test_parse_maturity_none_and_implausible():
    assert parse_maturity("KİRA SERTİFİKASI") == (None, 0.0, "none")
    # 8 digits but not a valid date (month 99) -> ignored
    assert parse_maturity("CODE 99992027") == (None, 0.0, "none")


def test_parse_currency():
    assert parse_currency("USD EUROBOND", "XS") == "USD"
    assert parse_currency("EUR TAHVİL", "XS") == "EUR"
    # XS with no named currency → "FX" (known foreign-currency, USD/EUR
    # unresolved from this source), NOT None/UNKNOWN.
    assert parse_currency("EUROBOND no ccy named", "XS") == "FX"
    assert parse_currency("ÖZEL SEKTÖR TAHVİLİ", "TRS") == "TRY"  # TR default
    # an unrecognised non-debt class stays None (no FX assumption)
    assert parse_currency("SOMETHING", None) is None


# --- extraction ------------------------------------------------------------

def _rows():
    return [
        {"ISIN Kodu": BOND, "Kıymet Açıklama": "AK FAKTORİNG TAHVİLİ 25052027",
         "MKKÇ Adı": "AK FAKTORİNG"},
        {"ISIN Kodu": BILL, "Kıymet Açıklama": "FİNANSMAN BONOSU 10102026",
         "MKKÇ Adı": "ULUSAL FAKTORİNG"},
        {"ISIN Kodu": SUKUK, "Kıymet Açıklama": "KİRA SERTİFİKASI 01012030",
         "MKKÇ Adı": "DK VARLIK KİRALAMA"},
        {"ISIN Kodu": EUROBOND, "Kıymet Açıklama": "USD EUROBOND 15062029",
         "MKKÇ Adı": "AKBANK"},
        {"ISIN Kodu": "TRATUPRS91E8", "Kıymet Açıklama": "EQUITY", "MKKÇ Adı": "TÜPRAŞ"},
        {"ISIN Kodu": BAD, "Kıymet Açıklama": "TAHVİL 01012027", "MKKÇ Adı": "AK FAKTORİNG"},
    ]


def test_extract_debt_filters_validates_and_dedups():
    secs, rejected, skipped = extract_debt(
        _rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    isins = {s.isin for s in secs}
    assert isins == {BOND, BILL, SUKUK, EUROBOND}   # equity skipped, BAD rejected
    assert skipped == 1                              # the TRA equity line
    assert [r["isin"] for r in rejected] == [BAD]
    bond = next(s for s in secs if s.isin == BOND)
    assert bond.type == "BOND" and bond.maturity_date == "2027-05-25"
    euro = next(s for s in secs if s.isin == EUROBOND)
    assert euro.currency == "USD" and euro.type == "EUROBOND"


def test_extract_debt_class_filter():
    secs, _, _ = extract_debt(_rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı",
                              classes=("TRS",))
    assert {s.isin for s in secs} == {BOND}


# --- reference round-trip ---------------------------------------------------

def _ref_file(securities) -> Path:
    p = Path(tempfile.mkdtemp()) / "mkk_debt.json"
    write_reference(securities, p, source="test")
    return p


def test_reference_roundtrip_and_quarantine():
    secs, _, _ = extract_debt(_rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    p = _ref_file(secs)
    # inject a malformed ISIN into the file -> must be quarantined on load
    blob = json.loads(p.read_text())
    blob["securities"].append({"isin": BAD, "issuer_name": "X", "type": "BOND",
                               "instrument_class": "TRS"})
    p.write_text(json.dumps(blob), encoding="utf-8")
    ref = MkkDebtReference(reference_path=p).load()
    assert len(ref) == 4
    assert [r["isin"] for r in ref.rejected] == [BAD]
    grouped = ref.by_issuer()
    assert set(grouped) == {"AK FAKTORİNG", "ULUSAL FAKTORİNG",
                            "DK VARLIK KİRALAMA", "AKBANK"}


def test_reference_missing_file_is_empty():
    ref = MkkDebtReference(reference_path=Path(tempfile.mkdtemp()) / "nope.json").load()
    assert len(ref) == 0 and ref.by_issuer() == {}


# --- issuer matching -------------------------------------------------------

def _companies():
    return [
        {"uuid": "co-akf", "name": "AK FAKTORİNG ANONİM ŞİRKETİ", "ticker": None,
         "brand": _brand_set("AK FAKTORİNG ANONİM ŞİRKETİ")},
        {"uuid": "co-uls", "name": "ULUSAL FAKTORİNG A.Ş.", "ticker": None,
         "brand": _brand_set("ULUSAL FAKTORİNG A.Ş.")},
        {"uuid": "co-akb", "name": "AKBANK T.A.Ş.", "ticker": "AKBNK",
         "brand": _brand_set("AKBANK T.A.Ş.")},
    ]


def test_match_issuer_resolves_and_refuses():
    cs = _companies()
    assert match_issuer("AK FAKTORİNG", cs)["uuid"] == "co-akf"
    assert match_issuer("AKBANK", cs)["uuid"] == "co-akb"
    assert match_issuer("DK VARLIK KİRALAMA", cs) is None        # unlisted SPV
    # AK FAKTORİNG must NOT collide with AKBANK
    assert match_issuer("AK FAKTORİNG", cs)["uuid"] != "co-akb"


# --- precision hardening (matcher v2) --------------------------------------

def _nurol_group():
    """The three NUROL-group entities that share the 'NUROL' brand."""
    return [
        {"uuid": "nrbnk", "name": "NUROL YATIRIM BANKASI A.Ş.", "ticker": "NRBNK"},
        {"uuid": "nugyo", "name": "NUROL GAYRİMENKUL YATIRIM ORTAKLIĞI A.Ş.",
         "ticker": "NUGYO"},
        {"uuid": "nurvk", "name": "NUROL VARLIK KİRALAMA A.Ş.", "ticker": "NURVK"},
    ]


def test_entity_type_classifier():
    assert _entity_type("NUROL YATIRIM BANKASI A.Ş.") == "BANK"
    assert _entity_type("NUROL GAYRİMENKUL YATIRIM ORTAKLIĞI A.Ş.") == "REIT"
    assert _entity_type("AKTİF BANK SUKUK VARLIK KİRALAMA A.Ş.") == "ASSET_LEASING"
    assert _entity_type("DENİZ FİNANSAL KİRALAMA A.Ş.") == "LEASING"
    assert _entity_type("BULLS GİRİŞİM SERMAYESİ YATIRIM ORTAKLIĞI A.Ş.") == "VC"
    assert _entity_type("MERCEDES BENZ KAMYON FİNANSMAN A.Ş.") == "FINANCING"
    assert _entity_type("TÜPRAŞ-TÜRKİYE PETROL RAFİNERİLERİ A.Ş.") is None


def test_identity_tokens_drop_generic_and_short():
    # generic industry words + single chars are not identity-bearing
    assert _identity_tokens("İŞ BANKASI A") == {"IS"}
    assert _identity_tokens("INVEST AZ YATIRIM MENKUL DEĞERLER") == {"INVEST", "AZ"}
    assert "FAKTORING" not in _identity_tokens("ARENA FİNANS FAKTORİNG")


def test_entity_type_guard_reassigns_to_correct_group_member():
    # the v1 bug: 41 instruments landed on NUROL GYO. With the type guard the
    # bank issuer resolves to the bank, not the REIT or the SPV.
    m = match_issuer("NUROL YATIRIM BANK", _nurol_group())
    assert m is not None and m["uuid"] == "nrbnk"


def test_generic_token_collisions_are_rejected():
    cs = [
        {"uuid": "a1", "name": "A1 CAPİTAL YATIRIM MENKUL DEĞERLER A.Ş.",
         "ticker": "A1CAP"},
        {"uuid": "dstk", "name": "DESTEK FİNANS FAKTORİNG A.Ş.", "ticker": "DSTKF"},
        {"uuid": "afb", "name": "AKTİF YATIRIM BANKASI A.Ş.", "ticker": "AFB"},
    ]
    # only generic tokens (YATIRIM/MENKUL/DEĞERLER, FİNANS/FAKTORİNG, YATIRIM/
    # BANKASI) overlap — distinct brands → no match.
    assert match_issuer("INVEST AZ YATIRIM MENKUL DEĞERLER", cs) is None
    assert match_issuer("ARENA FİNANS FAKTORİNG", cs) is None
    assert match_issuer("DESTEK YATIRIM BANKASI", cs) is None


def test_partial_identity_below_threshold_rejected():
    cs = [{"uuid": "ekt", "name": "EMLAK KATILIM VARLIK KİRALAMA A.Ş.",
           "ticker": "EKTVK"}]
    # EMLAK overlaps but KONUT≠KATILIM → coverage 0.5 < 0.6 → reject
    m, reason, _ = match_issuer_detailed("EMLAK KONUT VARLIK", cs)
    assert m is None and reason in ("below-threshold", "entity-type-conflict")


def test_alias_recovers_brand_vs_legal_name_mismatch():
    cs = [
        {"uuid": "vakif", "name": "TÜRKİYE VAKIFLAR BANKASI T.A.O.", "ticker": "TVB"},
        {"uuid": "tt", "name": "TÜRK TELEKOMÜNİKASYON A.Ş.", "ticker": "TTKOM"},
    ]
    m1, _r, meth1 = match_issuer_detailed("VAKIFBANK", cs)
    assert m1 is not None and m1["uuid"] == "vakif" and meth1 == "alias"
    m2, _r2, meth2 = match_issuer_detailed("TÜRK TELEKOM", cs)
    assert m2 is not None and m2["uuid"] == "tt" and meth2 == "alias"


def test_shared_token_across_unrelated_kinds_rejected():
    # ZİRAAT BANKASI (state bank, unlisted) must NOT attach to TÜRK TRAKTÖR,
    # which merely contains 'ZİRAAT' (machinery). Bank issuer vs operating co.
    cs = [{"uuid": "ttrak",
           "name": "TÜRK TRAKTÖR VE ZİRAAT MAKİNELERİ A.Ş.", "ticker": "TTRAK"}]
    m, reason, _ = match_issuer_detailed("ZİRAAT BANKASI BANKA BONOSU", cs)
    assert m is None and reason == "entity-type-conflict"


# --- loader round-trip -----------------------------------------------------

def _fresh_db():
    db = Path(tempfile.mkdtemp()) / "tmkg.kuzu"
    conn = connect(db)
    apply_schema(conn)
    return conn


def _seed(conn, uuid, name, ticker=None):
    conn.execute("MERGE (c:Company {uuid:$u}) SET c.name=$n, c.ticker=$t, "
                 "c.is_listed=true", {"u": uuid, "n": name, "t": ticker})


def _seed_universe(conn):
    _seed(conn, "co-akf", "AK FAKTORİNG ANONİM ŞİRKETİ")
    _seed(conn, "co-uls", "ULUSAL FAKTORİNG A.Ş.")
    _seed(conn, "co-akb", "AKBANK T.A.Ş.", "AKBNK")


def test_backfill_listed_only_matches_and_skips():
    conn = _fresh_db()
    _seed_universe(conn)
    secs, _, _ = extract_debt(_rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    ref = MkkDebtReference(reference_path=_ref_file(secs))
    report = Path(tempfile.mkdtemp()) / "d.json"
    stats = backfill_debt(conn, ref, report_path=report)
    assert stats["issuers_matched"] == 3 and stats["issuers_unmatched"] == 1
    assert stats["securities_written"] == 3        # DK VARLIK skipped (unlisted)
    # the eurobond Security + ISSUES edge landed on AKBANK with a maturity DATE
    r = conn.execute(
        "MATCH (c:Company {uuid:'co-akb'})-[e:ISSUES]->(s:Security) "
        "RETURN s.isin, s.type, s.maturity_date, e.instrument_class, e.source")
    isin, typ, mat, cls, src = r.get_next()
    assert isin == EUROBOND and typ == "EUROBOND" and cls == "XS"
    assert str(mat) == "2029-06-15" and "debt" in src
    blob = json.loads(report.read_text())
    assert [u["issuer_name"] for u in blob["unmatched"]] == ["DK VARLIK KİRALAMA"]


def test_backfill_is_idempotent():
    conn = _fresh_db()
    _seed_universe(conn)
    secs, _, _ = extract_debt(_rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    ref = MkkDebtReference(reference_path=_ref_file(secs))
    backfill_debt(conn, ref, report_path=Path(tempfile.mkdtemp()) / "a.json")
    backfill_debt(conn, ref, report_path=Path(tempfile.mkdtemp()) / "b.json")
    n_sec = conn.execute("MATCH (s:Security) RETURN count(s)").get_next()[0]
    n_edge = conn.execute("MATCH ()-[e:ISSUES]->() RETURN count(e)").get_next()[0]
    assert n_sec == 3 and n_edge == 3   # no duplication on re-run


def test_backfill_create_missing_issuers():
    conn = _fresh_db()
    _seed_universe(conn)
    secs, _, _ = extract_debt(_rows(), "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    ref = MkkDebtReference(reference_path=_ref_file(secs))
    stats = backfill_debt(conn, ref, create_missing_issuers=True,
                          report_path=Path(tempfile.mkdtemp()) / "c.json")
    assert stats["issuers_matched"] == 4 and stats["securities_written"] == 4
    # an issuer node was created for the unlisted SPV
    n = conn.execute("MATCH (c:Company) WHERE c.name='DK VARLIK KİRALAMA' "
                     "RETURN count(c)").get_next()[0]
    assert n == 1


# --- Phase 2.2: unmatched split (sovereign blocklist + brand-stub attach) ---

def _ds(isin, issuer):
    """A minimal valid DebtSecurity for loader tests (matching is by issuer)."""
    return DebtSecurity(
        isin=isin, instrument_class="TRS", type="BOND", issuer_name=issuer,
        description="TAHVİL 25052027", currency="TRY",
        maturity_date="2027-05-25", maturity_confidence=0.9,
        maturity_method="ddmmyyyy-single")


def _ref_from(pairs) -> MkkDebtReference:
    p = Path(tempfile.mkdtemp()) / "mkk_debt.json"
    write_reference([_ds(i, n) for i, n in pairs], p, source="test")
    return MkkDebtReference(reference_path=p)


def _seed_stub(conn, brand):
    conn.execute(
        "CREATE (:Company {uuid:$u, name:$n, listing_status:'EXTERNAL_STUB', "
        "is_listed:false})",
        {"u": "stub-" + brand, "n": f"{brand} (external stub parent)"})


def test_sovereign_dibs_excluded_not_unmatched():
    conn = _fresh_db()
    _seed_universe(conn)
    ref = _ref_from([(BOND, "AK FAKTORİNG"),
                     (BILL, "DİBS OPERASYONEL ÜYE"),
                     (SUKUK, "DİBS OPERASYONEL ÜYE")])
    report = Path(tempfile.mkdtemp()) / "d.json"
    stats = backfill_debt(conn, ref, report_path=report)
    # the sovereign book is a policy exclusion, NOT an unmatched failure
    assert stats["issuers_sovereign_excluded"] == 1
    assert stats["securities_sovereign_excluded"] == 2
    assert stats["issuers_unmatched"] == 0
    assert stats["securities_written"] == 1            # only AK FAKTORİNG's bond
    # nothing written for DİBS, and the denominator excludes the sovereign book
    assert stats["in_scope_reference"] == 1
    blob = json.loads(report.read_text())
    assert blob["sovereign_excluded"][0]["reason"] == "sovereign-out-of-scope"


def test_unmatched_attaches_to_brand_stub():
    conn = _fresh_db()
    _seed_universe(conn)
    _seed_stub(conn, "DENIZ")
    ref = _ref_from([(BOND, "DENİZBANK"), (BILL, "DENİZBANK")])
    stats = backfill_debt(conn, ref, report_path=Path(tempfile.mkdtemp()) / "d.json")
    assert stats["issuers_attached_to_stub"] == 1
    assert stats["issuers_unmatched"] == 0
    assert stats["securities_written"] == 2
    # debt landed on the brand stub (group converges on one node)
    n = conn.execute(
        "MATCH (:Company {uuid:'stub-DENIZ'})-[:ISSUES]->(s:Security) "
        "RETURN count(s)").get_next()[0]
    assert n == 2


def test_debt_matches_real_external_parent_but_not_stub_fuzzily():
    # EXTERNAL_PARENT (real GLEIF entity, 0.95) is a valid fuzzy match target;
    # EXTERNAL_STUB (inferred, 0.70) must attach only via the curated map, never
    # by fuzzy brand overlap.
    conn = _fresh_db()
    conn.execute(
        "CREATE (:Company {uuid:'ext-Z', "
        "name:'TÜRKİYE CUMHURİYETİ ZİRAAT BANKASI ANONİM ŞİRKETİ', "
        "listing_status:'EXTERNAL_PARENT', is_listed:false})")
    _seed_stub(conn, "GARANTI")
    ref = _ref_from([(BOND, "ZİRAAT BANKASI BANKA BONOSU"),
                     (BILL, "GARANTİ FİNANSAL KİRALAMA")])
    stats = backfill_debt(conn, ref, report_path=Path(tempfile.mkdtemp()) / "d.json")
    # Ziraat bond resolves to the real external legal entity
    assert conn.execute(
        "MATCH (:Company {uuid:'ext-Z'})-[:ISSUES]->(s) RETURN count(s)"
    ).get_next()[0] == 1
    # the GARANTİ leasing issuer (not in the curated map) is NOT fuzzy-attached
    assert conn.execute(
        "MATCH (:Company {uuid:'stub-GARANTI'})-[:ISSUES]->() RETURN count(*)"
    ).get_next()[0] == 0
    assert stats["issuers_attached_to_stub"] == 0


def test_stub_attach_degrades_to_unmatched_without_stub():
    # same issuer, but no stub node present -> logged unmatched, no dangling edge
    conn = _fresh_db()
    _seed_universe(conn)
    ref = _ref_from([(BOND, "DENİZBANK")])
    stats = backfill_debt(conn, ref, report_path=Path(tempfile.mkdtemp()) / "d.json")
    assert stats["issuers_attached_to_stub"] == 0
    assert stats["issuers_unmatched"] == 1
    assert stats["securities_written"] == 0


def test_backfill_logs_low_confidence_maturities():
    conn = _fresh_db()
    _seed_universe(conn)
    # BILL desc has two dates -> low-confidence maturity; bond has one -> high
    rows = [
        {"ISIN Kodu": BOND, "Kıymet Açıklama": "TAHVİL 25052027", "MKKÇ Adı": "AK FAKTORİNG"},
        {"ISIN Kodu": BILL, "Kıymet Açıklama": "BONO 01012025 İTFA 01012026",
         "MKKÇ Adı": "ULUSAL FAKTORİNG"},
    ]
    secs, _, _ = extract_debt(rows, "ISIN Kodu", "Kıymet Açıklama", "MKKÇ Adı")
    ref = MkkDebtReference(reference_path=_ref_file(secs))
    report = Path(tempfile.mkdtemp()) / "d.json"
    stats = backfill_debt(conn, ref, report_path=report)
    assert stats["low_confidence_maturities"] == 1
    blob = json.loads(report.read_text())
    assert blob["low_confidence_maturities"][0]["isin"] == BILL

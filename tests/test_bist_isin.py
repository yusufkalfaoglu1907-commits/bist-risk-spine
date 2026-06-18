"""BİST/MKK ticker→ISIN reference adapter + back-fill tests.

Fully offline: the adapter reads a committed reference file and the loader
writes into a temp Kuzu DB. No network. Run:

    PYTHONPATH=src python -m pytest tests/test_bist_isin.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tmkg.adapters.bist_isin_adapter import (
    BistIsinAdapter, is_valid_isin, is_equity_isin, isin_check_digit_ok,
)
from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders.bist_isin_backfill import (
    backfill_isins_from_bist, classify_listing_status,
    EQUITY_TRADED, NON_EQUITY_ISSUER,
)


# --- ISIN validation (the data-quality gate) -------------------------------

def test_check_digit_valid_real_isins():
    for isin in ("TRATUPRS91E8", "TRATHYAO91M5", "TRAGARAN91N1", "TREACSS00017"):
        assert isin_check_digit_ok(isin), isin
        assert is_valid_isin(isin)
        assert is_equity_isin(isin)


def test_check_digit_rejects_corruption():
    # flip the check digit -> must be rejected
    assert not isin_check_digit_ok("TRATUPRS91E9")
    assert not is_valid_isin("TRATUPRS91E9")


def test_validation_rejects_bad_shape():
    assert not is_valid_isin("US0378331005")     # not Turkish
    assert not is_valid_isin("TRATUPRS91E")      # too short
    assert not is_valid_isin("")
    assert not is_valid_isin(None)


def test_non_equity_class_isin_not_equity():
    # a structurally valid TR ISIN that is not TRA/TRE equity class
    # (TRD... debt-style prefix) is valid-but-not-equity
    assert is_equity_isin("TREACSS00017")
    assert not is_equity_isin("US0378331005")


# --- adapter ---------------------------------------------------------------

def _ref_file(mappings: dict, ambiguous: dict | None = None, **meta) -> Path:
    p = Path(tempfile.mkdtemp()) / "bist_isin.json"
    p.write_text(json.dumps({
        "source": meta.get("source", "test"),
        "fetched_iso": "2026-06-06",
        "mappings": mappings,
        "ambiguous": ambiguous or {},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_adapter_loads_and_quarantines_invalid():
    p = _ref_file({"TUPRS": "TRATUPRS91E8", "BAD": "TRATUPRS91E9", "THYAO": "TRATHYAO91M5"})
    a = BistIsinAdapter(reference_path=p).load()
    assert len(a) == 2                      # invalid one quarantined
    assert a.lookup("TUPRS").isin == "TRATUPRS91E8"
    assert a.lookup("BAD").found and not a.lookup("BAD").valid
    assert a.rejected == {"BAD": "TRATUPRS91E9"}


def test_adapter_missing_file_is_empty():
    a = BistIsinAdapter(reference_path=Path(tempfile.mkdtemp()) / "nope.json").load()
    assert len(a) == 0
    assert a.lookup("TUPRS").note == "not-in-reference"


def test_adapter_case_insensitive_ticker():
    p = _ref_file({"TUPRS": "TRATUPRS91E8"})
    a = BistIsinAdapter(reference_path=p).load()
    assert a.lookup("tuprs").isin == "TRATUPRS91E8"


def test_smoke_check_anchor_drift_raises():
    p = _ref_file({"TUPRS": "TRATHYAO91M5"})  # wrong ISIN for TUPRS (valid shape)
    a = BistIsinAdapter(reference_path=p)
    try:
        a.smoke_check()
    except AssertionError as e:
        assert "anchor drift" in str(e)
    else:
        raise AssertionError("expected anchor-drift assertion")


def test_shipped_reference_file_is_valid():
    """The committed seed file must always pass structural validation."""
    a = BistIsinAdapter()  # default shipped path
    res = a.smoke_check()
    assert res["rejected"] == 0
    assert res["tickers"] >= 1


# --- loader round-trips ----------------------------------------------------

def _seed_company(conn, uuid, ticker, lei=None, isin=None, with_security=True):
    conn.execute(
        "MERGE (c:Company {uuid:$u}) SET c.ticker=$t, c.is_listed=true, "
        "c.lei=$l, c.isin=$i",
        {"u": uuid, "t": ticker, "l": lei, "i": isin},
    )
    if with_security:
        sid = "se-" + uuid
        conn.execute("MERGE (s:Security {uuid:$s}) SET s.ticker=$t, s.type='EQUITY'",
                     {"s": sid, "t": ticker})
        conn.execute("MATCH (c:Company {uuid:$u}),(s:Security {uuid:$s}) "
                     "MERGE (c)-[:ISSUES]->(s)", {"u": uuid, "s": sid})


def _fresh_db():
    db = Path(tempfile.mkdtemp()) / "tmkg.kuzu"
    conn = connect(db)
    apply_schema(conn)
    return conn


# --- F9 / 2.3: ambiguous-ticker disambiguation -----------------------------

def _dis_file(rows: dict) -> Path:
    p = Path(tempfile.mkdtemp()) / "isin_disambiguation.json"
    p.write_text(json.dumps({"schema_version": 1, "disambiguations": rows},
                            ensure_ascii=False), encoding="utf-8")
    return p


# EKGYO's two ambiguous candidates (real values from bist_isin.json).
_EKGYO_CANDS = ["TREEGYO00017", "TREEGYO00025"]


def _ekgyo_adapter(dis_rows, mappings=None):
    ref = _ref_file(mappings or {}, ambiguous={"EKGYO": _EKGYO_CANDS})
    return BistIsinAdapter(reference_path=ref, disambiguation_path=_dis_file(dis_rows))


def test_confirmed_disambiguation_is_written():
    conn = _fresh_db()
    _seed_company(conn, "co-ekgyo", "EKGYO")   # no ISIN, ambiguous ticker
    a = _ekgyo_adapter({"EKGYO": {"candidates": _EKGYO_CANDS,
                                  "chosen": "TREEGYO00017", "confirmed": True}})
    stats = backfill_isins_from_bist(conn, a, cross_validate=False,
                                     report_path=Path(tempfile.mkdtemp()) / "r.json")
    assert stats["disambiguated"] == 1 and stats["isins_written"] == 1
    co = conn.execute("MATCH (c:Company {uuid:'co-ekgyo'}) RETURN c.isin").get_next()[0]
    assert co == "TREEGYO00017"


def test_unconfirmed_disambiguation_is_not_written():
    conn = _fresh_db()
    _seed_company(conn, "co-ekgyo", "EKGYO")
    a = _ekgyo_adapter({"EKGYO": {"candidates": _EKGYO_CANDS,
                                  "chosen": "TREEGYO00017", "confirmed": False}})
    stats = backfill_isins_from_bist(conn, a, cross_validate=False,
                                     report_path=Path(tempfile.mkdtemp()) / "r.json")
    assert stats["disambiguated"] == 0
    assert a.disambiguation_skipped == {"EKGYO": "unconfirmed"}
    co = conn.execute("MATCH (c:Company {uuid:'co-ekgyo'}) RETURN c.isin").get_next()[0]
    assert co is None


def test_chosen_not_a_candidate_is_rejected():
    a = _ekgyo_adapter({"EKGYO": {"candidates": _EKGYO_CANDS,
                                  "chosen": "TRATUPRS91E8",  # valid ISIN, wrong line
                                  "confirmed": True}}).load()
    assert a.disambiguated("EKGYO") is None
    assert a.disambiguation_skipped == {"EKGYO": "chosen-not-a-candidate"}


def test_disambiguation_conflicting_with_gleif_is_logged_not_written(tmp_path):
    conn = _fresh_db()
    _seed_company(conn, "co-ekgyo", "EKGYO", lei="LEI-EKGYO")
    # GLEIF surfaced a DIFFERENT equity candidate for this LEI -> disagreement.
    (tmp_path / "gleif_isins.json").write_text(json.dumps({"isins": {
        "LEI-EKGYO": {"isin": None, "candidates": ["TREEGYO00025"]}}}),
        encoding="utf-8")
    a = _ekgyo_adapter({"EKGYO": {"candidates": _EKGYO_CANDS,
                                  "chosen": "TREEGYO00017", "confirmed": True}})
    report = Path(tempfile.mkdtemp()) / "r.json"
    stats = backfill_isins_from_bist(conn, a, cross_validate=True,
                                     cache_dir=tmp_path, report_path=report)
    assert stats["disambiguated"] == 0 and stats["conflicts"] == 1
    co = conn.execute("MATCH (c:Company {uuid:'co-ekgyo'}) RETURN c.isin").get_next()[0]
    assert co is None
    blob = json.loads(report.read_text())
    assert blob["results"][0]["method"] == "disambiguation-conflict-gleif"


def test_shipped_disambiguation_file_is_confirmed_and_valid():
    """The committed 11-row file is web-verified + confirmed. Every honored pick
    must be a valid equity ISIN among its own candidates (strict load enforces
    this), and none may be skipped."""
    a = BistIsinAdapter().load(strict=True)   # default shipped paths; raises on a bad pick
    assert a.disambiguation_skipped == {}
    # all 11 resolve, EKGYO/ISDMR among them; ISDMR took the higher candidate
    assert a.disambiguated("EKGYO") == "TREEGYO00017"
    assert a.disambiguated("ISDMR") == "TREISDC00020"
    assert is_equity_isin(a.disambiguated("MMCAS"))


def test_backfill_writes_authoritative_when_no_gleif_candidates():
    conn = _fresh_db()
    _seed_company(conn, "co-thy", "THYAO")  # no LEI, no ISIN
    ref = _ref_file({"THYAO": "TRATHYAO91M5"})
    report = Path(tempfile.mkdtemp()) / "r.json"
    stats = backfill_isins_from_bist(
        conn, BistIsinAdapter(reference_path=ref),
        cross_validate=False, report_path=report,
    )
    assert stats["isins_written"] == 1, stats
    co = conn.execute("MATCH (c:Company {uuid:'co-thy'}) RETURN c.isin").get_next()[0]
    se = conn.execute("MATCH (s:Security {uuid:'se-co-thy'}) RETURN s.isin").get_next()[0]
    assert co == "TRATHYAO91M5" and se == "TRATHYAO91M5"


def test_backfill_agrees_with_gleif_candidates():
    conn = _fresh_db()
    _seed_company(conn, "co-a", "AAGYO", lei="LEI0000000000000AAGY0")
    ref = _ref_file({"AAGYO": "TREAGVR00014"})
    cache = Path(tempfile.mkdtemp())
    # GLEIF left it ambiguous between two equity share lines; BİST picks one of them
    (cache / "gleif_isins.json").write_text(json.dumps({"isins": {
        "LEI0000000000000AAGY0": {"isin": None, "method": "ambiguous-multi-equity",
                                  "candidates": ["TREAGVR00014", "TREAGVR00022"]}
    }}), encoding="utf-8")
    report = Path(tempfile.mkdtemp()) / "r.json"
    stats = backfill_isins_from_bist(
        conn, BistIsinAdapter(reference_path=ref),
        cross_validate=True, cache_dir=cache, report_path=report,
    )
    assert stats["isins_written"] == 1, stats
    blob = json.loads(report.read_text())
    assert blob["results"][0]["method"] == "bist+gleif-agree"


def test_backfill_flags_conflict_and_does_not_write():
    conn = _fresh_db()
    _seed_company(conn, "co-c", "AAGYO", lei="LEI0000000000000AAGY0")
    ref = _ref_file({"AAGYO": "TRATHYAO91M5"})   # valid ISIN but NOT in GLEIF's set
    cache = Path(tempfile.mkdtemp())
    (cache / "gleif_isins.json").write_text(json.dumps({"isins": {
        "LEI0000000000000AAGY0": {"isin": None, "method": "ambiguous-multi-equity",
                                  "candidates": ["TREAGVR00014", "TREAGVR00022"]}
    }}), encoding="utf-8")
    report = Path(tempfile.mkdtemp()) / "r.json"
    stats = backfill_isins_from_bist(
        conn, BistIsinAdapter(reference_path=ref),
        cross_validate=True, cache_dir=cache, report_path=report,
    )
    assert stats["isins_written"] == 0 and stats["conflicts"] == 1, stats
    co = conn.execute("MATCH (c:Company {uuid:'co-c'}) RETURN c.isin").get_next()[0]
    assert co is None  # conflict -> not written


def test_is_equity_traded_includes_ambiguous_not_debt():
    ref = _ref_file({"THYAO": "TRATHYAO91M5"}, ambiguous={"PRMS": ["TREPRMS00016", "TREPRMS00024"]})
    a = BistIsinAdapter(reference_path=ref).load()
    assert a.is_equity_traded("THYAO")        # resolved equity
    assert a.is_equity_traded("PRMS")          # multi-group, still equity
    assert not a.is_equity_traded("AKFK")      # debt-only issuer absent from both


def test_classify_listing_status_splits_traded_and_debt_issuers():
    conn = _fresh_db()
    _seed_company(conn, "co-thy", "THYAO")                      # traded (in mappings)
    _seed_company(conn, "co-prms", "PRMS", with_security=False)  # traded (ambiguous)
    _seed_company(conn, "co-akfk", "AKFK", with_security=False)  # debt-only issuer
    _seed_company(conn, "co-tup", "TUPRS", isin="TRATUPRS91E8")  # already has equity ISIN
    ref = _ref_file({"THYAO": "TRATHYAO91M5"},
                    ambiguous={"PRMS": ["TREPRMS00016", "TREPRMS00024"]})
    report = Path(tempfile.mkdtemp()) / "cls.json"
    stats = classify_listing_status(conn, BistIsinAdapter(reference_path=ref),
                                    report_path=report)
    assert stats["EQUITY_TRADED"] == 3 and stats["NON_EQUITY_ISSUER"] == 1, stats
    got = {}
    r = conn.execute("MATCH (c:Company) RETURN c.ticker, c.listing_status")
    while r.has_next():
        tk, st = r.get_next(); got[tk] = st
    assert got["THYAO"] == EQUITY_TRADED
    assert got["PRMS"] == EQUITY_TRADED
    assert got["TUPRS"] == EQUITY_TRADED      # via ISIN already on node
    assert got["AKFK"] == NON_EQUITY_ISSUER
    blob = json.loads(report.read_text())
    assert [x["ticker"] for x in blob["non_equity_issuers"]] == ["AKFK"]


def test_backfill_skips_unknown_and_only_missing():
    conn = _fresh_db()
    _seed_company(conn, "co-known", "THYAO")
    _seed_company(conn, "co-unknown", "ZZZZZ")
    _seed_company(conn, "co-haveisin", "TUPRS", isin="TRATUPRS91E8")  # already has ISIN
    ref = _ref_file({"THYAO": "TRATHYAO91M5", "TUPRS": "TRATUPRS91E8"})
    report = Path(tempfile.mkdtemp()) / "r.json"
    stats = backfill_isins_from_bist(
        conn, BistIsinAdapter(reference_path=ref),
        only_missing=True, cross_validate=False, report_path=report,
    )
    # only co-known and co-unknown are targets (co-haveisin excluded by only_missing)
    assert stats["targets"] == 2, stats
    assert stats["isins_written"] == 1
    assert stats["not_in_reference"] == 1

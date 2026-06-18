"""GLEIF back-fill tests.

Two layers:
  - OFFLINE unit tests for the name matcher (normalization, brand-token
    extraction, query-variant generation). No network — always run.
  - LIVE drift guard that hits api.gleif.org and a full back-fill round-trip
    into a temp Kuzu DB. These SKIP automatically when GLEIF is unreachable, so
    the offline suite still passes in CI.

    PYTHONPATH=src python -m pytest tests/test_gleif.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tmkg.adapters.gleif_adapter import (
    GleifAdapter, query_core, query_variants, _brand_tokens,
    _ascii_fold, _norm_for_score, pick_equity_isin,
)


# --- offline unit tests ----------------------------------------------------

def test_ascii_fold_turkish():
    assert _ascii_fold("KOÇ HOLDİNG ŞİŞE ÜÇ ĞÖ") == "KOC HOLDING SISE UC GO"


def test_norm_for_score_strips_punct_and_folds():
    assert _norm_for_score("KOÇ HOLDİNG A.Ş.") == "KOC HOLDING A S"


def test_brand_tokens_strips_legal_suffix():
    toks = _brand_tokens("ACISELSAN ACIPAYAM SELÜLOZ SANAYİ VE TİCARET A.Ş.")
    # trailing "SANAYİ VE TİCARET A.Ş." dropped, brand kept
    assert toks[:3] == ["ACISELSAN", "ACIPAYAM", "SELÜLOZ"]
    assert "A.Ş." not in toks and "TİCARET" not in toks


def test_brand_tokens_drops_leading_geographic_prefix():
    # "TÜRKİYE" is non-distinctive and diacritically unstable in GLEIF data
    assert _brand_tokens("TÜRKİYE GARANTİ BANKASI A.Ş.")[:2] == ["GARANTİ", "BANKASI"]


def test_brand_tokens_never_empty():
    # always keep at least the distinctive brand token, never strip to nothing
    assert _brand_tokens("AKSİGORTA A.Ş.") == ["AKSİGORTA"]
    assert len(_brand_tokens("SANAYİ VE TİCARET A.Ş.")) >= 1


def test_query_variants_include_ascii_fallback():
    vs = query_variants("TÜRKİYE GARANTİ BANKASI A.Ş.")
    assert vs[0] == "GARANTİ BANKASI"          # diacritic, 2 brand tokens first
    assert any(_ascii_fold(v) == v and "GARANTI" in v for v in vs)  # ascii fallback present


def test_query_core_preserves_diacritics():
    assert query_core("KOÇ HOLDİNG A.Ş.") == "KOÇ HOLDİNG"


def test_pick_equity_isin_prefers_tra_ticker():
    # Garanti-style: many warrants (TRWGRAN, abbrev) + one equity (TRA+full ticker)
    isins = ["TRWGRAN05186", "TRWGRAN02282", "TRAGARAN91N1", "TRWGRAN06317"]
    assert pick_equity_isin(isins, "GARAN") == ("TRAGARAN91N1", "TRA+ticker")


def test_pick_equity_isin_prefers_ticker_over_other_equity():
    # TUPRS: TRA+ticker wins even though a TRE equity-class ISIN is also present
    isins = ["TRETPRS00011", "TRSTPRS72614", "TRATUPRS91E8", "TRSTPRS82613"]
    assert pick_equity_isin(isins, "TUPRS") == ("TRATUPRS91E8", "TRA+ticker")


def test_pick_equity_isin_single_equity_class():
    # newer TRE-coded issuer whose abbreviated code != ticker (e.g. ACSEL)
    assert pick_equity_isin(["TREACSS00017"], "ACSEL") == ("TREACSS00017", "single-equity")


def test_pick_equity_isin_refuses_when_ambiguous_or_no_equity():
    # several equity-class share lines, no type field to choose -> refuse
    assert pick_equity_isin(["TREAGVR00014", "TREAGVR00022"], "AAGYO") == (
        None, "ambiguous-multi-equity")
    # only debt/financing instruments -> refuse (TRF is not equity)
    assert pick_equity_isin(["TRFADLVE2612"], "ADLVY") == (None, "no-equity-class")
    assert pick_equity_isin([], "XYZ") == (None, "none")


# --- live drift guard ------------------------------------------------------

def _adapter_or_skip():
    try:
        import httpx
        httpx.get("https://api.gleif.org/api/v1/lei-records?page[size]=1", timeout=8)
    except Exception:
        pytest.skip("GLEIF unreachable — skipping live tests")
    return GleifAdapter(cache_dir=tempfile.mkdtemp())


def test_smoke_check_passes():
    with _adapter_or_skip() as a:
        r = a.smoke_check()
        assert r["tupras_lei"].endswith("EE03") or len(r["tupras_lei"]) == 20


def test_match_known_issuers():
    cases = {
        "KOÇ HOLDİNG A.Ş.": "7890005U0H950VH19H45",
        "TÜRKİYE GARANTİ BANKASI A.Ş.": "5493002XSS7K7RHN1V37",  # ASCII-folded record
        "ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş.": "7890008XT4M710MU8714",
    }
    with _adapter_or_skip() as a:
        for name, lei in cases.items():
            m = a.match_company(name, use_cache=False)
            assert m.matched, f"{name} did not match (score={m.score})"
            assert m.lei == lei, f"{name} -> {m.lei}, expected {lei}"


def test_backfill_round_trip_writes_lei():
    """End-to-end: seed two Company nodes, back-fill, confirm LEI written + report."""
    a = _adapter_or_skip()
    from tmkg.graph.connection import connect
    from tmkg.schema.ddl import apply_schema
    from tmkg.loaders.gleif_backfill import backfill_leis

    db = Path(tempfile.mkdtemp()) / "tmkg.kuzu"
    conn = connect(db)
    apply_schema(conn)
    for uuid, name, tk in [
        ("co-test-asels", "ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş.", "ASELS"),
        ("co-test-eregl", "EREĞLİ DEMİR VE ÇELİK FABRİKALARI T.A.Ş.", "EREGL"),
    ]:
        conn.execute(
            "MERGE (c:Company {uuid:$u}) SET c.name=$n, c.ticker=$t, c.is_listed=true",
            {"u": uuid, "n": name, "t": tk},
        )
    report = Path(tempfile.mkdtemp()) / "report.json"
    with a:
        stats = backfill_leis(conn, a, report_path=report)
    assert stats["leis_written"] == 2, stats
    assert report.exists()
    res = conn.execute(
        "MATCH (c:Company {uuid:'co-test-asels'}) RETURN c.lei, c.legal_form")
    lei, lf = res.get_next()
    assert lei == "7890008XT4M710MU8714"
    assert lf  # ELF code populated


def test_fetch_primary_isin_live():
    with _adapter_or_skip() as a:
        r = a.fetch_primary_isin("789000RCNG97UV50EE03", ticker="TUPRS", use_cache=False)
        assert r.isin == "TRATUPRS91E8"
        assert r.method == "TRA+ticker"


def test_isin_backfill_round_trip_writes_company_and_security():
    """End-to-end: company with an LEI + issued EQUITY Security -> both get the ISIN."""
    a = _adapter_or_skip()
    from tmkg.graph.connection import connect
    from tmkg.schema.ddl import apply_schema
    from tmkg.loaders.gleif_backfill import backfill_isins

    db = Path(tempfile.mkdtemp()) / "tmkg.kuzu"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "MERGE (c:Company {uuid:'co-x'}) "
        "SET c.name='TÜRK HAVA YOLLARI A.O.', c.ticker='THYAO', "
        "c.is_listed=true, c.lei='789000EV8M3BL7ZPFB03'"
    )
    conn.execute("MERGE (s:Security {uuid:'se-x'}) SET s.ticker='THYAO', s.type='EQUITY'")
    conn.execute("MATCH (c:Company {uuid:'co-x'}),(s:Security {uuid:'se-x'}) "
                 "MERGE (c)-[:ISSUES]->(s)")

    report = Path(tempfile.mkdtemp()) / "isin.json"
    with a:
        stats = backfill_isins(conn, a, report_path=report)
    assert stats["isins_written"] == 1, stats
    assert report.exists()
    co_isin = conn.execute("MATCH (c:Company {uuid:'co-x'}) RETURN c.isin").get_next()[0]
    se_isin = conn.execute("MATCH (s:Security {uuid:'se-x'}) RETURN s.isin").get_next()[0]
    assert co_isin == "TRATHYAO91M5"
    assert se_isin == "TRATHYAO91M5"  # ISIN propagated to the issued equity

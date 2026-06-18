"""Offline tests for KAP subsidiary/affiliate discovery (extractor + loader).

The synthetic form reproduces the real "Şirket Genel Bilgi Formu" layout: the
related-party table follows the "Şirket ile Olan İlişkinin Niteliği" header, each
row is  NAME … <capital> <share> <CCY> <pct> <relationship>, the table renders
twice, and a free-text footnote (no legal suffix) sits among the rows.

Precision traps exercised:
  - generic child name ("Enerji Yatırımları A.Ş.") must NOT collapse onto an
    unrelated listed entity ("Metgün Enerji Yatırımları A.Ş." / METEN);
  - Turkish vowel-harmony morphology ("Makinaları" vs graph "Makineleri") must
    still align (common-prefix token match).

    PYTHONPATH=src python -m pytest tests/test_kap_subsidiary.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.adapters.kap_subsidiary_adapter import extract_subsidiaries, parse_pct
from tmkg.loaders.kap_subsidiary_backfill import backfill_subsidiaries

# Cleaned-text form: header … anchor … rows (Arçelik row duplicated = double
# render; a footnote without a legal suffix; one generic-name trap row).
FORM = (
    "Bağlı Ortaklıklar, Finansal Duran Varlıklar ile Finansal Yatırımlar "
    "Ticaret Unvanı Şirketin Faaliyet Konusu Ödenmiş/Çıkarılmış Sermayesi "
    "Şirketin Sermayedeki Payı Para Birimi Şirketin Sermayedeki Payı (%) "
    "Şirket ile Olan İlişkinin Niteliği "
    "Arçelik A.Ş. Üretim 675728205 327928625 TRY 48,53 Bağlı Ortaklık "
    "Ford Otomotiv Sanayi A.Ş. Üretim 3509100000 1356313045 TRY 38,65 İş Ortaklığı "
    "Türk Traktör ve Ziraat Makinaları A.Ş. Üretim 53369000 20013375 TRY 37,50 İş Ortaklığı "
    "Enerji Yatırımları A.Ş. Yatırım 1000000 770000 TRY 77,00 Bağlı Ortaklık "
    "Tabloda yalnızca doğrudan pay gösterilmiştir 0 0 TRY 30,00 Finansal Yatırım "
    # second render of the same table (dedup target):
    "Arçelik A.Ş. Üretim 675728205 327928625 TRY 48,53 Bağlı Ortaklık "
)


def _relations(parent="KCHOL"):
    recs = extract_subsidiaries(FORM, source="KAP:T")["records"]
    return [{**r, "parent_ticker": parent, "parent_name": "Koç Holding A.Ş.",
             "as_of": "2025-12-24"} for r in recs]


def _graph():
    conn = connect(Path(tempfile.mkdtemp()) / "sub.kuzu")
    apply_schema(conn)
    rows = [
        ("c-kchol", "KCHOL", "KOÇ HOLDİNG A.Ş."),
        ("c-arclk", "ARCLK", "ARÇELİK A.Ş."),
        ("c-froto", "FROTO", "FORD OTOMOTİV SANAYİ A.Ş."),
        ("c-ttrak", "TTRAK", "TÜRK TRAKTÖR VE ZİRAAT MAKİNELERİ A.Ş."),
        ("c-meten", "METEN", "METGÜN ENERJİ YATIRIMLARI A.Ş."),
    ]
    for u, t, n in rows:
        conn.execute("CREATE (:Company {uuid:$u, ticker:$t, name:$n, is_listed:true})",
                     {"u": u, "t": t, "n": n})
    return conn


def test_parse_pct():
    assert parse_pct("48,53") == 48.53
    assert parse_pct("70") == 70.0
    assert parse_pct("100,00") == 100.0
    assert parse_pct("") is None


def test_extractor_rows_relations_dedup_and_footnote():
    out = extract_subsidiaries(FORM, source="KAP:T")
    by_name = {r["child_name"]: r for r in out["records"]}
    # 4 distinct rows (Arçelik deduped despite double render); footnote dropped
    assert set(by_name) == {
        "Arçelik A.Ş.", "Ford Otomotiv Sanayi A.Ş.",
        "Türk Traktör ve Ziraat Makinaları A.Ş.", "Enerji Yatırımları A.Ş.",
    }
    assert by_name["Arçelik A.Ş."]["relation_kind"] == "subsidiary"
    assert by_name["Arçelik A.Ş."]["pct"] == 48.53
    assert by_name["Ford Otomotiv Sanayi A.Ş."]["relation_kind"] == "jv"


def test_loader_controls_for_subsidiary_only():
    conn = _graph()
    stats = backfill_subsidiaries(conn, _relations(), report_path=Path(tempfile.mkdtemp()) / "sub.json")
    # Arçelik = Bağlı Ortaklık -> CONTROLS; Ford & Türk Traktör = JV -> stake only
    assert stats["controls_new"] == 1
    assert stats["controls_corroborated"] == 0
    assert stats["holds_stake_new"] == 3      # ARCLK + FROTO + TTRAK percentages
    # CONTROLS lands on the subsidiary, not the JV
    res = conn.execute(
        "MATCH (:Company {ticker:'KCHOL'})-[r:CONTROLS]->(c:Company) RETURN c.ticker")
    controlled = set()
    while res.has_next():
        controlled.add(res.get_next()[0])
    assert controlled == {"ARCLK"}
    # SUBSIDIARY_OF mirrors it
    s = conn.execute(
        "MATCH (:Company {ticker:'ARCLK'})-[:SUBSIDIARY_OF]->(p:Company) RETURN p.ticker"
    ).get_next()[0]
    assert s == "KCHOL"


def test_loader_rejects_generic_name_false_positive():
    conn = _graph()
    stats = backfill_subsidiaries(conn, _relations(), report_path=Path(tempfile.mkdtemp()) / "sub.json")
    # "Enerji Yatırımları A.Ş." must NOT become METGÜN ENERJİ YATIRIMLARI (METEN)
    assert stats["unmatched_child"] == 1
    n = conn.execute(
        "MATCH (:Company {ticker:'KCHOL'})-[:CONTROLS]->(c:Company {ticker:'METEN'}) "
        "RETURN count(c)").get_next()[0]
    assert n == 0


def test_loader_matches_through_turkish_morphology():
    conn = _graph()
    backfill_subsidiaries(conn, _relations(), report_path=Path(tempfile.mkdtemp()) / "sub.json")
    # "Makinaları" (form) aligns with "Makineleri" (graph) via prefix match -> stake
    n = conn.execute(
        "MATCH (:Company {ticker:'KCHOL'})-[r:HOLDS_STAKE]->(c:Company {ticker:'TTRAK'}) "
        "RETURN r.pct").get_next()[0]
    assert n == 37.5


def test_loader_preserves_existing_gleif_provenance():
    conn = _graph()
    # pre-existing GLEIF-L2 filings-grade edge KCHOL->ARCLK
    conn.execute(
        "MATCH (p:Company {ticker:'KCHOL'}), (c:Company {ticker:'ARCLK'}) "
        "CREATE (p)-[:CONTROLS {source:'GLEIF-L2', basis:'direct-consolidation', "
        "confidence:0.95}]->(c)")
    stats = backfill_subsidiaries(conn, _relations(), report_path=Path(tempfile.mkdtemp()) / "sub.json")
    # ARCLK is corroborated, not re-created or downgraded
    assert stats["controls_corroborated"] == 1
    row = conn.execute(
        "MATCH (:Company {ticker:'KCHOL'})-[r:CONTROLS]->(:Company {ticker:'ARCLK'}) "
        "RETURN r.source, r.confidence").get_next()
    assert row[0] == "GLEIF-L2"          # provenance preserved
    assert row[1] == 0.95                # confidence not downgraded


def test_loader_idempotent_and_skips_self_links():
    conn = _graph()
    rels = _relations()
    # add a self-referential row (parent lists itself) -> must be skipped
    rels.append({"child_name": "Koç Holding A.Ş.", "relation": "Bağlı Ortaklık",
                 "relation_kind": "subsidiary", "pct": 100.0, "currency": "TRY",
                 "parent_ticker": "KCHOL", "as_of": "2025-12-24"})
    backfill_subsidiaries(conn, rels, report_path=Path(tempfile.mkdtemp()) / "sub.json")
    stats = backfill_subsidiaries(conn, rels, report_path=Path(tempfile.mkdtemp()) / "sub.json")          # second run
    assert stats["self_links_skipped"] == 1
    edges = conn.execute(
        "MATCH (:Company {ticker:'KCHOL'})-[r:CONTROLS]->(:Company {ticker:'ARCLK'}) "
        "RETURN count(r)").get_next()[0]
    assert edges == 1                                  # MERGE, no duplicate

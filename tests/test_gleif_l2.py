"""GLEIF Level-2 parent-edge back-fill tests.

Fully offline: a FakeAdapter returns canned ParentResults (the network shape is
verified separately by the live adapter's smoke_check), the loader writes into a
temp Kuzu DB, and we assert the CONTROLS / SUBSIDIARY_OF graph it builds.

    PYTHONPATH=src python -m pytest tests/test_gleif_l2.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tmkg.adapters.gleif_adapter import ParentResult
from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.loaders.gleif_l2_backfill import backfill_l2_parents

# In-universe LEIs
L_KOC = "7890005U0H950VH19H45"   # Koç Holding (top holdco)
L_ARC = "789000748KTQCUMJ0R25"   # Arçelik   -> Koç (direct == ultimate)
L_SAH = "78900090FFOWNLGP0F20"   # Sabancı Holding (top holdco)
L_AKB = "789000TUMN63Z28TJ497"   # Akbank    -> Sabancı (direct == ultimate)
L_SUB = "789000SUBSUB0000SUB1"   # a node with DISTINCT direct + ultimate parents
L_TOP = "789000TOPTOP0000TOP1"   # no reported parent
# Out-of-universe parent
L_FOREIGN = "529900FOREIGNPARENT1"


class FakeAdapter:
    """Stand-in for GleifAdapter.fetch_parents — canned, no network."""
    def __init__(self, table: dict[str, ParentResult]):
        self._t = table
        self.calls = 0

    def fetch_parents(self, lei: str, use_cache: bool = True) -> ParentResult:
        self.calls += 1
        return self._t.get(lei, ParentResult(lei=lei, note="ok"))


def _seed(conn):
    companies = [
        ("co-koc", L_KOC, "KCHOL", "KOÇ HOLDİNG A.Ş."),
        ("co-arc", L_ARC, "ARCLK", "ARÇELİK A.Ş."),
        ("co-sah", L_SAH, "SAHOL", "HACI ÖMER SABANCI HOLDİNG A.Ş."),
        ("co-akb", L_AKB, "AKBNK", "AKBANK T.A.Ş."),
        ("co-sub", L_SUB, "SUBCO", "SUB COMPANY A.Ş."),
        ("co-top", L_TOP, "TOPCO", "TOP HOLDCO A.Ş."),
        ("co-extchild", L_FOREIGN[:4] + "CHILD000000CHILD", "EXTCH", "EXT CHILD A.Ş."),
    ]
    # last one's child LEI must be in-universe; give it its own LEI
    companies[-1] = ("co-extchild", "789000EXTCHILD00CHIL", "EXTCH", "EXT CHILD A.Ş.")
    for uuid, lei, ticker, name in companies:
        conn.execute(
            "MERGE (c:Company {uuid:$u}) SET c.lei=$lei, c.ticker=$t, c.name=$n, "
            "c.is_listed=true",
            {"u": uuid, "lei": lei, "t": ticker, "n": name},
        )


def _table():
    return {
        L_ARC: ParentResult(L_ARC, L_KOC, "KOÇ HOLDİNG", L_KOC, "KOÇ HOLDİNG"),
        L_AKB: ParentResult(L_AKB, L_SAH, "SABANCI", L_SAH, "SABANCI"),
        # distinct direct vs ultimate, both in-universe
        L_SUB: ParentResult(L_SUB, L_SAH, "SABANCI", L_KOC, "KOÇ HOLDİNG"),
        L_TOP: ParentResult(L_TOP),  # no parents
        "789000EXTCHILD00CHIL": ParentResult(
            "789000EXTCHILD00CHIL", L_FOREIGN, "FOREIGN PARENT INC",
            L_FOREIGN, "FOREIGN PARENT INC"),
    }


def _count(conn, rel):
    r = conn.execute(f"MATCH ()-[e:{rel}]->() RETURN count(e)")
    return r.get_next()[0]


def test_builds_control_and_subsidiary_edges():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "g.kuzu")
        apply_schema(conn)
        _seed(conn)
        adapter = FakeAdapter(_table())
        stats = backfill_l2_parents(
            conn, adapter, only_missing=False, create_missing_parents=False,
            report_path=Path(d) / "rep.json",
        )
        # ARCLK->KOC, AKBNK->SAH: 2 direct in-universe.
        # SUBCO: direct SAH (in-univ) + ultimate KOC (in-univ, distinct).
        assert stats["direct_in_universe"] == 3       # ARC, AKB, SUB(direct)
        assert stats["ultimate_in_universe"] == 1     # SUB(ultimate only; others equal direct)
        assert stats["no_parent"] == 3                # TOP + the two top holdcos KCHOL, SAHOL
        assert stats["direct_external"] == 1          # EXTCH -> FOREIGN (skipped)
        assert stats["external_parents_created"] == 0
        # SUBSIDIARY_OF: ARC, AKB, SUB = 3 (direct only)
        assert _count(conn, "SUBSIDIARY_OF") == 3
        # CONTROLS: 3 direct + 1 ultimate = 4
        assert _count(conn, "CONTROLS") == 4


def test_edge_provenance_stamped():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "g.kuzu")
        apply_schema(conn)
        _seed(conn)
        backfill_l2_parents(conn, FakeAdapter(_table()), only_missing=False,
                            report_path=Path(d) / "rep.json")
        r = conn.execute(
            "MATCH (:Company {ticker:'KCHOL'})-[e:CONTROLS]->(:Company {ticker:'ARCLK'}) "
            "RETURN e.basis, e.source, e.confidence")
        basis, source, conf = r.get_next()
        assert basis == "direct-consolidation"
        assert source == "GLEIF-L2"
        assert conf == 0.95
        # the ultimate edge carries a different basis
        r2 = conn.execute(
            "MATCH (:Company {ticker:'KCHOL'})-[e:CONTROLS]->(:Company {ticker:'SUBCO'}) "
            "RETURN e.basis")
        assert r2.get_next()[0] == "ultimate-consolidation"


def test_idempotent_rerun():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "g.kuzu")
        apply_schema(conn)
        _seed(conn)
        backfill_l2_parents(conn, FakeAdapter(_table()), only_missing=False,
                            report_path=Path(d) / "rep.json")
        c1, s1 = _count(conn, "CONTROLS"), _count(conn, "SUBSIDIARY_OF")
        backfill_l2_parents(conn, FakeAdapter(_table()), only_missing=False,
                            report_path=Path(d) / "rep.json")
        assert (_count(conn, "CONTROLS"), _count(conn, "SUBSIDIARY_OF")) == (c1, s1)


def test_only_missing_skips_resolved():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "g.kuzu")
        apply_schema(conn)
        _seed(conn)
        backfill_l2_parents(conn, FakeAdapter(_table()), only_missing=False,
                            report_path=Path(d) / "rep.json")
        a2 = FakeAdapter(_table())
        stats = backfill_l2_parents(conn, a2, only_missing=True,
                                    report_path=Path(d) / "rep.json")
        # ARC, AKB, SUB now have a SUBSIDIARY_OF edge -> excluded. The two top
        # holdcos (KCHOL, SAHOL), TOP (no parent), and EXTCH (external, never got
        # a SUBSIDIARY_OF edge) remain as targets.
        assert stats["targets"] == 4
        assert a2.calls == 4


def test_create_missing_parents_materialises_external():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "g.kuzu")
        apply_schema(conn)
        _seed(conn)
        stats = backfill_l2_parents(
            conn, FakeAdapter(_table()), only_missing=False,
            create_missing_parents=True, report_path=Path(d) / "rep.json")
        assert stats["external_parents_created"] == 1
        # external parent node exists, tagged, and controls the child
        r = conn.execute(
            "MATCH (p:Company {uuid:$u}) RETURN p.listing_status, p.is_listed",
            {"u": f"ext-{L_FOREIGN}"})
        status, listed = r.get_next()
        assert status == "EXTERNAL_PARENT"
        assert listed is False
        r2 = conn.execute(
            "MATCH (:Company {uuid:$u})-[:CONTROLS]->(:Company {ticker:'EXTCH'}) "
            "RETURN count(*)", {"u": f"ext-{L_FOREIGN}"})
        assert r2.get_next()[0] == 1

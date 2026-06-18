"""Sector back-fill: populate the KAP sector taxonomy onto the live graph.

The live graph seeds Company identity from KAP but carries no sector data. This
loader reads the committed sector reference (`data/reference/sectors.json` via
`SectorAdapter`) and writes, idempotently:

  * `Sector` nodes for the full two-level taxonomy (main sectors + sub-sectors),
    each carrying `level` and `parent_code`;
  * `SUBSECTOR_OF` edges (sub-sector -> main sector) so roll-ups are one hop;
  * `IN_SECTOR` edges from every existing `Company` whose ticker resolves in the
    reference to its LEAF (sub-sector); the main sector is reached by traversal,
    so no redundant company->main edge is written.

It matches on the Company's `ticker` (the reference includes legacy/secondary
codes, so a ticker variant still resolves). Companies with no match — debt-only
issuers, funds, names absent from the equities taxonomy — are left unlinked and
counted, never guessed. Every run writes an audit report for human review.

IN_SECTOR carries no provenance columns in the ontology (it is a structured
classification, confidence 1.0 by construction), so this is a deterministic,
re-runnable upsert.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import kuzu

from tmkg import config
from tmkg.adapters.sector_adapter import SectorAdapter

REPORT_PATH = config.REPO_ROOT / "data" / "cache" / "sector_backfill_report.json"


def _scalar(conn: kuzu.Connection, cypher: str, params: dict | None = None):
    res = conn.execute(cypher, params or {})
    return res.get_next()[0] if res.has_next() else None


def load_sector_nodes(conn: kuzu.Connection, adapter: SectorAdapter) -> int:
    """Upsert every Sector node (main + sub) with level + parent_code."""
    for s in adapter.sectors():
        conn.execute(
            """
            MERGE (x:Sector {code: $code})
            SET x.name=$name, x.level=$level, x.parent_code=$parent
            """,
            {"code": s.code, "name": s.name, "level": s.level, "parent": s.parent},
        )
    return len(adapter.sectors())


def load_subsector_edges(conn: kuzu.Connection, adapter: SectorAdapter) -> int:
    """Connect each sub-sector to its main sector (sub -[:SUBSECTOR_OF]-> main)."""
    n = 0
    for s in adapter.sectors():
        if s.parent is None:
            continue
        conn.execute(
            """
            MATCH (c:Sector {code: $child}), (p:Sector {code: $parent})
            MERGE (c)-[:SUBSECTOR_OF]->(p)
            """,
            {"child": s.code, "parent": s.parent},
        )
        n += 1
    return n


def load_company_sectors(conn: kuzu.Connection, adapter: SectorAdapter) -> dict:
    """Link existing Company nodes to their leaf sub-sector by ticker.

    Returns an audit dict: linked companies, unmatched tickers, and the set of
    sub-sectors that actually received at least one company."""
    res = conn.execute(
        "MATCH (c:Company) WHERE c.ticker IS NOT NULL AND c.ticker <> '' "
        "RETURN c.ticker"
    )
    tickers = []
    while res.has_next():
        tickers.append(res.get_next()[0])

    linked, unmatched = 0, []
    used_leaves: set[str] = set()
    for ticker in tickers:
        lk = adapter.lookup(ticker)
        if not lk.found:
            unmatched.append(ticker)
            continue
        conn.execute(
            """
            MATCH (c:Company {ticker: $ticker}), (s:Sector {code: $leaf})
            MERGE (c)-[r:IN_SECTOR]->(s)
            SET r.sector_basis = 'kap-direct'
            """,
            {"ticker": ticker, "leaf": lk.leaf},
        )
        linked += 1
        used_leaves.add(lk.leaf)

    return {
        "companies_total": len(tickers),
        "companies_linked": linked,
        "companies_unmatched": len(unmatched),
        "unmatched_tickers": sorted(unmatched),
        "leaves_populated": len(used_leaves),
    }


def _upward_parent_map(conn: kuzu.Connection) -> dict[str, set[str]]:
    """child_uuid -> {parent_uuid} over BOTH control sources (CONTROLS reversed +
    SUBSIDIARY_OF as-is)."""
    parent_of: dict[str, set[str]] = {}
    r = conn.execute("MATCH (p:Company)-[:CONTROLS]->(c:Company) RETURN p.uuid, c.uuid")
    while r.has_next():
        p, c = r.get_next()
        parent_of.setdefault(c, set()).add(p)
    r = conn.execute("MATCH (c:Company)-[:SUBSIDIARY_OF]->(p:Company) RETURN c.uuid, p.uuid")
    while r.has_next():
        c, p = r.get_next()
        parent_of.setdefault(c, set()).add(p)
    return parent_of


def inherit_sectors(conn: kuzu.Connection) -> dict:
    """Propagate a sectored parent's leaf sub-sector to controlled children that
    have NO sector of their own (F8).

    Additive and provenance-honest: writes IN_SECTOR with
    sector_basis='inherited-from-parent' only for a company that has no IN_SECTOR
    edge at all, and only from its NEAREST genuinely (KAP-)sectored ancestor over
    the control graph. A KAP-assigned edge is never touched, and inherited edges
    are never chained off other inherited edges (every inheritance traces to a
    real KAP sector). EXTERNAL_* stub/parent nodes are skipped — they are control
    anchors, not classifiable entities.
    """
    # leaf sub-sector each company currently sits in (KAP-direct at this point).
    leaf: dict[str, str] = {}
    r = conn.execute("MATCH (c:Company)-[:IN_SECTOR]->(s:Sector) RETURN c.uuid, s.code")
    while r.has_next():
        u, code = r.get_next()
        leaf.setdefault(u, code)

    parent_of = _upward_parent_map(conn)

    r = conn.execute(
        "MATCH (c:Company) "
        "WHERE NOT EXISTS { MATCH (c)-[:IN_SECTOR]->(:Sector) } "
        "AND (c.listing_status IS NULL OR NOT c.listing_status STARTS WITH 'EXTERNAL') "
        "RETURN c.uuid, c.ticker")
    targets: list[tuple[str, str | None]] = []
    while r.has_next():
        targets.append(tuple(r.get_next()))

    written: list[dict] = []
    for uuid, ticker in targets:
        # BFS up to the nearest ancestor carrying a (KAP-) sector.
        seen = {uuid}
        frontier = list(parent_of.get(uuid, ()))
        found_leaf = found_src = None
        while frontier and found_leaf is None:
            nxt: list[str] = []
            for p in frontier:
                if p in seen:
                    continue
                seen.add(p)
                if p in leaf:
                    found_leaf, found_src = leaf[p], p
                    break
                nxt.extend(parent_of.get(p, ()))
            frontier = nxt
        if found_leaf is None:
            continue
        conn.execute(
            "MATCH (c:Company {uuid:$c}), (s:Sector {code:$leaf}) "
            "MERGE (c)-[r:IN_SECTOR]->(s) "
            "SET r.sector_basis='inherited-from-parent', r.source='control-graph'",
            {"c": uuid, "leaf": found_leaf})
        written.append({"company": ticker or uuid, "leaf": found_leaf,
                        "from_parent": found_src})

    return {"inherited": len(written), "inherited_edges": written}


def sector_coverage(conn: kuzu.Connection) -> dict:
    """Sector coverage reported BOTH company-weighted and instrument-weighted (F8).

    The company-weighted figure ("606/729 classified") flatters the picture when
    most debt sits with unsectored issuers; the instrument-weighted figure (share
    of debt Securities whose issuer carries a sector) is the honest companion. The
    729-denominator excludes EXTERNAL_* stub/parent anchors, as elsewhere.
    """
    in_universe = ("(c.listing_status IS NULL "
                   "OR NOT c.listing_status STARTS WITH 'EXTERNAL')")
    total_co = _scalar(conn,
        f"MATCH (c:Company) WHERE {in_universe} RETURN count(c)") or 0
    sectored_co = _scalar(conn,
        f"MATCH (c:Company) WHERE {in_universe} "
        "AND EXISTS { MATCH (c)-[:IN_SECTOR]->(:Sector) } RETURN count(c)") or 0
    inherited_co = _scalar(conn,
        f"MATCH (c:Company) WHERE {in_universe} "
        "AND EXISTS { MATCH (c)-[r:IN_SECTOR]->(:Sector) "
        "            WHERE r.sector_basis='inherited-from-parent' } "
        "RETURN count(c)") or 0
    total_instr = _scalar(conn,
        "MATCH (:Company)-[:ISSUES]->(s:Security) RETURN count(s)") or 0
    sectored_instr = _scalar(conn,
        "MATCH (c:Company)-[:ISSUES]->(s:Security) "
        "WHERE EXISTS { MATCH (c)-[:IN_SECTOR]->(:Sector) } RETURN count(s)") or 0
    return {
        "companies_total": total_co,
        "companies_sectored": sectored_co,
        "companies_sectored_inherited": inherited_co,
        "company_weighted_coverage": round(sectored_co / total_co, 4) if total_co else 0.0,
        "instruments_total": total_instr,
        "instruments_sectored": sectored_instr,
        "instrument_weighted_coverage": round(sectored_instr / total_instr, 4)
                                        if total_instr else 0.0,
    }


def backfill(conn: kuzu.Connection, adapter: SectorAdapter | None = None,
             write_report: bool = True) -> dict:
    """Run the full sector back-fill and (optionally) write the audit report.

    Includes the F8 inheritance pass (propagate sector parent->NEI-child over
    CONTROLS) and reports coverage both company- and instrument-weighted. The
    inheritance pass is a no-op until control edges exist, so it is safe to run on
    a graph that has not yet been through the GLEIF/KAP/SPV control stages.
    """
    adapter = adapter or SectorAdapter().load(strict=True)
    if len(adapter) == 0:
        raise RuntimeError(
            f"sector reference is empty/missing at {adapter.reference_path} — run "
            "scripts/import_sectors.py first."
        )

    n_nodes = load_sector_nodes(conn, adapter)
    n_edges = load_subsector_edges(conn, adapter)
    company = load_company_sectors(conn, adapter)
    inheritance = inherit_sectors(conn)
    coverage = sector_coverage(conn)

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "source": adapter.source,
        "fetched_iso": adapter.fetched_iso,
        "sector_nodes": n_nodes,
        "subsector_edges": n_edges,
        **company,
        "inherited_sectors": inheritance["inherited"],
        "inherited_edges": inheritance["inherited_edges"],
        "coverage": coverage,
    }
    if write_report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        report["report_path"] = str(REPORT_PATH)
    return report

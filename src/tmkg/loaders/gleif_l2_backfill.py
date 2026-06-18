"""GLEIF Level-2 back-fill — corporate control / ownership edges.

Turns the identity spine (LEIs attached in Level-1) into a *control graph* using
GLEIF's Level-2 relationship data — each entity's self-reported direct and
ultimate accounting-consolidation parent. This is the substrate the architecture
calls for under `CONTROLS` / `SUBSIDIARY_OF` (ontology §2).

Edges written (per the ontology):
  - direct parent in our universe →
        (parent)-[:CONTROLS  {basis:'direct-consolidation'}]->(child)
        (child)-[:SUBSIDIARY_OF]->(parent)
  - ultimate parent in our universe, when different from the direct parent →
        (ultimate)-[:CONTROLS {basis:'ultimate-consolidation'}]->(child)
    (SUBSIDIARY_OF is the *direct* legal relationship only; the ultimate link is
    a control edge, not a direct-subsidiary edge.)

Provenance / scope stance (consistent with the rest of the project):
  - GLEIF L2 is FILINGS-GRADE (the entity reports its own parent), so matches
    need no fuzzy threshold — confidence is fixed high (0.95) and the source /
    method are stamped on every edge.
  - **In-universe by default**: a parent whose LEI is not among our Companies is
    logged to the audit report and skipped, NOT invented. `create_missing_parents`
    opts into materialising a minimal external-parent Company node (mirrors the
    debt loader's `--create-missing-issuers`). This keeps the entity universe
    honest unless you deliberately widen it.
  - Idempotent: edges are MERGE-d; re-running only refreshes edge properties.

Requires Level-1 LEIs to be present first (`backfill_gleif --stage lei`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.gleif_adapter import GleifAdapter

_SOURCE = "GLEIF-L2"
_METHOD = "gleif-relationship-api"
_CONFIDENCE = 0.95


def _lei_to_uuid(conn: kuzu.Connection) -> dict[str, str]:
    """Map every in-universe LEI to its Company uuid (the parent-resolution key)."""
    res = conn.execute(
        "MATCH (c:Company) WHERE c.lei IS NOT NULL AND c.lei <> '' "
        "RETURN c.lei, c.uuid"
    )
    out: dict[str, str] = {}
    while res.has_next():
        lei, uuid = res.get_next()
        out[lei] = uuid
    return out


def _targets(conn: kuzu.Connection, only_missing: bool) -> list[dict]:
    """Companies carrying an LEI. With only_missing, skip those that already have
    an outgoing SUBSIDIARY_OF edge (already resolved on a prior run)."""
    if only_missing:
        q = ("MATCH (c:Company) WHERE c.lei IS NOT NULL AND c.lei <> '' "
             "AND NOT EXISTS { MATCH (c)-[:SUBSIDIARY_OF]->(:Company) } "
             "RETURN c.uuid, c.lei, c.ticker, c.name ORDER BY c.ticker")
    else:
        q = ("MATCH (c:Company) WHERE c.lei IS NOT NULL AND c.lei <> '' "
             "RETURN c.uuid, c.lei, c.ticker, c.name ORDER BY c.ticker")
    res = conn.execute(q)
    rows = []
    while res.has_next():
        uuid, lei, ticker, name = res.get_next()
        rows.append({"uuid": uuid, "lei": lei, "ticker": ticker, "name": name})
    return rows


def _ensure_external_parent(conn: kuzu.Connection, lei: str, name: str | None) -> str:
    """Materialise (idempotently) a minimal Company node for an out-of-universe
    parent and return its uuid. Tagged so it's distinguishable from KAP issuers."""
    uuid = f"ext-{lei}"
    conn.execute(
        """
        MERGE (c:Company {uuid: $uuid})
        SET c.lei=$lei,
            c.name=COALESCE($name, c.name),
            c.is_listed=false,
            c.listing_status='EXTERNAL_PARENT'
        """,
        {"uuid": uuid, "lei": lei, "name": name},
    )
    return uuid


def _write_control(conn: kuzu.Connection, parent_uuid: str, child_uuid: str,
                   basis: str, subsidiary: bool) -> None:
    """MERGE a CONTROLS edge (parent->child) and, for direct parents, the
    matching SUBSIDIARY_OF edge (child->parent). Idempotent."""
    conn.execute(
        """
        MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c})
        MERGE (p)-[r:CONTROLS]->(c)
        SET r.basis=$basis, r.source=$src,
            r.extraction_method=$method, r.confidence=$conf
        """,
        {"p": parent_uuid, "c": child_uuid, "basis": basis,
         "src": _SOURCE, "method": _METHOD, "conf": _CONFIDENCE},
    )
    if subsidiary:
        conn.execute(
            """
            MATCH (c:Company {uuid:$c}), (p:Company {uuid:$p})
            MERGE (c)-[r:SUBSIDIARY_OF]->(p)
            SET r.source=$src, r.extraction_method=$method, r.confidence=$conf
            """,
            {"c": child_uuid, "p": parent_uuid,
             "src": _SOURCE, "method": _METHOD, "conf": _CONFIDENCE},
        )


def backfill_l2_parents(
    conn: kuzu.Connection,
    adapter: GleifAdapter,
    limit: int | None = None,
    only_missing: bool = True,
    create_missing_parents: bool = False,
    report_path: Path | str | None = None,
) -> dict:
    """Build CONTROLS / SUBSIDIARY_OF edges from GLEIF Level-2 parents.

    Returns a summary dict and writes a full audit report to `report_path`
    (default: data/cache/gleif_l2_report.json).
    """
    lei2uuid = _lei_to_uuid(conn)
    targets = _targets(conn, only_missing)
    if limit is not None:
        targets = targets[:limit]

    report: list[dict] = []
    stats = {
        "targets": len(targets), "direct_in_universe": 0, "direct_external": 0,
        "ultimate_in_universe": 0, "ultimate_external": 0,
        "no_parent": 0, "http_errors": 0,
        "external_parents_created": 0, "external_reconciled": 0,
        "edges_written": 0,
    }

    def resolve(parent_lei: str | None, parent_name: str | None) -> tuple[str | None, str]:
        """Return (parent_uuid, disposition) for a reported parent LEI."""
        if not parent_lei:
            return None, "none"
        if parent_lei in lei2uuid:
            return lei2uuid[parent_lei], "in_universe"
        if create_missing_parents:
            # Mint a fresh LEI-keyed node for this out-of-universe parent. (Brand-
            # stub reconciliation was retired with the debt/stubs subsystem —
            # external parents are no longer collapsed onto inferred EXTERNAL_STUB
            # placeholders; real GLEIF entities stand on their own LEI.)
            uuid = _ensure_external_parent(conn, parent_lei, parent_name)
            stats["external_parents_created"] += 1
            return uuid, "external_created"
        return None, "external_skipped"

    for t in targets:
        child_uuid, lei = t["uuid"], t["lei"]
        pr = adapter.fetch_parents(lei)
        entry = {
            "ticker": t["ticker"], "lei": lei, "kap_name": t["name"],
            "direct_lei": pr.direct_lei, "direct_name": pr.direct_name,
            "ultimate_lei": pr.ultimate_lei, "ultimate_name": pr.ultimate_name,
            "note": pr.note,
        }

        if pr.note.startswith("http_error"):
            stats["http_errors"] += 1
            report.append(entry)
            continue

        if not pr.direct_lei and not pr.ultimate_lei:
            stats["no_parent"] += 1
            report.append(entry)
            continue

        # --- direct parent (CONTROLS + SUBSIDIARY_OF) ---
        d_uuid, d_disp = resolve(pr.direct_lei, pr.direct_name)
        entry["direct_disposition"] = d_disp
        if pr.direct_lei and pr.direct_lei != lei:
            if d_disp in ("in_universe", "external_created", "external_reconciled"):
                _write_control(conn, d_uuid, child_uuid,
                               basis="direct-consolidation", subsidiary=True)
                stats["edges_written"] += 2
                stats["direct_in_universe" if d_disp == "in_universe"
                      else "direct_external"] += 1
            elif d_disp == "external_skipped":
                stats["direct_external"] += 1

        # --- ultimate parent (CONTROLS only, if distinct from direct) ---
        if pr.ultimate_lei and pr.ultimate_lei not in (lei, pr.direct_lei):
            u_uuid, u_disp = resolve(pr.ultimate_lei, pr.ultimate_name)
            entry["ultimate_disposition"] = u_disp
            if u_disp in ("in_universe", "external_created", "external_reconciled"):
                _write_control(conn, u_uuid, child_uuid,
                               basis="ultimate-consolidation", subsidiary=False)
                stats["edges_written"] += 1
                stats["ultimate_in_universe" if u_disp == "in_universe"
                      else "ultimate_external"] += 1
            elif u_disp == "external_skipped":
                stats["ultimate_external"] += 1

        report.append(entry)

    rp = Path(report_path) if report_path else (
        Path(config.RAW_DOCS_PATH).parent / "cache" / "gleif_l2_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "source": _SOURCE, "confidence": _CONFIDENCE,
        "create_missing_parents": create_missing_parents,
        "summary": stats,
        "relationships": report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    stats["report"] = str(rp)
    return stats

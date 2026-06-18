"""SPV -> parent CONTROLS edges inferred from regulated Turkish naming convention.

Most debt-issuer SPVs still lacking a control edge are special-purpose vehicles
whose parent is embedded in their OWN name by regulation/convention:

    "<Brand> Varlık Kiralama A.Ş."      sukuk SPV       -> parent <Brand> (bank/holding)
    "<Brand> Finansal Kiralama A.Ş."    leasing arm     -> parent <Brand>
    "<Brand> Faktoring A.Ş."            factoring arm   -> parent <Brand>
    "<Brand> Yatırım Menkul Değerler"   brokerage       -> parent <Brand>

This is a HEURISTIC (not a filing), so it is the lowest-confidence control source
in the project (0.70, source 'spv-name-inference') and is gated hard for
precision — a wrong control edge is worse than a missing one:

  - SCOPE: only debt-issuer SPV entity types (asset-leasing / factoring / leasing
    / financing / broker). Operating companies, banks, holdings and REITs are
    apex/standalone and are NOT given inferred parents.
  - BRAND: the SPV's lead brand is its first identity token that is neither
    generic nor GEOGRAPHIC — so "Türk Finansman" (Türk = geo, Finansman =
    generic) yields no brand and is skipped, and "Albaraka Türk" can never bridge
    to "Mercedes-Benz Finansman Türk" via the shared "Türk".
  - PARENT: an in-graph BANK or HOLDING whose OWN lead identity token EQUALS the
    SPV's brand, is not the SPV itself, and is the UNIQUE such candidate. If two
    group entities lead with the same brand (e.g. three "TERA" entities), the case
    is logged ambiguous and NOT written.
  - NON-DESTRUCTIVE: an existing CONTROLS edge (GLEIF-L2 / KAP-subsidiary) is
    corroborated, never overwritten.

Run after the GLEIF-L2 and KAP-subsidiary stages so it only fills genuine gaps.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.gleif_adapter import _ascii_fold
from tmkg.loaders.debt_backfill import _identity_tokens, _lead_identity, _entity_type

LOADER_VERSION = 1
_SOURCE = "spv-name-inference"
_METHOD = "spv-naming-convention"
_BASIS = "spv-naming-convention"
_CONFIDENCE = 0.70

# Debt-class securities whose issuers we try to attach to a parent.
_DEBT_CLASSES = ("TRS", "TRF", "TRD", "XS")
# SPV / finance-arm entity types that follow the "<parent brand> <type>" naming
# convention. (BANK / HOLDING / REIT / operating are apex or standalone.)
_SPV_TYPES = {"ASSET_LEASING", "FACTORING", "LEASING", "FINANCING", "BROKER"}
# Parent-capable types: the controllers of those SPVs.
_PARENT_TYPES = {"BANK", "HOLDING"}
_GEO = {"TURK", "TURKIYE"}


def _spv_lead_brand(name: str) -> str | None:
    """First identity token of the SPV name that is neither generic nor geographic
    — the brand that names the controlling group. None if the name is all generic
    /geo (e.g. "Türk Finansman A.Ş.")."""
    ids = _identity_tokens(name)
    for tok in name.replace(".", " ").replace("-", " ").split():
        f = _ascii_fold(tok).upper()
        if f in ids and f not in _GEO:
            return f
    return None


def _debt_issuer_spvs(conn: kuzu.Connection) -> list[dict]:
    """Companies that issue a debt-class Security and whose name classifies as an
    SPV / finance-arm type."""
    res = conn.execute(
        "MATCH (co:Company)-[i:ISSUES]->() "
        "WHERE i.instrument_class IN $cls AND co.name IS NOT NULL AND co.name <> '' "
        "RETURN DISTINCT co.uuid, co.name, co.ticker",
        {"cls": list(_DEBT_CLASSES)})
    out = []
    while res.has_next():
        uuid, name, ticker = res.get_next()
        if _entity_type(name) in _SPV_TYPES:
            out.append({"uuid": uuid, "name": name, "ticker": ticker})
    return out


def _parent_index(conn: kuzu.Connection) -> dict[str, list[dict]]:
    """BANK/HOLDING companies grouped by their lead identity token (the parent's
    brand). A brand mapping to >1 company makes that brand ambiguous."""
    res = conn.execute(
        "MATCH (c:Company) WHERE c.name IS NOT NULL AND c.name <> '' "
        "RETURN c.uuid, c.name, c.ticker")
    by_brand: dict[str, list[dict]] = {}
    while res.has_next():
        uuid, name, ticker = res.get_next()
        if _entity_type(name) not in _PARENT_TYPES:
            continue
        lead = _lead_identity(name)
        if not lead or lead in _GEO:
            continue
        by_brand.setdefault(lead, []).append(
            {"uuid": uuid, "name": name, "ticker": ticker})
    return by_brand


def _edge_exists(conn, rel: str, a: str, b: str) -> bool:
    return conn.execute(
        f"MATCH (x:Company {{uuid:$a}})-[r:{rel}]->(y:Company {{uuid:$b}}) "
        f"RETURN count(r)", {"a": a, "b": b}).get_next()[0] > 0


def _write_control(conn, p_uuid: str, c_uuid: str) -> bool:
    """Create CONTROLS + SUBSIDIARY_OF if absent (non-destructive). Returns True
    if a new CONTROLS edge was created."""
    created = False
    if not _edge_exists(conn, "CONTROLS", p_uuid, c_uuid):
        conn.execute(
            """MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c})
               MERGE (p)-[r:CONTROLS]->(c)
               SET r.basis=$basis, r.source=$src, r.extraction_method=$m,
                   r.confidence=$conf""",
            {"p": p_uuid, "c": c_uuid, "basis": _BASIS, "src": _SOURCE,
             "m": _METHOD, "conf": _CONFIDENCE})
        created = True
    if not _edge_exists(conn, "SUBSIDIARY_OF", c_uuid, p_uuid):
        conn.execute(
            """MATCH (c:Company {uuid:$c}), (p:Company {uuid:$p})
               MERGE (c)-[r:SUBSIDIARY_OF]->(p)
               SET r.source=$src, r.extraction_method=$m, r.confidence=$conf""",
            {"c": c_uuid, "p": p_uuid, "src": _SOURCE, "m": _METHOD,
             "conf": _CONFIDENCE})
    return created


# Edges the SPV naming convention misattributes as SOLE control but which are in
# fact joint ventures (the controlling group is only one of several JV partners).
# The inference writes a single parent->child CONTROLS edge at full SPV
# confidence; this demotes it so blast-radius provenance reports it as
# JV-suspect, not a clean sole-control link. Keyed on (parent_ticker, child_ticker).
# KCHOL->KSFIN (Koç Finansman) is the audit's named false-precision case (F7).
_JV_BASIS = "spv-naming-convention-jv-suspect"
_JV_CONFIDENCE = 0.40
_KNOWN_JV_SUSPECTS = (("KCHOL", "KSFIN"),)


def demote_jv_suspect_edges(
    conn: kuzu.Connection,
    pairs: tuple[tuple[str, str], ...] = _KNOWN_JV_SUSPECTS,
) -> dict:
    """Demote known JV-misattributed CONTROLS edges (F7).

    For each (parent_ticker, child_ticker) that the SPV inference may have
    written as sole control, rewrite the edge's basis to
    'spv-naming-convention-jv-suspect' and drop its confidence, so downstream
    provenance reporting flags it rather than presenting a JV as confident sole
    control. No-op for pairs whose edge does not exist. Idempotent.
    """
    demoted: list[dict] = []
    for parent_t, child_t in pairs:
        r = conn.execute(
            "MATCH (p:Company {ticker:$pt})-[:CONTROLS]->(c:Company {ticker:$ct}) "
            "RETURN p.uuid, c.uuid", {"pt": parent_t, "ct": child_t})
        if not r.has_next():
            continue
        conn.execute(
            """MATCH (p:Company {ticker:$pt})-[r:CONTROLS]->(c:Company {ticker:$ct})
               SET r.basis=$basis, r.confidence=$conf""",
            {"pt": parent_t, "ct": child_t, "basis": _JV_BASIS, "conf": _JV_CONFIDENCE})
        demoted.append({"parent": parent_t, "child": child_t})
    return {"jv_suspects_demoted": len(demoted), "demoted": demoted}


def backfill_spv_parents(
    conn: kuzu.Connection,
    report_path: Path | str | None = None,
) -> dict:
    """Infer + write parent control edges for debt-issuer SPVs. Returns a summary."""
    spvs = _debt_issuer_spvs(conn)
    parents_by_brand = _parent_index(conn)

    written, ambiguous, no_parent, no_brand = [], [], [], []
    controls_new = controls_corroborated = 0

    for spv in spvs:
        brand = _spv_lead_brand(spv["name"])
        if not brand:
            no_brand.append({"spv": spv["ticker"], "name": spv["name"]})
            continue
        cands = [p for p in parents_by_brand.get(brand, []) if p["uuid"] != spv["uuid"]]
        if not cands:
            no_parent.append({"spv": spv["ticker"], "brand": brand, "name": spv["name"]})
            continue
        if len(cands) > 1:
            ambiguous.append({"spv": spv["ticker"], "brand": brand,
                              "name": spv["name"],
                              "candidates": [p["ticker"] for p in cands]})
            continue
        parent = cands[0]
        created = _write_control(conn, parent["uuid"], spv["uuid"])
        if created:
            controls_new += 1
        else:
            controls_corroborated += 1
        written.append({
            "parent_ticker": parent["ticker"], "parent_name": parent["name"],
            "spv_ticker": spv["ticker"], "spv_name": spv["name"],
            "brand": brand, "new": created,
        })

    summary = {
        "loader_version": LOADER_VERSION,
        "spv_candidates": len(spvs),
        "controls_new": controls_new,
        "controls_corroborated": controls_corroborated,
        "ambiguous": len(ambiguous),
        "no_in_graph_parent": len(no_parent),
        "no_brand": len(no_brand),
    }
    cache_dir = Path(config.RAW_DOCS_PATH).parent / "cache"
    rp = Path(report_path) if report_path else (cache_dir / "spv_parent_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "source": _SOURCE, "confidence": _CONFIDENCE,
        "summary": summary,
        "written": written,
        "ambiguous": ambiguous,
        "no_in_graph_parent": no_parent,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["report"] = str(rp)
    return summary

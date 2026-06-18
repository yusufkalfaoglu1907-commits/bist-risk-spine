"""External stub parent nodes — bounded universe widening (Phase 2.1, F3).

The KAP `IGS` universe is *listed-only*. Many debt-issuer SPVs and the cross-
border parents of listed companies are therefore out-of-graph, which leaves real
groups unassemblable: an SPV with no in-graph parent looks like a standalone, and
a blast-radius query over it returns the audit's "confident emptiness" (DNFIN).

This loader materialises a **bounded, curated** set of external **stub** Company
nodes so those groups can assemble — without reopening the fuzzy-creation fight
the listed-only rule was meant to avoid:

  - A stub is `listing_status='EXTERNAL_STUB'`, `is_listed=false`, carries **no
    equity ISIN and no pricing**, and is **excluded from every 729-denominator /
    equity-side metric** (callers filter `listing_status` NOT STARTS WITH
    'EXTERNAL'). It exists only to be a control anchor (and, in 2.2, a debt
    issuer).
  - Stubs are **brand-keyed** (`uuid = stub-<BRAND>`), so the SPV-parent source
    here and the MKK-unmatched debt source (2.2) converge on **one node per real
    entity** (e.g. one `stub-DENIZ` controls DNFIN/DENFA *and* issues Denizbank's
    debt) rather than creating duplicates that split the group.
  - Every stub + edge is justified by a committed report row (the SPV report's
    `no_in_graph_parent` list), stamped with source/confidence on the edge, and
    logged to `external_stub_report.json`. This is curation, not guessing.

Source confidence order (per the fix plan):
  - GLEIF external parents (LEI-keyed, 0.95)  — handled by the L2 stage's
    `--create-missing-parents`; reconciled here by brand when present.
  - SPV `no_in_graph_parent` (brand-named, 0.70)  — **this module**.
  - MKK unmatched issuers (0.x)  — attached in 2.2.

Non-destructive + idempotent: a stub/edge is MERGE-d; an existing higher-grade
control edge is corroborated, never overwritten.

Run AFTER `--stage spv` (it reads that stage's report).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.loaders.debt_backfill import _lead_identity

LOADER_VERSION = 1
STUB_STATUS = "EXTERNAL_STUB"

# SPV-parent source (this module's contribution).
_SPV_PARENT_SOURCE = "external-stub-spv-parent"
_SPV_PARENT_METHOD = "spv-naming-convention"
_SPV_PARENT_BASIS = "spv-naming-convention"
_SPV_PARENT_CONFIDENCE = 0.70


def _stub_uuid(brand: str) -> str:
    return "stub-" + brand


def _stub_name(brand: str) -> str:
    """Display name for a brand-keyed stub. Deliberately marked as a stub so no
    reader mistakes it for a filings-grade legal entity; the real legal name is
    filled by the MKK-unmatched source (2.2) when that entity issues debt."""
    return f"{brand} (external stub parent)"


def _read_spv_no_parent(reports_dir: Path) -> list[dict]:
    """The SPV stage's `no_in_graph_parent` rows: {spv, brand, name}. These are
    SPVs whose lead brand resolved to NO in-graph BANK/HOLDING — exactly the gap
    a stub fills."""
    f = reports_dir / "spv_parent_report.json"
    if not f.exists():
        return []
    try:
        blob = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in blob.get("no_in_graph_parent", []) if r.get("brand")]


def _ensure_stub(conn: kuzu.Connection, brand: str) -> str:
    """MERGE a brand-keyed EXTERNAL_STUB Company (idempotent). Never clobbers a
    name a richer source already set (COALESCE)."""
    uuid = _stub_uuid(brand)
    conn.execute(
        """
        MERGE (c:Company {uuid:$u})
        SET c.name = COALESCE(c.name, $n),
            c.listing_status = $st,
            c.is_listed = false
        """,
        {"u": uuid, "n": _stub_name(brand), "st": STUB_STATUS},
    )
    return uuid


def reconcile_to_brand_stub(
    conn: kuzu.Connection, lei: str, name: str | None
) -> str | None:
    """Brand reconciliation for a GLEIF external parent (Phase 2.1 remainder).

    The GLEIF Level-2 stage materialises out-of-universe parents (LEI-keyed,
    0.95). Some of those parents are the SAME real group as a brand-keyed stub
    already created here from the SPV source — e.g. GLEIF reports a listed
    company's parent as "ZİRAAT KATILIM BANKASI" while `stub-ZIRAAT` already
    controls Ziraat's SPVs. Creating a second LEI-keyed node would SPLIT that
    group across two parents.

    So before the L2 stage invents an `ext-<LEI>` node, it calls this: if a
    brand stub already exists for the parent's lead-identity brand, we enrich
    that stub with the LEI (filings-grade, never overwriting an existing one)
    and return its uuid, so the SPV-parent and GLEIF-L2 sources converge on ONE
    node per real entity. Returns None when no such stub exists — the caller
    then creates the LEI-keyed external node as before.

    Conservative by design: matches ONLY on an EXACT lead-identity brand token,
    so two genuinely distinct external entities that merely share a token are
    never merged (precision-first — a wrong merge loses a real LEI distinction).
    """
    if not name:
        return None
    brand = _lead_identity(name)
    if not brand:
        return None
    uuid = _stub_uuid(brand)
    r = conn.execute("MATCH (c:Company {uuid:$u}) RETURN c.uuid", {"u": uuid})
    if not r.has_next():
        return None
    # Attach the LEI to the existing brand stub (COALESCE: a higher-grade source
    # that already set one wins). listing_status stays EXTERNAL_* so the node
    # remains excluded from the 729-company denominator.
    conn.execute(
        "MATCH (c:Company {uuid:$u}) SET c.lei = COALESCE(c.lei, $lei)",
        {"u": uuid, "lei": lei})
    return uuid


def _company_uuid_by_ticker(conn: kuzu.Connection, ticker: str) -> str | None:
    r = conn.execute("MATCH (c:Company {ticker:$t}) RETURN c.uuid", {"t": ticker})
    return r.get_next()[0] if r.has_next() else None


def _edge_exists(conn: kuzu.Connection, rel: str, a: str, b: str) -> bool:
    return conn.execute(
        f"MATCH (x:Company {{uuid:$a}})-[r:{rel}]->(y:Company {{uuid:$b}}) "
        f"RETURN count(r)", {"a": a, "b": b}).get_next()[0] > 0


def _write_control(conn: kuzu.Connection, parent_uuid: str, child_uuid: str) -> bool:
    """Create CONTROLS (parent->child) + mirrored SUBSIDIARY_OF if absent
    (non-destructive). Returns True iff a NEW CONTROLS edge was created."""
    created = False
    if not _edge_exists(conn, "CONTROLS", parent_uuid, child_uuid):
        conn.execute(
            """MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c})
               MERGE (p)-[r:CONTROLS]->(c)
               SET r.basis=$basis, r.source=$src,
                   r.extraction_method=$m, r.confidence=$conf""",
            {"p": parent_uuid, "c": child_uuid, "basis": _SPV_PARENT_BASIS,
             "src": _SPV_PARENT_SOURCE, "m": _SPV_PARENT_METHOD,
             "conf": _SPV_PARENT_CONFIDENCE})
        created = True
    if not _edge_exists(conn, "SUBSIDIARY_OF", child_uuid, parent_uuid):
        conn.execute(
            """MATCH (c:Company {uuid:$c}), (p:Company {uuid:$p})
               MERGE (c)-[r:SUBSIDIARY_OF]->(p)
               SET r.source=$src, r.extraction_method=$m, r.confidence=$conf""",
            {"c": child_uuid, "p": parent_uuid, "src": _SPV_PARENT_SOURCE,
             "m": _SPV_PARENT_METHOD, "conf": _SPV_PARENT_CONFIDENCE})
    return created


def backfill_external_stubs(
    conn: kuzu.Connection,
    reports_dir: Path | str | None = None,
    report_path: Path | str | None = None,
) -> dict:
    """Create brand-keyed EXTERNAL_STUB parents for SPVs with no in-graph parent,
    and control edges stub -> SPV. Returns a summary; writes an audit report.

    One stub per brand: a brand controlling several SPVs (e.g. AK -> AKSFA/AKM/
    AKFK) yields a single stub that assembles all of them into one group.
    """
    cache_dir = Path(reports_dir or (Path(config.RAW_DOCS_PATH).parent / "cache"))
    spv_rows = _read_spv_no_parent(cache_dir)

    by_brand: dict[str, list[dict]] = {}
    for r in spv_rows:
        by_brand.setdefault(r["brand"], []).append(r)

    written: list[dict] = []
    missing_spv: list[dict] = []
    stubs_created = 0
    controls_new = controls_corroborated = 0

    for brand, spvs in sorted(by_brand.items()):
        stub_uuid = _ensure_stub(conn, brand)
        stubs_created += 1
        controlled: list[str] = []
        for s in spvs:
            spv_uuid = _company_uuid_by_ticker(conn, s["spv"])
            if spv_uuid is None:
                missing_spv.append({"brand": brand, "spv": s["spv"], "name": s["name"]})
                continue
            if _write_control(conn, stub_uuid, spv_uuid):
                controls_new += 1
            else:
                controls_corroborated += 1
            controlled.append(s["spv"])
        written.append({
            "stub_uuid": stub_uuid, "brand": brand, "name": _stub_name(brand),
            "source": _SPV_PARENT_SOURCE, "confidence": _SPV_PARENT_CONFIDENCE,
            "controls": controlled,
            "justified_by": [s["name"] for s in spvs],
        })

    summary = {
        "loader_version": LOADER_VERSION,
        "spv_no_parent_rows": len(spv_rows),
        "stubs_created": stubs_created,
        "controls_new": controls_new,
        "controls_corroborated": controls_corroborated,
        "spv_not_in_graph": len(missing_spv),
    }

    rp = Path(report_path) if report_path else (cache_dir / "external_stub_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "loader_version": LOADER_VERSION,
        "stub_status": STUB_STATUS,
        "summary": summary,
        "stubs": written,
        "spv_not_in_graph": missing_spv,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    summary["report"] = str(rp)
    return summary

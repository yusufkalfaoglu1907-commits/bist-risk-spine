"""Create CONTROLS / SUBSIDIARY_OF / HOLDS_STAKE edges from KAP general-info forms.

Reads the relations harvested by ``kap_subsidiary_adapter`` (each row = a parent
company's declared related party with kind + percent) and writes the control /
ownership edges that close the contagion-graph gap GLEIF L2 left open.

EDGE POLICY (per the relationship nature the company itself declared):
  - 'subsidiary' (Bağlı Ortaklık, consolidated) ->
        (parent)-[:CONTROLS {basis:'kap-bagli-ortaklik'}]->(child)
        (child)-[:SUBSIDIARY_OF]->(parent)
        (parent)-[:HOLDS_STAKE {pct}]->(child)        (when % present)
  - 'jv' / 'associate' / 'investment' -> HOLDS_STAKE only (joint / minority — not
        unilateral control, so no CONTROLS edge is asserted).

RESOLUTION (precision-first — a wrong control edge is worse than a missing one):
  - PARENT is the disclosing company, resolved by its exchange TICKER (exact, from
    the harvest). Parents not in the graph are skipped+logged.
  - CHILD is free text; resolved by a purpose-built identity-token matcher
    (:func:`match_child`) that (a) compares tokens fuzzily (common prefix >=5, so
    Turkish vowel-harmony morphology like MAKİNALARI/MAKİNELERİ still aligns),
    (b) requires the child's lead brand to be present, (c) enforces the debt
    loader's entity-type guard, and (d) REJECTS any candidate that introduces a
    *distinctive* brand token (low document-frequency) the child never named —
    this is what stops a generic child name ("Enerji Yatırımları A.Ş.") from
    collapsing onto an unrelated listed entity ("METGÜN ENERJİ YATIRIMLARI").
    Acronym-only names (TÜPRAŞ ⊄ "Türkiye Petrol Rafinerileri") stay unmatched by
    design and are logged for review, not guessed.

Idempotent: all edges MERGE-d; re-running only refreshes edge properties.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.loaders.debt_backfill import (
    _identity_tokens, _lead_identity, _entity_type, _type_conflict, _brand_set,
)

LOADER_VERSION = 1
_SOURCE = "KAP-subsidiary"
_METHOD = "kap-genel-bilgi-formu"
_CONFIDENCE = 0.85           # name-matched: below GLEIF-L2's filings-grade 0.95
_BASIS = "kap-bagli-ortaklik"

# Geographic tokens are identity-bearing but must not count as a distinctive
# "extra" a candidate adds (so "… FİNANSMAN" still resolves to "… FİNANSMAN TÜRK").
_GEO_SOFT = {"TURK", "TURKIYE"}
# A candidate token rarer than this (document-frequency over the company set) is
# treated as a distinctive brand: if the child never named it, reject the match.
_DISTINCTIVE_MAXDF = 3


# --- fuzzy token comparison ------------------------------------------------

def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _tok_match(a: str, b: str) -> bool:
    """Two identity tokens align if equal or share a >=5-char prefix (absorbs
    Turkish vowel-harmony inflections: TESİSLER/TESİSLERİ, MAKİNALARI/MAKİNELERİ)."""
    return a == b or _common_prefix_len(a, b) >= 5


def build_company_index(conn: kuzu.Connection) -> tuple[list[dict], Counter, dict]:
    """Load companies with cached identity tokens + entity type, the identity-token
    document-frequency map, and a ticker index."""
    res = conn.execute(
        "MATCH (c:Company) WHERE c.name IS NOT NULL AND c.name <> '' "
        "RETURN c.uuid, c.name, c.ticker ORDER BY c.name")
    comps: list[dict] = []
    while res.has_next():
        uuid, name, ticker = res.get_next()
        comps.append({
            "uuid": uuid, "name": name, "ticker": ticker,
            "identity": _identity_tokens(name), "etype": _entity_type(name),
        })
    df: Counter = Counter()
    for c in comps:
        for t in c["identity"]:
            df[t] += 1
    by_ticker = {(c["ticker"] or "").upper(): c for c in comps if c.get("ticker")}
    return comps, df, by_ticker


def match_child(child_name: str, comps: list[dict], df: Counter,
                threshold: float = 0.6, maxdf: int = _DISTINCTIVE_MAXDF
                ) -> tuple[dict | None, str]:
    """Resolve a related-party name to a Company, or (None, reason).

    Precision-first gate: identity coverage >= threshold (fuzzy tokens), lead
    brand present, entity-type compatible, and NO distinctive extra brand token.
    """
    ci = _identity_tokens(child_name)
    if not ci:
        return None, "no-identity-tokens"
    lead = _lead_identity(child_name)
    itype = _entity_type(child_name)

    best = None
    best_key = None
    for c in comps:
        cs = c["identity"]
        if not cs:
            continue
        shared = [t for t in ci if any(_tok_match(t, u) for u in cs)]
        if not shared:
            continue
        if lead is not None and not any(_tok_match(lead, u) for u in cs):
            continue
        if _type_conflict(itype, c["etype"]):
            continue
        cov = len(shared) / len(ci)
        if cov < threshold:
            continue
        # distinctive-token guard: candidate brand the child never named
        extra = [u for u in cs
                 if u not in _GEO_SOFT and not any(_tok_match(u, t) for t in ci)]
        if any(df[u] <= maxdf for u in extra):
            continue
        key = (round(cov, 6), -len(extra))
        if best_key is None or key > best_key:
            best, best_key = c, key

    if best is None:
        return None, "no-confident-match"
    return ({"uuid": best["uuid"], "name": best["name"],
             "ticker": best["ticker"], "score": round(best_key[0], 3)}, "matched")


# --- edge writers ----------------------------------------------------------

def _edge_exists(conn, rel: str, a_uuid: str, b_uuid: str) -> bool:
    return conn.execute(
        f"MATCH (a:Company {{uuid:$a}})-[r:{rel}]->(b:Company {{uuid:$b}}) "
        f"RETURN count(r)", {"a": a_uuid, "b": b_uuid}).get_next()[0] > 0


def _set_as_of(conn, edge_match: str, params: dict, as_of):
    """Set the (DATE) as_of on a just-created edge, only when present (Kuzu needs
    an explicit date() cast and can't cast NULL)."""
    if not as_of:
        return
    conn.execute(edge_match + " SET r.as_of=date($as_of)",
                 {**params, "as_of": as_of})


def _write_controls(conn, p_uuid, c_uuid, as_of) -> bool:
    """Create the CONTROLS + SUBSIDIARY_OF edges IF NOT already present.

    PROVENANCE-PRESERVING: an existing CONTROLS edge (e.g. GLEIF Level-2's
    filings-grade 0.95 link) is NOT overwritten — KAP only corroborates it. Only
    genuinely new edges are stamped with KAP provenance. Returns True if a new
    CONTROLS edge was created (False = corroborated existing)."""
    created = False
    if not _edge_exists(conn, "CONTROLS", p_uuid, c_uuid):
        conn.execute(
            """MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c})
               MERGE (p)-[r:CONTROLS]->(c)
               SET r.basis=$basis, r.source=$src, r.extraction_method=$m,
                   r.confidence=$conf""",
            {"p": p_uuid, "c": c_uuid, "basis": _BASIS, "src": _SOURCE,
             "m": _METHOD, "conf": _CONFIDENCE})
        _set_as_of(conn,
                   "MATCH (p:Company {uuid:$p})-[r:CONTROLS]->(c:Company {uuid:$c})",
                   {"p": p_uuid, "c": c_uuid}, as_of)
        created = True
    if not _edge_exists(conn, "SUBSIDIARY_OF", c_uuid, p_uuid):
        conn.execute(
            """MATCH (c:Company {uuid:$c}), (p:Company {uuid:$p})
               MERGE (c)-[r:SUBSIDIARY_OF]->(p)
               SET r.source=$src, r.extraction_method=$m, r.confidence=$conf""",
            {"c": c_uuid, "p": p_uuid, "src": _SOURCE, "m": _METHOD,
             "conf": _CONFIDENCE})
        _set_as_of(conn,
                   "MATCH (c:Company {uuid:$c})-[r:SUBSIDIARY_OF]->(p:Company {uuid:$p})",
                   {"c": c_uuid, "p": p_uuid}, as_of)
    return created


def _write_stake(conn, p_uuid, c_uuid, pct, as_of) -> bool:
    """Create a HOLDS_STAKE edge IF NOT already present (non-destructive). Returns
    True if newly created."""
    if _edge_exists(conn, "HOLDS_STAKE", p_uuid, c_uuid):
        return False
    conn.execute(
        """MATCH (p:Company {uuid:$p}), (c:Company {uuid:$c})
           MERGE (p)-[r:HOLDS_STAKE]->(c)
           SET r.pct=$pct, r.source=$src, r.extraction_method=$m,
               r.confidence=$conf""",
        {"p": p_uuid, "c": c_uuid, "pct": pct, "src": _SOURCE, "m": _METHOD,
         "conf": _CONFIDENCE})
    _set_as_of(conn,
               "MATCH (p:Company {uuid:$p})-[r:HOLDS_STAKE]->(c:Company {uuid:$c})",
               {"p": p_uuid, "c": c_uuid}, as_of)
    return True


def backfill_subsidiaries(
    conn: kuzu.Connection,
    relations: list[dict],
    threshold: float = 0.6,
    report_path: Path | str | None = None,
) -> dict:
    """Apply harvested parent->child relations to the graph. Returns a summary."""
    comps, df, by_ticker = build_company_index(conn)

    written, unmatched_child, unmatched_parent, self_links = [], [], [], 0
    controls_new = controls_corroborated = stakes_new = 0
    seen_edges: set[tuple] = set()

    for rel in relations:
        ptick = (rel.get("parent_ticker") or "").upper()
        parent = by_ticker.get(ptick)
        if parent is None:
            unmatched_parent.append({"parent_ticker": ptick,
                                     "child_name": rel.get("child_name")})
            continue
        cname = rel.get("child_name") or ""
        cm, reason = match_child(cname, comps, df, threshold=threshold)
        if cm is None:
            unmatched_child.append({"parent_ticker": ptick, "child_name": cname,
                                    "relation": rel.get("relation"),
                                    "reason": reason})
            continue
        if cm["uuid"] == parent["uuid"]:
            self_links += 1
            continue

        kind = rel.get("relation_kind")
        pct = rel.get("pct")
        as_of = rel.get("as_of")  # ISO date string or None
        key = (parent["uuid"], cm["uuid"], kind)
        if key in seen_edges:
            continue
        seen_edges.add(key)

        edge_status = "stake"
        if kind == "subsidiary":
            if _write_controls(conn, parent["uuid"], cm["uuid"], as_of):
                controls_new += 1
                edge_status = "controls-new"
            else:
                controls_corroborated += 1
                edge_status = "controls-corroborated"
        if pct is not None and _write_stake(conn, parent["uuid"], cm["uuid"], pct, as_of):
            stakes_new += 1
        written.append({
            "parent_ticker": ptick, "parent_name": parent["name"],
            "child_ticker": cm["ticker"], "child_name": cname,
            "matched_company": cm["name"], "relation": rel.get("relation"),
            "relation_kind": kind, "pct": pct, "score": cm["score"],
            "edge_status": edge_status,
        })

    summary = {
        "loader_version": LOADER_VERSION,
        "relations_in": len(relations),
        "matched": len(written),
        "controls_new": controls_new,
        "controls_corroborated": controls_corroborated,
        "holds_stake_new": stakes_new,
        "unmatched_child": len(unmatched_child),
        "unmatched_parent": len(unmatched_parent),
        "self_links_skipped": self_links,
    }
    cache_dir = Path(config.RAW_DOCS_PATH).parent / "cache"
    rp = Path(report_path) if report_path else (cache_dir / "kap_subsidiary_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "source": _SOURCE, "confidence": _CONFIDENCE, "threshold": threshold,
        "summary": summary,
        "written": written,
        "unmatched_child": unmatched_child,
        "unmatched_parent": unmatched_parent,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["report"] = str(rp)
    return summary

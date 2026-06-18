"""Phase-1 ownership/control/governance loaders.

Edges: HOLDS_STAKE, CONTROLS, SUBSIDIARY_OF, BOARD_MEMBER_OF, IN_SECTOR.
Every asserted edge carries provenance (source / extraction_method / confidence)
per ontology §0. In Phase 1 these come from fixtures; in Phase 3 they come from
LLM extraction over KAP documents — the loader signature is identical.
"""
from __future__ import annotations

import json

import kuzu

from tmkg import config


def _load():
    return json.loads((config.FIXTURES_PATH / "ownership.json").read_text(encoding="utf-8"))


def load_in_sector(conn: kuzu.Connection, rows) -> int:
    for r in rows:
        conn.execute(
            """
            MATCH (c:Company {uuid: $c}), (s:Sector {code: $s})
            MERGE (c)-[:IN_SECTOR]->(s)
            """,
            {"c": r["company"], "s": r["sector"]},
        )
    return len(rows)


def load_holds_stake(conn: kuzu.Connection, rows) -> int:
    for r in rows:
        from_label = r.get("from_label", "Company")
        conn.execute(
            f"""
            MATCH (a:{from_label} {{uuid: $f}}), (b:Company {{uuid: $t}})
            MERGE (a)-[r:HOLDS_STAKE]->(b)
            SET r.pct=$pct, r.as_of=date($as_of), r.source=$source,
                r.extraction_method=$em, r.confidence=$conf
            """,
            {"f": r["from"], "t": r["to"], "pct": r.get("pct"),
             "as_of": r.get("as_of"), "source": r.get("source"),
             "em": r.get("extraction_method"), "conf": r.get("confidence")},
        )
    return len(rows)


def load_controls(conn: kuzu.Connection, rows) -> int:
    for r in rows:
        from_label = r.get("from_label", "Company")
        conn.execute(
            f"""
            MATCH (a:{from_label} {{uuid: $f}}), (b:Company {{uuid: $t}})
            MERGE (a)-[r:CONTROLS]->(b)
            SET r.basis=$basis, r.as_of=date($as_of), r.source=$source,
                r.extraction_method=$em, r.confidence=$conf
            """,
            {"f": r["from"], "t": r["to"], "basis": r.get("basis"),
             "as_of": r.get("as_of"), "source": r.get("source"),
             "em": r.get("extraction_method"), "conf": r.get("confidence")},
        )
    return len(rows)


def load_subsidiary_of(conn: kuzu.Connection, rows) -> int:
    for r in rows:
        conn.execute(
            """
            MATCH (a:Company {uuid: $f}), (b:Company {uuid: $t})
            MERGE (a)-[r:SUBSIDIARY_OF]->(b)
            SET r.as_of=date($as_of), r.source=$source,
                r.extraction_method=$em, r.confidence=$conf
            """,
            {"f": r["from"], "t": r["to"], "as_of": r.get("as_of"),
             "source": r.get("source"), "em": r.get("extraction_method"),
             "conf": r.get("confidence")},
        )
    return len(rows)


def load_board_member_of(conn: kuzu.Connection, rows) -> int:
    for r in rows:
        conn.execute(
            """
            MATCH (p:Person {uuid: $p}), (c:Company {uuid: $c})
            MERGE (p)-[r:BOARD_MEMBER_OF]->(c)
            SET r.role=$role, r.since=date($since), r.source=$source,
                r.extraction_method=$em, r.confidence=$conf
            """,
            {"p": r["person"], "c": r["company"], "role": r.get("role"),
             "since": r.get("since"), "source": r.get("source"),
             "em": r.get("extraction_method"), "conf": r.get("confidence")},
        )
    return len(rows)


def load_all(conn: kuzu.Connection, data=None) -> dict:
    data = data if data is not None else _load()
    return {
        "in_sector": load_in_sector(conn, data.get("in_sector", [])),
        "holds_stake": load_holds_stake(conn, data.get("holds_stake", [])),
        "controls": load_controls(conn, data.get("controls", [])),
        "subsidiary_of": load_subsidiary_of(conn, data.get("subsidiary_of", [])),
        "board_member_of": load_board_member_of(conn, data.get("board_member_of", [])),
    }

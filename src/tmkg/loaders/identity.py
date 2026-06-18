"""Phase-1 identity-spine loaders: Company, Person, Security, Sector, Portfolio.

Idempotent MERGE-style upserts keyed on the internal uuid (or natural PK).
Source data is normalized dicts — from `fixtures/` in Phase 1, or from the KAP
adapter + GLEIF in later phases. Loaders do not care which.
"""
from __future__ import annotations

import json
from pathlib import Path

import kuzu

from tmkg import config


def _load_json(name: str):
    return json.loads((config.FIXTURES_PATH / name).read_text(encoding="utf-8"))


def _b(v) -> bool:
    return bool(v) if v is not None else False


def load_companies(conn: kuzu.Connection, rows=None) -> int:
    rows = rows if rows is not None else _load_json("companies.json")
    for r in rows:
        conn.execute(
            """
            MERGE (c:Company {uuid: $uuid})
            SET c.lei=$lei, c.isin=$isin, c.kap_oid=$kap_oid, c.ticker=$ticker,
                c.name=$name, c.legal_form=$legal_form, c.jurisdiction=$jurisdiction,
                c.registration_authority=$ra, c.is_listed=$is_listed,
                c.is_pep=$is_pep, c.is_sanctioned=$is_sanctioned
            """,
            {
                "uuid": r["uuid"], "lei": r.get("lei"), "isin": r.get("isin"),
                "kap_oid": r.get("kap_oid"), "ticker": r.get("ticker"),
                "name": r.get("name"), "legal_form": r.get("legal_form"),
                "jurisdiction": r.get("jurisdiction"),
                "ra": r.get("registration_authority"),
                "is_listed": _b(r.get("is_listed")),
                "is_pep": _b(r.get("is_pep")), "is_sanctioned": _b(r.get("is_sanctioned")),
            },
        )
    return len(rows)


def load_people(conn: kuzu.Connection, rows=None) -> int:
    rows = rows if rows is not None else _load_json("people.json")
    for r in rows:
        conn.execute(
            """
            MERGE (p:Person {uuid: $uuid})
            SET p.name=$name, p.is_pep=$is_pep, p.is_sanctioned=$is_sanctioned
            """,
            {"uuid": r["uuid"], "name": r.get("name"),
             "is_pep": _b(r.get("is_pep")), "is_sanctioned": _b(r.get("is_sanctioned"))},
        )
    return len(rows)


def load_securities(conn: kuzu.Connection, rows=None) -> int:
    """Creates Security nodes and the ISSUES edge from issuer Company."""
    rows = rows if rows is not None else _load_json("securities.json")
    for r in rows:
        conn.execute(
            """
            MERGE (s:Security {uuid: $uuid})
            SET s.isin=$isin, s.ticker=$ticker, s.type=$type, s.currency=$currency
            """,
            {"uuid": r["uuid"], "isin": r.get("isin"), "ticker": r.get("ticker"),
             "type": r.get("type"), "currency": r.get("currency")},
        )
        if r.get("issuer"):
            conn.execute(
                """
                MATCH (c:Company {uuid: $issuer}), (s:Security {uuid: $sid})
                MERGE (c)-[:ISSUES]->(s)
                """,
                {"issuer": r["issuer"], "sid": r["uuid"]},
            )
    return len(rows)


def load_sectors(conn: kuzu.Connection, rows=None) -> int:
    rows = rows if rows is not None else _load_json("sectors.json")
    for r in rows:
        conn.execute(
            "MERGE (s:Sector {code: $code}) SET s.name=$name",
            {"code": r["code"], "name": r.get("name")},
        )
    return len(rows)


def load_portfolio(conn: kuzu.Connection, data=None) -> int:
    """Creates the Portfolio node and HOLDS edges to held Securities."""
    data = data if data is not None else _load_json("portfolio.json")
    conn.execute(
        "MERGE (p:Portfolio {uuid: $uuid}) SET p.name=$name",
        {"uuid": data["uuid"], "name": data.get("name")},
    )
    n = 0
    for h in data.get("holdings", []):
        conn.execute(
            """
            MATCH (p:Portfolio {uuid: $pf}), (s:Security {uuid: $sid})
            MERGE (p)-[r:HOLDS]->(s)
            SET r.weight=$weight, r.qty=$qty
            """,
            {"pf": data["uuid"], "sid": h["security"],
             "weight": h.get("weight"), "qty": h.get("qty")},
        )
        n += 1
    return n

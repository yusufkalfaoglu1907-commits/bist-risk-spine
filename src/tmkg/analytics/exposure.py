"""Phase-1 exit-test analytics: aggregated exposure to a controlling group.

Question (architecture §6): "What is my aggregated exposure to the Koç group
across my holdings, weighted by stake?"

Approach:
  Portfolio -HOLDS-> Security <-ISSUES- Company (the names you hold).
  A holding is "in the group" if the group root controls it, i.e. there is a
  directed control chain
        (root) -CONTROLS-> ... -CONTROLS-> (company)        [ownership goes down]
  or, equivalently, the company sits in a subsidiary chain up to the root
        (company) -SUBSIDIARY_OF-> ... -> (root)
  or the company *is* the root.

Membership is computed per holding with explicit existence/shortest-path
queries — this avoids OPTIONAL-MATCH null-flag pitfalls and naturally
deduplicates when multiple control paths exist.
"""
from __future__ import annotations

import kuzu


def _min_hops(conn: kuzu.Connection, root: str, company: str, max_hops: int):
    """Shortest control distance from root down to company, or None."""
    if company == root:
        return 0
    best = None
    # root controls company (ownership direction: root -> company)
    res = conn.execute(
        f"""
        MATCH p = (root:Company {{uuid: $root}})-[:CONTROLS*1..{max_hops}]->(c:Company {{uuid: $c}})
        RETURN min(length(p))
        """,
        {"root": root, "c": company},
    )
    v = res.get_next()[0]
    if v is not None:
        best = int(v)
    # company is a subsidiary up-chain to root
    res = conn.execute(
        f"""
        MATCH p = (c:Company {{uuid: $c}})-[:SUBSIDIARY_OF*1..{max_hops}]->(root:Company {{uuid: $root}})
        RETURN min(length(p))
        """,
        {"root": root, "c": company},
    )
    v = res.get_next()[0]
    if v is not None:
        best = int(v) if best is None else min(best, int(v))
    return best


def group_exposure(
    conn: kuzu.Connection,
    portfolio_uuid: str,
    group_root_uuid: str,
    max_hops: int = 4,
) -> list[dict]:
    """One row per holding: ticker, name, portfolio weight, group membership,
    and the shortest control distance to the group root."""
    res = conn.execute(
        """
        MATCH (pf:Portfolio {uuid: $pf})-[h:HOLDS]->(s:Security)<-[:ISSUES]-(c:Company)
        RETURN DISTINCT c.uuid AS uuid, c.ticker AS ticker, c.name AS name, h.weight AS weight
        ORDER BY weight DESC
        """,
        {"pf": portfolio_uuid},
    )
    holdings = []
    while res.has_next():
        uuid, ticker, name, weight = res.get_next()
        holdings.append({"uuid": uuid, "ticker": ticker, "name": name, "weight": weight})

    rows = []
    for hld in holdings:
        hops = _min_hops(conn, group_root_uuid, hld["uuid"], max_hops)
        rows.append({
            "ticker": hld["ticker"], "name": hld["name"], "weight": hld["weight"],
            "in_group": hops is not None,
            "control_hops": hops,
        })
    rows.sort(key=lambda r: (not r["in_group"], -r["weight"]))
    return rows


def total_group_weight(rows: list[dict]) -> float:
    """Sum of portfolio weights for holdings inside the group."""
    return round(sum(r["weight"] for r in rows if r["in_group"]), 4)

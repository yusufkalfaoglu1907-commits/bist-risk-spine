"""Multi-hop blast-radius analytics over CONTROLS.

Question: a single subsidiary faces a refinancing wall (a cluster of debt
maturing inside some window). How far does that shock reach through the
*control* structure of its group? Which sibling/parent entities share the
group, and what does the group's combined maturity wall look like over the
same window?

Mechanics
---------
1. Resolve the group from a seed company by walking *up* the control graph
   (CONTROLS reversed, or SUBSIDIARY_OF forward — the GLEIF-L2 backfill writes
   both as inverses) to the apex holdco(s).
2. Fan *down* from the apex over CONTROLS*1..N to the full member set — the
   blast radius. The seed itself and the apex are always included.
3. For every member, compute its refinancing wall inside [start, end] from the
   debt instruments it ISSUES (Security.maturity_date), broken out by
   instrument_class and currency.
4. Aggregate to a group-level contagion figure and rank members by exposure.

IMPORTANT LIMITATION — counts, not money.
-----------------------------------------
The Security node carries NO notional / face-value field, so "blast radius" is
measured in *instrument counts and maturity timing*, never in currency amounts.
A member rolling one EUR500m eurobond and one rolling a TRY10m bill both show
"1 instrument". Treat the output as a map of *where* refinancing pressure
clusters, not *how much* is at risk. Adding a notional column to the debt
adapter is the obvious upgrade.

SECOND LIMITATION — control-graph coverage.
-------------------------------------------
CONTROLS edges exist only for the ~62 debt issuers GLEIF Level-2 links to an
in-universe parent. ~134 of 196 debt issuers (the standalone finance SPVs) have
no control edge at all, so their walls are invisible to any group rooted
elsewhere. Group totals are therefore *lower bounds* on true contagion.
"""
from __future__ import annotations

import datetime as _dt
import heapq
import json
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.analytics.outstanding import (
    outstanding_as_of, CONFIDENT_BASES, UPPER_BOUND_BASES,
)
from tmkg.schema.integrity import strongly_connected_components

# Banner the audit mandated until provenance/refusal logic ships everywhere: a
# group whose totals are not assembled must say so on every result.
COVERAGE_BANNER = "counts are meaningful; totals are not"
# A group total reported as a confident headline only at/above this priced share.
_PARTIAL_COVERAGE_THRESHOLD = 0.50


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def _norm(s: str | None) -> str:
    return (s or "").strip().upper()


def load_ingest_context(reports_dir: Path | str | None = None) -> dict:
    """Read the run reports that say what ingest excluded / could not root (F1/F2).

    Tolerant: missing reports yield empty flags (analytics can run on a graph
    built without them). Returns the unmatched-issuer + no-in-graph-parent
    indexes used to flag a seed, and the honest excluded-at-ingest count.
    """
    base = Path(reports_dir) if reports_dir else (config.RAW_DOCS_PATH.parent / "cache")
    debt = _read_json(base / "mkk_debt_report.json") or {}
    spv = _read_json(base / "spv_parent_report.json") or {}
    summary = debt.get("summary") or {}
    ref_n = debt.get("reference_securities")
    written = summary.get("securities_written")
    excluded = (ref_n - written) if (ref_n is not None and written is not None) else None
    return {
        "excluded_at_ingest": excluded,
        "reference_securities": ref_n,
        "securities_written": written,
        "unmatched_issuer_names": {
            _norm(u.get("issuer_name")) for u in (debt.get("unmatched") or [])
            if u.get("issuer_name")
        },
        "no_parent_tickers": {
            _norm(r.get("spv")) for r in (spv.get("no_in_graph_parent") or [])
            if r.get("spv")
        },
        "debt_report": "mkk_debt_report.json",
        "spv_report": "spv_parent_report.json",
    }


def _seed_report_flags(
    ctx: dict, seed_ticker: str | None, seed_name: str | None
) -> tuple[bool, bool]:
    """(in_unmatched_debt, in_no_in_graph_parent) for the seed, from the reports."""
    name = _norm(seed_name)
    ticker = _norm(seed_ticker)
    in_unmatched = any(
        u and (u in name or u == ticker) for u in ctx["unmatched_issuer_names"]
    )
    in_no_parent = bool(ticker) and ticker in ctx["no_parent_tickers"]
    return in_unmatched, in_no_parent


# --- group resolution ------------------------------------------------------

def _load_parent_map(conn: kuzu.Connection) -> dict[str, set[str]]:
    """child_uuid -> set(parent_uuid), over BOTH control sources.

    Upward control comes from two edge types written as inverses by different
    loaders: CONTROLS (parent->child, so reversed here) and SUBSIDIARY_OF
    (child->parent, taken as-is). Folding SUBSIDIARY_OF in is the F6 fix — the
    KAP-only SUBSIDIARY_OF edges have no mirror CONTROLS edge, so rooting that
    walked CONTROLS alone was blind to them (the docstring claimed otherwise).
    """
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


# --- provenance tiers (F7) -------------------------------------------------
#
# Every control edge carries a `basis`; that basis maps to a provenance tier
# describing how the edge was established, worst-to-best:
#   inference_attached  – heuristic, not a filing (SPV naming convention)
#   kap_declared        – issuer's own KAP "bağlı ortaklık" disclosure
#   gleif_confirmed     – GLEIF Level-2 accounting-consolidation filing
# A member inherits the WORST tier on its path from the root (a single inferred
# hop makes the whole attachment inferred), and the highest confidence product
# achievable at that tier. `group_root` is the apex itself — in the group by
# definition, no attachment to question.
_TIER_RANK = {"inference_attached": 1, "kap_declared": 2, "gleif_confirmed": 3}
_TIER_BY_RANK = {v: k for k, v in _TIER_RANK.items()}
_ROOT_RANK = max(_TIER_RANK.values()) + 1   # sentinel: root constrains nothing


def basis_to_tier(basis: str | None) -> str:
    """Map a CONTROLS.basis to its provenance tier name (F7)."""
    if basis in ("direct-consolidation", "ultimate-consolidation"):
        return "gleif_confirmed"
    if basis == "kap-bagli-ortaklik":
        return "kap_declared"
    if basis in ("spv-naming-convention", "spv-naming-convention-jv-suspect"):
        return "inference_attached"
    return "unknown"


def _load_controls_meta(conn: kuzu.Connection) -> dict[str, list[tuple[str, str | None, float]]]:
    """controller -> [(controlled, basis, confidence)] over CONTROLS."""
    succ: dict[str, list[tuple[str, str | None, float]]] = {}
    r = conn.execute(
        "MATCH (p:Company)-[e:CONTROLS]->(c:Company) "
        "RETURN p.uuid, c.uuid, e.basis, e.confidence")
    while r.has_next():
        p, c, basis, conf = r.get_next()
        succ.setdefault(p, []).append(
            (c, basis, 1.0 if conf is None else float(conf)))
    return succ


def member_provenance(
    conn: kuzu.Connection, root_uuid: str, member_uuids: list[str]
) -> dict[str, tuple[str | None, float | None]]:
    """Per-member (provenance_tier, min_path_confidence) from the root (F7).

    A widest-path (max-min) search: each member is reached on the path whose
    WEAKEST edge-tier is as strong as possible, breaking ties by the highest
    confidence product. So a member reachable by an all-GLEIF chain is
    `gleif_confirmed` even if a flimsier alternate path also exists.
    """
    succ = _load_controls_meta(conn)
    want = set(member_uuids)
    # best[node] = (bottleneck_rank, confidence_product), maximized lexicographically
    best: dict[str, tuple[int, float]] = {root_uuid: (_ROOT_RANK, 1.0)}
    pq: list[tuple[int, float, str]] = [(-_ROOT_RANK, -1.0, root_uuid)]
    while pq:
        nb, nc, u = heapq.heappop(pq)
        if best.get(u) != (-nb, -nc):
            continue                                   # stale heap entry
        b_rank, b_conf = best[u]
        for v, basis, conf in succ.get(u, ()):
            te = _TIER_RANK.get(basis_to_tier(basis), 0)
            cand = (min(b_rank, te), b_conf * conf)
            if cand > best.get(v, (-1, -1.0)):
                best[v] = cand
                heapq.heappush(pq, (-cand[0], -cand[1], v))

    out: dict[str, tuple[str | None, float | None]] = {}
    for u in want:
        if u == root_uuid:
            out[u] = ("group_root", 1.0)
            continue
        lbl = best.get(u)
        if lbl is None:
            out[u] = ("unknown", None)
        else:
            rank, conf = lbl
            out[u] = (_TIER_BY_RANK.get(rank, "unknown"), round(conf, 4))
    return out


def resolve_group_root(
    conn: kuzu.Connection, seed_uuid: str, max_hops: int = 6
) -> list[dict]:
    """Apex holdco(s) above `seed` — SCC-aware, cycle-safe (F6).

    Walks upward via CONTROLS (reversed) AND SUBSIDIARY_OF. An apex is the top of
    the control structure: a strongly connected component with no parent edge
    leaving it. Collapsing SCCs first means an ownership cycle (which the audit
    injected to break the old "ancestor with no parent" rule — in a cycle every
    node has a parent, so NO apex was found and rooting silently fell back to the
    seed) resolves to the cycle itself as the apex rather than collapsing the
    group. If the seed is uncontrolled it is its own root. Returns one row per
    apex, nearest first (shortest upward hop count from the seed).
    """
    parent_of = _load_parent_map(conn)

    # SCCs over the full control graph (child -> parent), so a cycle becomes one
    # super-node. comp_of maps a uuid to its component id.
    all_nodes: set[str] = set(parent_of)
    for ps in parent_of.values():
        all_nodes |= ps
    all_nodes.add(seed_uuid)
    up_succ = {n: list(parent_of.get(n, ())) for n in all_nodes}
    comps = strongly_connected_components(all_nodes, up_succ)
    comp_of = {n: i for i, comp in enumerate(comps) for n in comp}

    # A component is a "root SCC" (apex-eligible) when every parent of every
    # member lands back inside the same component — no external controller.
    root_comp = {
        i: all(comp_of[p] == i for n in comp for p in parent_of.get(n, ()))
        for i, comp in enumerate(comps)
    }

    # BFS upward from the seed (bounded), recording the shortest hop to each node.
    hops = {seed_uuid: 0}
    frontier = [seed_uuid]
    depth = 0
    while frontier and depth < max_hops:
        depth += 1
        nxt: list[str] = []
        for n in frontier:
            for p in parent_of.get(n, ()):
                if p not in hops:
                    hops[p] = depth
                    nxt.append(p)
        frontier = nxt

    # Apex = nearest reached member of each reachable root SCC (one rep per SCC).
    best_by_comp: dict[int, str] = {}
    for n in hops:
        ci = comp_of[n]
        if not root_comp.get(ci):
            continue
        cur = best_by_comp.get(ci)
        if cur is None or (hops[n], n) < (hops[cur], cur):
            best_by_comp[ci] = n
    reps = list(best_by_comp.values())

    if not reps:
        # apex sits beyond max_hops (or graph quirk) — seed is its own apex
        reps = [seed_uuid]
        hops.setdefault(seed_uuid, 0)

    names = _fetch_company_labels(conn, reps)
    roots = [
        {"uuid": u, "ticker": names.get(u, (None, None))[0],
         "name": names.get(u, (None, None))[1], "hops_from_seed": hops[u]}
        for u in reps
    ]
    return sorted(roots, key=lambda d: (d["hops_from_seed"], d["uuid"]))


def _fetch_company_labels(
    conn: kuzu.Connection, uuids: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """uuid -> (ticker, name) for a set of companies, in one query."""
    if not uuids:
        return {}
    r = conn.execute(
        "MATCH (c:Company) WHERE c.uuid IN $L RETURN c.uuid, c.ticker, c.name",
        {"L": uuids},
    )
    out: dict[str, tuple[str | None, str | None]] = {}
    while r.has_next():
        u, t, n = r.get_next()
        out[u] = (t, n)
    return out


def group_members(
    conn: kuzu.Connection, root_uuid: str, max_hops: int = 6
) -> list[dict]:
    """Every company the apex controls (CONTROLS*1..N down), plus the apex.

    `control_hops` is the shortest control distance from the root over DIRECT
    edges only — paths that traverse an `ultimate-consolidation` shortcut are
    excluded from the hop count (F4). GLEIF Level-2 writes both a real
    parent->child chain (`direct-consolidation`) AND a shortcut from the ultimate
    parent straight to each grandchild (`ultimate-consolidation`); counting the
    shortcut understated ownership depth (KCHOL->YKR looked 1 hop, not 2). The
    *membership* set is unchanged — it is the full CONTROLS reach, shortcuts and
    all — so a member reachable ONLY via a shortcut still appears, falling back to
    its any-basis hop count.
    """
    members: dict[str, dict] = {}
    r = conn.execute(
        "MATCH (root:Company {uuid:$r}) RETURN root.uuid, root.ticker, root.name",
        {"r": root_uuid},
    )
    if r.has_next():
        uuid, ticker, name = r.get_next()
        members[uuid] = {"uuid": uuid, "ticker": ticker, "name": name,
                         "control_hops": 0}

    # Membership + any-basis hop (the fallback for shortcut-only members).
    res = conn.execute(
        f"""
        MATCH path = (root:Company {{uuid: $r}})-[:CONTROLS*1..{max_hops}]->(m:Company)
        RETURN m.uuid AS uuid, m.ticker AS ticker, m.name AS name,
               min(length(path)) AS hops
        """,
        {"r": root_uuid},
    )
    while res.has_next():
        uuid, ticker, name, hops = res.get_next()
        if uuid in members:
            continue
        members[uuid] = {"uuid": uuid, "ticker": ticker, "name": name,
                         "control_hops": int(hops)}

    # Direct-only hop: shortest path using no ultimate-consolidation shortcut.
    res = conn.execute(
        f"""
        MATCH path = (root:Company {{uuid: $r}})-[:CONTROLS*1..{max_hops}]->(m:Company)
        WHERE ALL(rel IN rels(path)
                  WHERE rel.basis IS NULL OR rel.basis <> 'ultimate-consolidation')
        RETURN m.uuid AS uuid, min(length(path)) AS hops
        """,
        {"r": root_uuid},
    )
    while res.has_next():
        uuid, hops = res.get_next()
        if uuid in members and uuid != root_uuid:
            members[uuid]["control_hops"] = int(hops)

    # Per-member provenance tier + path confidence (F7).
    prov = member_provenance(conn, root_uuid, list(members))
    for uuid, m in members.items():
        tier, conf = prov.get(uuid, ("unknown", None))
        m["provenance_tier"] = tier
        m["min_path_confidence"] = conf

    return sorted(members.values(), key=lambda d: (d["control_hops"], d["ticker"] or ""))


# --- refinancing wall ------------------------------------------------------

def refinancing_wall(
    conn: kuzu.Connection,
    company_uuid: str,
    window_start: _dt.date,
    window_end: _dt.date,
    as_of: _dt.date | None = None,
) -> dict:
    """Debt this company ISSUES maturing within [window_start, window_end].

    Beyond counts/class/currency, reports the *outstanding* amount as of `as_of`
    (default = window_start), computed per instrument via
    ``outstanding.outstanding_as_of``: a live bullet contributes its nominal
    exactly; an amortizer contributes its nominal as an UPPER BOUND (surfaced
    separately, never folded into the confident total); a matured instrument
    contributes 0. So a graph queried later auto-drops paper that has rolled off,
    with no re-fetch.

    Keys:
      outstanding_by_currency        – confident sum (live bullets) per currency
      outstanding_upper_by_currency  – amortizing/unknown live paper (upper bound)
      priced_instruments / nominal_coverage – pricing coverage of the wall
      matured_in_window              – instruments already matured as of as_of
    """
    as_of = as_of or window_start
    res = conn.execute(
        """
        MATCH (c:Company {uuid: $c})-[i:ISSUES]->(s:Security)
        WHERE s.maturity_date IS NOT NULL
          AND s.maturity_date >= $wstart AND s.maturity_date <= $wend
        RETURN i.instrument_class AS cls, s.currency AS ccy,
               s.maturity_date AS mat, s.nominal AS nom,
               s.nominal_currency AS nccy, s.is_amortizing AS amort,
               s.nominal_basis AS nbasis
        """,
        {"c": company_uuid, "wstart": window_start, "wend": window_end},
    )
    by_class: dict[str, int] = {}
    by_ccy: dict[str, int] = {}
    out_conf: dict[str, float] = {}
    out_upper: dict[str, float] = {}
    total = 0
    priced = 0
    matured = 0
    earliest: _dt.date | None = None
    while res.has_next():
        cls, ccy, mat, nom, nccy, amort, nbasis = res.get_next()
        total += 1
        by_class[cls or "UNKNOWN"] = by_class.get(cls or "UNKNOWN", 0) + 1
        by_ccy[ccy or "UNKNOWN"] = by_ccy.get(ccy or "UNKNOWN", 0) + 1
        if mat is not None and (earliest is None or mat < earliest):
            earliest = mat
        amt, basis = outstanding_as_of(nom, mat, as_of, amort, cls, nbasis)
        k = nccy or ccy or "UNKNOWN"
        if basis == "matured":
            matured += 1
        elif basis in CONFIDENT_BASES:
            priced += 1
            out_conf[k] = out_conf.get(k, 0.0) + amt
        elif basis in UPPER_BOUND_BASES:
            priced += 1
            out_upper[k] = out_upper.get(k, 0.0) + amt
        # 'unpriced' → contributes only to the coverage gap
    return {
        "instruments": total,
        "by_class": by_class,
        "by_currency": by_ccy,
        "earliest_maturity": earliest,
        "as_of": as_of,
        "outstanding_by_currency": out_conf,
        "outstanding_upper_by_currency": out_upper,
        "priced_instruments": priced,
        "matured_in_window": matured,
        "nominal_coverage": round(priced / total, 3) if total else 0.0,
        # back-compat alias: confident outstanding (bullets) by currency
        "nominal_by_currency": out_conf,
    }


# --- top-level blast-radius query -----------------------------------------

def group_blast_radius(
    conn: kuzu.Connection,
    seed_uuid: str,
    window_start: _dt.date,
    window_end: _dt.date,
    max_hops: int = 6,
    as_of: _dt.date | None = None,
    reports_dir: Path | str | None = None,
) -> dict:
    """Group-level contagion from one subsidiary's refinancing wall.

    Resolves the seed's apex, fans out to the group, and totals every member's
    refinancing wall in the window. Returns a structured report:

        {
          seed:   {uuid, ticker, name, wall},
          roots:  [apex holdco(s)],
          root_used: <uuid the fan-out is rooted at>,
          window: (start, end),
          coverage: {coverage_class, seed_control_edges, nominal_coverage,
                     excluded_at_ingest, banner?, ...},   # F1/F2/F5
          members: [ {uuid, ticker, name, control_hops, provenance_tier, wall}, ... ],
          group_total: {instruments, by_class, by_currency, members_with_wall, ...},
        }

    Refusal logic (F1/F5) — a group must not present totals it cannot stand behind:
      - coverage_class 'blind'  (seed isolated, or flagged unmatched/no-parent in
        the run reports): NO money in group_total — counts + currency mix only.
        Converts the "confident emptiness" failure into an honest non-answer.
      - coverage_class 'partial' (priced share < 50%): money moves under
        group_total['partial_totals'] with the unpriced count adjacent — never a
        headline figure (the ₺16.9bn-anchor failure).
      - coverage_class 'assembled' (priced share >= 50%): money reported inline.
    `coverage.banner` carries "counts are meaningful; totals are not" unless the
    group is fully assembled.

    When the seed sits under several independent apexes the nearest is used for
    the fan-out; all candidates are returned under `roots` for inspection.
    """
    seed = conn.execute(
        "MATCH (s:Company {uuid:$s}) RETURN s.uuid, s.ticker, s.name", {"s": seed_uuid}
    )
    if not seed.has_next():
        raise ValueError(f"seed company {seed_uuid!r} not found")
    s_uuid, s_ticker, s_name = seed.get_next()

    _ec = conn.execute(
        "MATCH (s:Company {uuid:$s}) "
        "OPTIONAL MATCH (s)-[o:CONTROLS]->(:Company) "
        "OPTIONAL MATCH (:Company)-[i:CONTROLS]->(s) "
        "RETURN count(DISTINCT o) + count(DISTINCT i), count(DISTINCT i)",
        {"s": s_uuid}).get_next()
    seed_edges = _ec[0]
    seed_parent_edges = _ec[1]   # incoming CONTROLS = the seed has a parent in-graph

    roots = resolve_group_root(conn, seed_uuid, max_hops)
    root_used = roots[0]["uuid"] if roots else seed_uuid
    members = group_members(conn, root_used, max_hops)

    g_class: dict[str, int] = {}
    g_ccy: dict[str, int] = {}
    g_out: dict[str, float] = {}
    g_out_upper: dict[str, float] = {}
    g_prov: dict[str, dict] = {}        # provenance-tier split (F7)
    g_total = 0
    g_priced = 0
    g_matured = 0
    members_with_wall = 0
    for m in members:
        m["wall"] = refinancing_wall(conn, m["uuid"], window_start, window_end, as_of)
        n = m["wall"]["instruments"]
        if n:
            members_with_wall += 1
            g_total += n
            g_priced += m["wall"]["priced_instruments"]
            g_matured += m["wall"]["matured_in_window"]
            for k, v in m["wall"]["by_class"].items():
                g_class[k] = g_class.get(k, 0) + v
            for k, v in m["wall"]["by_currency"].items():
                g_ccy[k] = g_ccy.get(k, 0) + v
            for k, v in m["wall"]["outstanding_by_currency"].items():
                g_out[k] = g_out.get(k, 0.0) + v
            for k, v in m["wall"]["outstanding_upper_by_currency"].items():
                g_out_upper[k] = g_out_upper.get(k, 0.0) + v
            # split the same wall into its provenance bucket (worst-edge tier)
            tier = m.get("provenance_tier") or "unknown"
            b = g_prov.setdefault(tier, {
                "instruments": 0, "members": 0,
                "outstanding_by_currency": {}, "outstanding_upper_by_currency": {}})
            b["instruments"] += n
            b["members"] += 1
            for k, v in m["wall"]["outstanding_by_currency"].items():
                b["outstanding_by_currency"][k] = b["outstanding_by_currency"].get(k, 0.0) + v
            for k, v in m["wall"]["outstanding_upper_by_currency"].items():
                b["outstanding_upper_by_currency"][k] = \
                    b["outstanding_upper_by_currency"].get(k, 0.0) + v

    seed_wall = next(
        (m["wall"] for m in members if m["uuid"] == s_uuid),
        refinancing_wall(conn, s_uuid, window_start, window_end, as_of),
    )

    # --- coverage preamble + refusal logic (F1/F2/F5) ----------------------
    ctx = load_ingest_context(reports_dir)
    in_unmatched, in_no_parent = _seed_report_flags(ctx, s_ticker, s_name)
    nominal_coverage = round(g_priced / g_total, 3) if g_total else 0.0

    # 'blind' — the group cannot be assembled around this seed: it is flagged in
    # the run reports (its own debt was excluded at ingest / it has no in-graph
    # parent) or it is structurally isolated (alone, no control edge). The
    # no-in-graph-parent signal is *pre-widening*: once a stub (F3) or any parent
    # gives the seed an incoming control edge it is no longer parentless, so that
    # flag only forces 'blind' when the seed genuinely has no parent edge.
    parentless = in_no_parent and seed_parent_edges == 0
    isolated = (len(members) == 1 and seed_edges == 0)
    if in_unmatched or parentless or isolated:
        coverage_class = "blind"
    elif nominal_coverage < _PARTIAL_COVERAGE_THRESHOLD:
        coverage_class = "partial"
    else:
        coverage_class = "assembled"

    unpriced = g_total - g_priced
    coverage = {
        "coverage_class": coverage_class,
        "seed_control_edges": int(seed_edges),
        "seed_in_unmatched_debt": in_unmatched,
        "seed_in_no_in_graph_parent": parentless,
        "nominal_coverage": nominal_coverage,
        "instruments_priced": g_priced,
        "instruments_unpriced": unpriced,
        "excluded_at_ingest": ctx["excluded_at_ingest"],
        "excluded_at_ingest_note": (
            f"instruments excluded at ingest: {ctx['excluded_at_ingest']} "
            f"(see {ctx['debt_report']})"
            if ctx["excluded_at_ingest"] is not None else None
        ),
    }
    if coverage_class != "assembled":
        coverage["banner"] = COVERAGE_BANNER

    # group_total always carries counts (always meaningful); money placement
    # depends on coverage_class per the refusal rules above.
    group_total: dict = {
        "instruments": g_total,
        "by_class": g_class,
        "by_currency": g_ccy,
        "matured_in_window": g_matured,
        "members_with_wall": members_with_wall,
        "members_total": len(members),
        "coverage_class": coverage_class,
    }
    money = {
        "outstanding_by_currency": g_out,
        "outstanding_upper_by_currency": g_out_upper,
        "priced_instruments": g_priced,
        "nominal_coverage": nominal_coverage,
        "by_provenance_tier": g_prov,
        # back-compat alias: confident outstanding (bullets) by currency
        "nominal_by_currency": g_out,
    }
    if coverage_class == "blind":
        group_total["totals_suppressed"] = (
            "blind: control-graph coverage insufficient — counts only, no group total"
        )
    elif coverage_class == "partial":
        group_total["partial_totals"] = {**money, "instruments_unpriced": unpriced}
    else:                                            # assembled
        group_total.update(money)

    return {
        "seed": {"uuid": s_uuid, "ticker": s_ticker, "name": s_name, "wall": seed_wall},
        "roots": roots,
        "root_used": root_used,
        "window": (window_start, window_end),
        "as_of": as_of or window_start,
        "coverage": coverage,
        "members": sorted(
            members, key=lambda m: (-m["wall"]["instruments"], m["control_hops"])
        ),
        "group_total": group_total,
    }

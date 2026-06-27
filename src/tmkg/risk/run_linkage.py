"""Linkage-shock runner (M8.2) — read the ownership/control graph and propagate an idiosyncratic shock.

The one graph-touching piece of the linkage risk tool. The propagation math lives in the pure
``linkage_propagation`` module; this reads the L1 Kuzu edges and orchestrates:

  1. read ``HOLDS_STAKE`` (Company→Company, pct + confidence) and ``CONTROLS`` (the verified DAG) —
     structural identity/ownership, read directly like ``pit.idbridge`` (not a time-varying signal
     read; uses the latest-known ownership snapshot — PIT-as-of ownership is a future enhancement);
  2. normalise ``pct`` (0..100) → fraction (0..1) explicitly;
  3. propagate the origin shocks up the ownership chains (magnitude) + compute each origin's CONTROLS
     blast-radius (reachability);
  4. write the §4 report.

A **risk** tool — no Sharpe, no gate, no ``signal_registry`` write. Edges are injectable (``edges=``)
so the self-test runs with no graph. Reads L1 only; never the network (§4).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from tmkg.risk.linkage_propagation import (
    OwnershipEdge,
    control_blast_radius,
    propagate_ownership_shock,
)


def read_ownership_edges(con, *, listed_only: bool = False) -> list[OwnershipEdge]:
    """Read HOLDS_STAKE Company→Company edges, normalising pct (0..100) → fraction (0..1)."""
    where = "s.pct IS NOT NULL"
    if listed_only:
        where += " AND a.ticker IS NOT NULL AND b.ticker IS NOT NULL"
    res = con.execute(
        f"MATCH (a:Company)-[s:HOLDS_STAKE]->(b:Company) WHERE {where} "
        "RETURN a.ticker, a.name, b.ticker, b.name, s.pct, s.confidence"
    )
    edges: list[OwnershipEdge] = []
    while res.has_next():
        a_tk, a_nm, b_tk, b_nm, pct, conf = res.get_next()
        holder = a_tk or a_nm
        held = b_tk or b_nm
        if holder is None or held is None or pct is None:
            continue
        edges.append(OwnershipEdge(holder=holder, held=held,
                                   fraction=float(pct) / 100.0,
                                   confidence=float(conf) if conf is not None else 1.0))
    return edges


def read_controls_edges(con, *, listed_only: bool = False) -> list[tuple[str, str]]:
    """Read CONTROLS (controller, controlled) Company→Company pairs."""
    where = "a.ticker IS NOT NULL AND b.ticker IS NOT NULL" if listed_only else "true"
    res = con.execute(
        f"MATCH (a:Company)-[c:CONTROLS]->(b:Company) WHERE {where} "
        "RETURN coalesce(a.ticker, a.name), coalesce(b.ticker, b.name)"
    )
    out: list[tuple[str, str]] = []
    while res.has_next():
        controller, controlled = res.get_next()
        if controller and controlled:
            out.append((controller, controlled))
    return out


def run_linkage_shock(
    con=None,
    *,
    shocks: Mapping[str, float],
    edges: list[OwnershipEdge] | None = None,
    controls_edges: list[tuple[str, str]] | None = None,
    listed_only: bool = False,
    min_confidence: float = 0.0,
    max_hops: int = 6,
    report_dir: str | Path | None = None,
    report_name: str = "m8_linkage_shock_report",
) -> dict:
    """Propagate ``shocks`` (origin name → signed idiosyncratic magnitude) through the ownership graph
    and compute each origin's CONTROLS blast-radius.

    ``edges`` / ``controls_edges`` may be injected (self-test); otherwise read from ``con`` (a Kuzu
    connection). Returns the report dict and, if ``report_dir``, writes the §4 JSON."""
    if not shocks:
        raise ValueError("run_linkage_shock needs a non-empty shock vector")
    if edges is None:
        if con is None:
            raise ValueError("run_linkage_shock needs either `con` or injected `edges`")
        edges = read_ownership_edges(con, listed_only=listed_only)
    if controls_edges is None and con is not None:
        controls_edges = read_controls_edges(con, listed_only=listed_only)
    controls_edges = controls_edges or []

    prop = propagate_ownership_shock(edges, shocks, max_hops=max_hops, min_confidence=min_confidence)
    blast = {origin: control_blast_radius(controls_edges, origin) for origin in shocks}

    report = {
        "milestone": "M8",
        "tool": "linkage_shock_propagation",
        "report": report_name,
        "note": ("risk re-pricing through the ownership/control graph, not alpha — structural "
                 "look-through exposure (who is mechanically exposed), NOT a linked-firm co-move "
                 "prediction (that was NO-GO, ADR-0006). No Sharpe / gate / registry write."),
        "shocks": {k: float(v) for k, v in shocks.items()},
        "ownership": {
            "n_edges": len(edges),
            "listed_only": listed_only,
            "min_confidence": min_confidence,
            **prop.summary(),
            "paths": prop.paths[:200],
        },
        "control_blast_radius": {
            origin: {"n_in_blast_radius": b["n_in_blast_radius"],
                     "controllers": b["controllers"], "controlled": b["controlled"]}
            for origin, b in blast.items()
        },
    }
    if report_dir is not None:
        _write_report(report, Path(report_dir) / f"{report_name}.json")
    return report


def _write_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False, default=str), encoding="utf-8")
    return path

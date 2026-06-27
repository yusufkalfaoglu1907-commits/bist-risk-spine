"""Id-bridge health monitor (M8.3) — make the single-point-of-failure observable and regression-guarded.

The id-bridge (ticker ↔ ISIN ↔ kap_oid ↔ LEI) is the project's named SPOF (CLAUDE.md §5): if a ticker
resolves to the wrong identity, every downstream signal for that name is silently wrong. The resolver
(``pit.idbridge.IdBridge``) already refuses-rather-than-guesses on a single lookup; this monitor sweeps
the **whole ticker-bearing universe** and reports the bridge's standing health so decay is visible:

  * **per-leg coverage** — the fraction of ticker names carrying each id leg (a leg silently emptying out
    is a real regression);
  * **collisions** — any non-null id value shared by >1 distinct company (an *ambiguous* identity: the
    exact failure the resolver refuses on, surfaced here proactively across the whole graph);
  * **round-trip sweep** — every ticker resolved and confirmed to round-trip to the same company
    (defense in depth; with zero collisions this is guaranteed, but it catches a future schema change).

It reads the L1 graph directly (identity is not a time-varying signal read — same stance as the resolver)
and writes ``data/cache/idbridge_health_report.json`` (§4). Pure read; no network, no L2, no mutation.
The paired invariant (``tests/invariants/test_idbridge_health.py``) fails when coverage drops below the
documented floors or any collision / broken round-trip appears.
"""
from __future__ import annotations

from typing import Any

from tmkg.ingest.audit import write_run_report
from tmkg.pit.errors import IdentityAmbiguous
from tmkg.pit.idbridge import IdBridge

# Legs that must round-trip (ticker is the sweep key, so the resolvable legs are the other three).
_LEGS = ("isin", "kap_oid", "lei")

# Regression floors on the fraction of **ticker-bearing** names carrying each leg. These guard against
# a leg silently emptying out; they sit below the current real coverage (isin≈0.83, kap_oid=1.00,
# lei≈0.92 on 2026-06-27), not at an aspirational target. NB the README's "ISIN 100%" is on the
# *equity-traded subset*, a stricter population than all ticker names — this floor is the broad guard.
DEFAULT_COVERAGE_FLOORS: dict[str, float] = {"isin": 0.80, "kap_oid": 0.99, "lei": 0.88}


def _ticker_rows(con: Any) -> list[dict]:
    """All ticker-bearing companies with their id legs (one query)."""
    res = con.execute(
        "MATCH (c:Company) WHERE c.ticker IS NOT NULL "
        "RETURN c.ticker, c.isin, c.kap_oid, c.lei, c.uuid"
    )
    cols = ["ticker", "isin", "kap_oid", "lei", "uuid"]
    out: list[dict] = []
    while res.has_next():
        out.append(dict(zip(cols, res.get_next())))
    return out


def _collisions(con: Any, leg: str) -> list[dict]:
    """Non-null values of ``leg`` shared by >1 distinct company — an ambiguous identity."""
    res = con.execute(
        f"MATCH (c:Company) WHERE c.{leg} IS NOT NULL "
        f"RETURN c.{leg} AS v, count(DISTINCT c.uuid) AS n"
    )
    out: list[dict] = []
    while res.has_next():
        v, n = res.get_next()
        if n > 1:
            out.append({"value": v, "n_companies": int(n)})
    return out


def idbridge_health(
    con: Any,
    *,
    coverage_floors: dict[str, float] | None = None,
    full_round_trip: bool = True,
    max_listed: int = 50,
) -> dict:
    """Compute the id-bridge health report over the whole ticker-bearing universe.

    ``full_round_trip`` sweeps every ticker through ``IdBridge.round_trip`` (defense in depth);
    set ``False`` to skip the per-name sweep (coverage + collisions still computed). ``passes`` is
    True iff every leg meets its coverage floor, there are no collisions, and no round-trip broke.
    Pure read — does not mutate the graph or write anything (the script wraps it to write the §4 report).
    """
    floors = coverage_floors or DEFAULT_COVERAGE_FLOORS
    rows = _ticker_rows(con)
    n = len(rows)
    coverage = {leg: (sum(1 for r in rows if r.get(leg)) / n if n else 0.0) for leg in _LEGS}
    collisions = {leg: _collisions(con, leg) for leg in _LEGS}

    ok = 0
    broken: list[dict] = []
    ambiguous: list[dict] = []
    if full_round_trip:
        bridge = IdBridge(con)
        for r in rows:
            try:
                bridge.round_trip(r["ticker"])
                ok += 1
            except IdentityAmbiguous as e:
                bucket = ambiguous if "matches" in str(e) else broken
                bucket.append({"ticker": r["ticker"], "detail": str(e)})

    failures: list[str] = []
    for leg, floor in floors.items():
        if coverage[leg] < floor:
            failures.append(f"{leg} coverage {coverage[leg]:.3f} < floor {floor}")
    for leg, coll in collisions.items():
        if coll:
            failures.append(f"{leg} has {len(coll)} colliding value(s) — ambiguous identity")
    if broken:
        failures.append(f"{len(broken)} broken round-trip(s)")

    return {
        "monitor": "idbridge_health",
        "n_companies_ticker": n,
        "coverage": {leg: round(coverage[leg], 4) for leg in _LEGS},
        "coverage_floors": floors,
        "collisions": {leg: collisions[leg][:max_listed] for leg in _LEGS},
        "collision_counts": {leg: len(collisions[leg]) for leg in _LEGS},
        "round_trip": {
            "swept": bool(full_round_trip),
            "ok": ok,
            "broken": broken[:max_listed],
            "ambiguous": ambiguous[:max_listed],
            "broken_count": len(broken),
            "ambiguous_count": len(ambiguous),
        },
        "passes": not failures,
        "failures": failures,
    }


def write_idbridge_health_report(con: Any, **kwargs) -> tuple[dict, Any]:
    """Run the monitor and write ``data/cache/idbridge_health_report.json`` (§4). Returns (report, path)."""
    report = idbridge_health(con, **kwargs)
    path = write_run_report("idbridge_health", report)
    return report, path

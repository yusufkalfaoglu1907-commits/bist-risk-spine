"""Linkage-graph shock propagation (M8.2) — the non-cross-sectional use of the ownership graph.

Where M8.1 re-prices a *macro channel* shock uniformly through every name's factor betas (systematic
exposure), this propagates an *idiosyncratic node* shock through the **ownership/control graph**
(structural, firm-specific exposure). It is a **risk** tool, not alpha: it answers "if this name takes
an idiosyncratic hit, which holders are mechanically exposed and by how much" — an accounting-style
look-through, **not** a prediction that linked names will co-move (that linked-firm *alpha* claim was
tested and rejected on BIST, ADR-0006). No Sharpe, no gate, no registry write.

Two mechanisms, both pure (edge lists + shocks in, results out — no graph/network/L2):

  * **ownership look-through** (``propagate_ownership_shock``) — a held company's shock flows *up* to
    its holders in proportion to the stake fraction (``HOLDS_STAKE.pct``), compounding along multi-hop
    chains (KCHOL → AYGAZ → … ). Magnitude-bearing.
  * **control blast-radius** (``control_blast_radius``) — the set of names structurally reachable from
    an origin over the ``CONTROLS`` DAG (controllers up / controlled down). Reachability, not magnitude
    (CONTROLS is binary) — "who is in the same control group as the shocked name."

Honesty (§4): a stake fraction outside [0, 1] **raises** (the caller must normalise pct→fraction
explicitly); an origin present in no edge is surfaced as ``unmapped`` not silently dropped; an
ownership cycle is recorded rather than looped on; the look-through is flagged as ignoring the held
stake's weight in the holder's *total* asset base (we lack fundamentals/market-cap to scale that yet).
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OwnershipEdge:
    """A holder owns ``fraction`` (0..1) of ``held``; ``confidence`` is the edge's provenance trust."""
    holder: str
    held: str
    fraction: float
    confidence: float = 1.0


@dataclass(frozen=True)
class PropagationResult:
    per_holder: dict[str, float]          # total propagated shock per exposed holder
    paths: list[dict]                     # one record per propagation path (holder ← … ← origin)
    origins: dict[str, float]             # echoed origin shocks
    unmapped_origins: list[str]           # shocked nodes that appear in no ownership edge
    cycles: list[dict] = field(default_factory=list)

    def worst_exposed(self, n: int = 10) -> list[tuple[str, float]]:
        return sorted(self.per_holder.items(), key=lambda kv: kv[1])[:n]

    def summary(self) -> dict:
        return {
            "n_holders_exposed": len(self.per_holder),
            "n_paths": len(self.paths),
            "unmapped_origins": list(self.unmapped_origins),
            "cycles": self.cycles[:10],
            "worst_exposed": {k: float(v) for k, v in self.worst_exposed(10)},
            "caveat": ("look-through weights a holder's exposure by the stake fraction only; it does "
                       "NOT scale by the held stake's share of the holder's total assets (no "
                       "fundamentals yet) — treat magnitudes as upper-bound structural exposure."),
        }


def propagate_ownership_shock(
    edges: list[OwnershipEdge],
    shocks: Mapping[str, float],
    *,
    max_hops: int = 6,
    min_confidence: float = 0.0,
) -> PropagationResult:
    """Propagate idiosyncratic ``shocks`` (node → signed magnitude) UP the ownership chains in ``edges``.

    A holder's exposure to an origin is ``(Π stake fractions along the path) · shock[origin]``, summed
    over every path and origin. Edges below ``min_confidence`` are dropped; chains longer than
    ``max_hops`` are truncated; a stake fraction outside [0, 1] raises (normalise pct→fraction first);
    a cycle is recorded and not traversed. Pure — no graph/network read."""
    up: dict[str, list[tuple[str, float, float]]] = defaultdict(list)  # held -> [(holder, frac, conf)]
    nodes: set[str] = set()
    for e in edges:
        if not (0.0 <= e.fraction <= 1.0):
            raise ValueError(
                f"ownership fraction {e.fraction!r} for {e.holder}->{e.held} outside [0,1] — "
                "caller must convert pct to a fraction (pct/100) before propagating"
            )
        nodes.add(e.holder)
        nodes.add(e.held)
        if e.confidence < min_confidence:
            continue
        up[e.held].append((e.holder, e.fraction, e.confidence))

    per_holder: dict[str, float] = defaultdict(float)
    paths: list[dict] = []
    cycles: list[dict] = []

    for origin, shock in shocks.items():
        # DFS upward from the shocked origin; (node, fraction_product, min_conf, path-so-far)
        stack: list[tuple[str, float, float, tuple[str, ...]]] = [(origin, 1.0, 1.0, (origin,))]
        while stack:
            node, fp, mc, path = stack.pop()
            if len(path) > max_hops + 1:
                continue
            for holder, frac, conf in up.get(node, []):
                if holder in path:
                    cycles.append({"cycle_at": holder, "path": list(path) + [holder]})
                    continue
                nfp = fp * frac
                nmc = min(mc, conf)
                npath = path + (holder,)
                contrib = nfp * float(shock)
                per_holder[holder] += contrib
                paths.append({
                    "holder": holder, "origin": origin, "hops": len(npath) - 1,
                    "fraction_product": round(nfp, 6), "contribution": contrib,
                    "min_confidence": nmc, "path": list(npath),
                })
                stack.append((holder, nfp, nmc, npath))

    unmapped = [o for o in shocks if o not in nodes]
    return PropagationResult(per_holder=dict(per_holder), paths=paths,
                             origins=dict(shocks), unmapped_origins=unmapped, cycles=cycles)


def control_blast_radius(
    controls_edges: list[tuple[str, str]],
    origin: str,
    *,
    direction: str = "both",
    max_hops: int = 12,
) -> dict:
    """The names structurally reachable from ``origin`` over the ``CONTROLS`` DAG.

    ``controls_edges`` are (controller, controlled) pairs. ``direction``: ``up`` = controllers
    (ancestors), ``down`` = controlled (descendants), ``both``. Returns each reachable name with its
    hop distance + the union ``blast_radius`` (the structural group exposed to a control/governance
    shock at ``origin``). Reachability only — CONTROLS is binary, so no magnitude."""
    if direction not in ("up", "down", "both"):
        raise ValueError(f"direction must be up|down|both, got {direction!r}")
    down: dict[str, list[str]] = defaultdict(list)
    up: dict[str, list[str]] = defaultdict(list)
    for controller, controlled in controls_edges:
        down[controller].append(controlled)
        up[controlled].append(controller)

    def _bfs(adj: dict[str, list[str]]) -> dict[str, int]:
        seen: dict[str, int] = {}
        q: deque[tuple[str, int]] = deque([(origin, 0)])
        while q:
            node, depth = q.popleft()
            if depth >= max_hops:
                continue
            for nxt in adj.get(node, []):
                if nxt != origin and nxt not in seen:
                    seen[nxt] = depth + 1
                    q.append((nxt, depth + 1))
        return seen

    controllers = _bfs(up) if direction in ("up", "both") else {}
    controlled = _bfs(down) if direction in ("down", "both") else {}
    blast = sorted(set(controllers) | set(controlled))
    return {
        "origin": origin,
        "direction": direction,
        "controllers": controllers,           # name -> hops up to it
        "controlled": controlled,             # name -> hops down to it
        "blast_radius": blast,
        "n_in_blast_radius": len(blast),
    }

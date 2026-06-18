"""Post-load graph-integrity checks — fail-loud invariants on the control graph.

The blast-radius / group-rooting analytics assume the CONTROLS graph is a DAG:
rooting walks *up* to an apex that has no parent, and membership fans *down*. A
cycle (A controls B controls … controls A) breaks both — there is no apex, every
node looks parented, and a naive upward walk can loop forever. The audit (F6)
injected a KOCFN→KCHOL edge to prove the loaders could silently admit one.

This module is the guard: after any build/back-fill it counts CONTROLS cycles
and, by default, raises so a bad load cannot reach an analyst as a confident-
looking answer. It is intentionally exact (Tarjan SCC over the full edge set, no
hop bound) rather than a bounded var-length probe that could miss a long cycle.
"""
from __future__ import annotations

from typing import Iterable

import kuzu


def strongly_connected_components(
    nodes: Iterable[str], successors: dict[str, list[str]]
) -> list[list[str]]:
    """Tarjan's SCC, iterative (no recursion-depth limit on deep chains).

    `successors[n]` is the list of nodes n points to. Returns one list per
    strongly connected component. Singleton components (the common DAG case) are
    returned too; a component with len > 1 is a cycle.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        # Iterative DFS. work holds (node, successor-iterator) frames.
        work: list[tuple[str, Iterable[str]]] = [(root, iter(successors.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, it = work[-1]
            descended = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, iter(successors.get(w, ()))))
                    descended = True
                    break
                if on_stack.get(w):
                    low[node] = min(low[node], index[w])
            if descended:
                continue
            # node fully explored
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                out.append(comp)
            work.pop()
            if work:  # propagate low-link to parent frame
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return out


def load_controls_successors(conn: kuzu.Connection) -> tuple[set[str], dict[str, list[str]]]:
    """Every Company uuid + the CONTROLS adjacency (controller -> [controlled])."""
    nodes: set[str] = set()
    succ: dict[str, list[str]] = {}
    r = conn.execute("MATCH (c:Company) RETURN c.uuid")
    while r.has_next():
        nodes.add(r.get_next()[0])
    r = conn.execute("MATCH (p:Company)-[:CONTROLS]->(c:Company) RETURN p.uuid, c.uuid")
    while r.has_next():
        p, c = r.get_next()
        succ.setdefault(p, []).append(c)
        nodes.add(p)
        nodes.add(c)
    return nodes, succ


def find_controls_cycles(conn: kuzu.Connection) -> list[list[str]]:
    """All CONTROLS cycles as a list of node-uuid groups.

    A multi-node strongly connected component is a cycle; a self-loop
    (a company controlling itself) is reported as a singleton group too.
    """
    nodes, succ = load_controls_successors(conn)
    cycles = [comp for comp in strongly_connected_components(nodes, succ) if len(comp) > 1]
    cycles.extend([n] for n in nodes if n in succ.get(n, ()))  # self-loops
    return cycles


class ControlsCycleError(AssertionError):
    """Raised when the CONTROLS graph contains a cycle (it must be a DAG)."""


def check_no_controls_cycles(conn: kuzu.Connection, *, raise_on_fail: bool = True) -> dict:
    """Fail-loud post-load integrity check. Returns a small report dict.

    On a cycle, raises ``ControlsCycleError`` by default (the offending node
    groups are in the message and the returned dict). Pass
    ``raise_on_fail=False`` to get the report without raising (for tooling that
    wants to print rather than abort).
    """
    cycles = find_controls_cycles(conn)
    report = {"controls_cycles": len(cycles), "cycle_members": cycles}
    if cycles and raise_on_fail:
        preview = "; ".join(" -> ".join(c) for c in cycles[:5])
        raise ControlsCycleError(
            f"CONTROLS graph is not a DAG: {len(cycles)} cycle(s) found: {preview}"
        )
    return report

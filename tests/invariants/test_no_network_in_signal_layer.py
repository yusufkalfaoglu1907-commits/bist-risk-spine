"""No-network-in-signal-layer invariant (CLAUDE.md §4 rule 1).

"Signal/backtest code never makes a network call. It reads L2/L1 only. A backtest that
depends on a live connection is not reproducible." The network is confined to the ingestion
adapters; the L3 compute layer (signals / factors / returns / analytics) reads the local
cache. This is enforced two ways:

  1. importing the signal package must not require a network client (a runtime smoke);
  2. an **AST source scan** of the L3 packages: no module may import a network client library
     (httpx / requests / urllib / aiohttp / socket / http.client) nor a network-bearing
     ingestion *adapter* module (matriks / evds / fred / worldgovbonds / the HTTP base).

Importing ``tmkg.ingest.pipeline``'s L2 *read* helpers (e.g. ``build_factor_return_panel``,
which reads factor levels back through PITAccess) is allowed — that is reading the local
cache, exactly what §4 sanctions. The boundary is the *adapter*, not the orchestrator.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import tmkg.config as config

# Packages that form the L3 compute / signal layer (must never hit the network).
_L3_PACKAGES = ("signals", "factors", "returns", "analytics", "events", "risk", "monitor")

# A network call is impossible without one of these client libraries — forbid them outright.
_NET_CLIENT_ROOTS = frozenset(
    {"httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "http", "websocket"}
)

# The ingestion *adapter* modules whose job is to touch the network. Importing one into the
# signal layer is the forbidden coupling (the orchestrator `pipeline` and the L2/audit/
# universe/survivorship helpers are not adapters and may be read for their local-cache logic).
_NET_ADAPTER_MODULES = frozenset(
    {
        "tmkg.ingest.matriks",
        "tmkg.ingest.evds",
        "tmkg.ingest.fred",
        "tmkg.ingest.worldgovbonds",
        "tmkg.ingest.gdelt",
        "tmkg.ingest.base",
    }
)


@pytest.mark.invariant
def test_signals_package_imports_without_network():
    # Importing L3 must not require a network client.
    importlib.import_module("tmkg.signals")


def _network_imports(tree: ast.AST) -> list[str]:
    """Offending import targets in this module: a network client library, or a
    network-bearing ingestion adapter module. AST-only (comments/strings excluded)."""
    offenders: list[str] = []

    def _flag(fqname: str) -> None:
        root = fqname.split(".")[0]
        if root in _NET_CLIENT_ROOTS or fqname in _NET_ADAPTER_MODULES:
            offenders.append(fqname)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _flag(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            _flag(module)
            # `from tmkg.ingest import matriks` -> module="tmkg.ingest", name="matriks"
            for alias in node.names:
                _flag(f"{module}.{alias.name}")
    return offenders


@pytest.mark.invariant
def test_signal_modules_do_not_import_ingest_or_network():
    """No module under the L3 packages may import a network client or a network-bearing
    ingestion adapter — so signal/backtest code provably cannot make a network call (§4)."""
    pkg_root = Path(config.__file__).parent
    offenders: dict[str, list[str]] = {}
    for pkg in _L3_PACKAGES:
        pkg_dir = pkg_root / pkg
        if not pkg_dir.exists():
            continue
        for py in pkg_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            hits = _network_imports(tree)
            if hits:
                offenders[str(py.relative_to(pkg_root.parent))] = sorted(set(hits))
    assert not offenders, (
        "L3 signal-layer code imports a network client / ingestion adapter — it must read "
        f"L2/L1 only (CLAUDE.md §4 rule 1): {offenders}"
    )

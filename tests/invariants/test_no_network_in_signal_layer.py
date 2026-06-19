"""No-network-in-signal-layer invariant (CLAUDE.md §4 rule 1)."""
from __future__ import annotations

import importlib

import pytest


@pytest.mark.invariant
def test_signals_package_imports_without_network():
    # Importing L3 must not require a network client.
    importlib.import_module("tmkg.signals")


@pytest.mark.invariant
@pytest.mark.skip(reason="M3: unskip once signal modules exist — AST-scan their imports")
def test_signal_modules_do_not_import_ingest_or_network():
    """No module under tmkg.signals / factors / returns may import tmkg.ingest or
    a network client (httpx / requests / urllib). Enforce by scanning sources."""
    ...

"""No-fabrication invariant (CLAUDE.md §4 rule 2)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tmkg.config as config
from tmkg.ingest import MatriksAdapter
from tmkg.pit import FabricationGuard, SourceUnreachable

# Packages whose code can land rows in L2 (the quant store): the Matriks ingest
# adapter/pipeline, the return constructors it feeds, and the store itself. The v1
# `tmkg.loaders` package legitimately reads fixtures/ but writes the **L1 Kuzu
# identity graph**, never L2 — so it is out of scope for this L2-only invariant.
_L2_INGRESS_PACKAGES = ("l2", "ingest", "returns")


@pytest.mark.invariant
def test_fabrication_guards_exist():
    assert issubclass(FabricationGuard, RuntimeError)
    assert issubclass(SourceUnreachable, RuntimeError)


@pytest.mark.invariant
def test_missing_credentials_raise_not_fabricate():
    """No creds = unreachable. The adapter must FAIL LOUD (SourceUnreachable),
    never return a placeholder bar (offline, deterministic)."""
    a = MatriksAdapter()
    a.username, a.api_key = "", ""
    with pytest.raises(SourceUnreachable):
        a.fetch("marketPrice", action="price", symbol="THYAO")


@pytest.mark.invariant
def test_unresolvable_host_raises_not_fabricate():
    """A network failure must surface as SourceUnreachable, not a fabricated value."""
    a = MatriksAdapter(timeout=2.0)
    a.username, a.api_key = "39617", "sk_live_dummy"
    a.rest_url = "https://matriks.invalid/mcp-api/v1"  # .invalid never resolves (RFC 6761)
    with pytest.raises(SourceUnreachable):
        a.fetch("marketPrice", action="price", symbol="THYAO")


def _references_fixtures(tree: ast.AST) -> bool:
    """True if the AST sources fixtures/: a string constant naming the fixtures
    path, or any use of config.FIXTURES_PATH. Comments are excluded (AST-only)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "fixtures" in node.value.lower():
                return True
        # config.FIXTURES_PATH / `from tmkg.config import FIXTURES_PATH`
        if isinstance(node, ast.Attribute) and node.attr == "FIXTURES_PATH":
            return True
        if isinstance(node, ast.Name) and node.id == "FIXTURES_PATH":
            return True
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "FIXTURES_PATH" for alias in node.names):
                return True
    return False


@pytest.mark.invariant
def test_fixtures_never_reach_l2():
    """Nothing under fixtures/ may be loaded into L2 (illustrative fixtures are
    unit-test-only and must never be sourced as if real). Enforced by scanning the
    L2-ingress packages' sources: none of them may read fixtures/ — only the L1
    identity loaders (tmkg.loaders) legitimately do, and those never write L2."""
    pkg_root = Path(config.__file__).parent
    offenders = []
    for pkg in _L2_INGRESS_PACKAGES:
        for py in (pkg_root / pkg).rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            if _references_fixtures(tree):
                offenders.append(str(py.relative_to(pkg_root.parent)))
    assert not offenders, (
        "L2-ingress code references fixtures/ (illustrative fixtures must never "
        f"reach L2, CLAUDE.md §4): {offenders}"
    )

"""Neutralization orthogonality — §5 invariant (VERIFICATION.md §1).

Maps to CLAUDE.md §5 "factor strip" / system-design-v2.md §200: residual returns from
M2 must be statistically orthogonal to **each** stripped factor, in the specified order.
This is the guard that makes "residual linkage" falsifiable — a residual still correlated
with USD/TRY, Turkey CDS, oil or a holding-cluster is a disguised factor bet, not a
residual, and downstream (M3) it would fabricate supply-chain linkage.

The guard runs the shipped ladder over the full explicit order and asserts the residual
is orthogonal to every rung, including the §5-critical foreign-flow factor (which, if not
stripped, makes flow-driven comovement masquerade as residual linkage).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tmkg.factors.neutralize import DEFAULT_LADDER, strip_residual


def _correlated_factor_panel(n: int, seed: int) -> tuple[np.ndarray, list[str]]:
    """A realistic, *correlated* factor panel spanning the full ladder roles — market,
    fx, rates/cds, energy, sector, foreign-flow, holding — so orthogonality is a real
    claim (independent factors would make it trivial)."""
    rng = np.random.default_rng(seed)
    roles = list(DEFAULT_LADDER)
    common = rng.normal(0, 1, n)  # a shared driver -> the factors are genuinely correlated
    cols = []
    for j, _ in enumerate(roles):
        cols.append(0.6 * common + rng.normal(0, 1, n) + 0.1 * j)
    return np.column_stack(cols), roles


@pytest.mark.invariant
def test_residual_orthogonal_to_each_factor_in_the_full_ladder():
    n = 400
    F, roles = _correlated_factor_panel(n, seed=7)
    rng = np.random.default_rng(99)
    true_betas = rng.normal(0, 1, F.shape[1])
    y = F @ true_betas + rng.normal(0, 0.5, n)  # a return that IS a factor bet + noise

    resid = strip_residual(y, F)

    # the residual must be orthogonal to every stripped factor — no exceptions, and
    # explicitly including foreign_flow and holding (the easy-to-skip §5 rungs).
    for j, role in enumerate(roles):
        fc = F[:, j] - F[:, j].mean()
        corr = abs(np.corrcoef(resid, fc)[0, 1])
        assert corr < 1e-9, f"residual leaks factor {role!r}: |corr|={corr:g}"


@pytest.mark.invariant
def test_foreign_flow_must_be_in_the_ladder():
    """§5: the foreign-flow factor must be stripped, or flow comovement masquerades as
    residual linkage. Guard the canonical ladder declares it."""
    assert "foreign_flow" in DEFAULT_LADDER


@pytest.mark.invariant
def test_unstripped_factor_remains_correlated_the_guard_has_teeth():
    """A residual that fails to strip a factor stays correlated with it — proving the
    orthogonality test above is not vacuously passing."""
    n = 300
    F, roles = _correlated_factor_panel(n, seed=11)
    rng = np.random.default_rng(5)
    y = F @ rng.normal(0, 1, F.shape[1]) + rng.normal(0, 0.3, n)
    # strip everything EXCEPT the foreign-flow rung
    ff = roles.index("foreign_flow")
    kept = [j for j in range(F.shape[1]) if j != ff]
    resid = strip_residual(y, F[:, kept])
    leak = abs(np.corrcoef(resid, F[:, ff] - F[:, ff].mean())[0, 1])
    assert leak > 1e-3  # the un-stripped factor is still in the residual

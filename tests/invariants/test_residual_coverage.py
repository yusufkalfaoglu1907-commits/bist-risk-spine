"""Residual coverage-continuity + factor non-degeneracy — §5 substrate guards.

These two invariants guard the *real* L2 substrate (``data/l2.duckdb``), not a
synthetic panel. They exist because the rest of the suite checks PIT + orthogonality
on synthetic data but never asks whether the production residual table is actually
*continuous* or whether every stripped factor actually carries variance:

1. **Coverage continuity (no *unexplained* hole)** — a naive "every session must have
   residuals" guard is wrong here: the residual series legitimately goes dark for
   ``min_obs`` sessions after every regime break (the §5 no-straddle rule — you cannot
   neutralize against a regime whose betas you have not yet estimated), and on any day a
   stripped factor did not print (§4 no-fabrication). The 2025-03-19 İmamoğlu "hole"
   (2025-03-19 → 2025-05-20) is exactly the 39-session ``imamoglu_shock_2025`` warm-up,
   *by design*. So this guard asserts the only thing that is a real defect: every
   **emittable** session — panel-complete and far enough into its regime that a correct
   build *would* have produced a residual — actually has one. Zero tolerance for an
   emittable-but-missing day; that is the signature of a crashed/truncated/date-bounded
   build, the silent-corruption failure mode we actually fear. It also catches a stale
   frontier (the latest emittable session must be covered).

2. **Factor non-degeneracy** — a factor whose regressor column is dead (σ≈0) or whose
   betas are all ~0 strips nothing, so §5's "foreign-flow must be stripped or flow
   comovement masquerades as residual linkage" would be satisfied only nominally. The
   honest measure of "does this factor bite" is **scale-invariant**: the variance it
   contributes to the fit, ``mean|β| · σ(factor return)`` — *not* raw |β|, which is a
   units artifact (FFLOW's β≈2e-5 looks dead but its σ≈249 raw-USD-mn flow makes its
   contribution mid-pack; a yield factor's β is small because its σ is large). This
   guard floors the per-factor contribution against the panel median.

If the real store is absent (CI without the built DB), both guards skip.
"""
from __future__ import annotations

import duckdb
import pytest

import tmkg.config as config
from tmkg.factors import registry
from tmkg.factors.betas import factor_panel
from tmkg.factors.regime import regime_for_date
from tmkg.factors.registry import CORE_FACTORS
from tmkg.factors.series import compute_factor_returns
from tmkg.ingest.pipeline import build_factor_return_panel
from tmkg.l2.store import L2Store

L2_PATH = config.REPO_ROOT / "data" / "l2.duckdb"

# A residual panel counts as "built" for a session if it carries this many names.
_MIN_RESIDUAL_PANEL = 50
# Production residual-build knobs (must match scripts/run_m3_gate.py FIT_WINDOW/MIN_OBS).
_FIT_WINDOW = 60
_FIT_MIN_OBS = 40

# A factor is degenerate if its variance contribution falls below this fraction of the
# panel median — i.e. it is ~an order of magnitude deader than the typical factor.
_DEGENERACY_FLOOR_FRAC = 0.05


def _con():
    # Match L2Store's default (read-write) so a same-process panel build — which opens
    # its own L2Store handle — shares connection config (DuckDB forbids mixing read_only
    # and read-write handles to one file). These guards only SELECT; nothing is mutated.
    if not L2_PATH.exists():
        pytest.skip(f"real L2 store absent ({L2_PATH}) — substrate guard skipped")
    con = duckdb.connect(str(L2_PATH))
    n = con.execute("SELECT count(*) FROM residuals").fetchone()[0]
    if n == 0:
        con.close()
        pytest.skip("residuals table empty — nothing to guard yet")
    return con


def _emittable_sessions(con, as_of):
    """The set of sessions a correct residual build *should* have produced, at the build's
    own vintage ``as_of``. A session D is emittable iff its factor panel is complete that
    day AND its trailing ``_FIT_WINDOW`` holds ≥ ``_FIT_MIN_OBS`` panel-complete sessions
    in D's own regime (the rolling_residuals skip rule, re-derived from the spec). Regime
    blackouts (§5) and panel-gap days (§4) are therefore *not* emittable — only genuine
    drops are. Conservative at the left edge (ignores pre-span history), so it can only
    under-claim emittability, never falsely flag a covered day."""
    # real BIST sessions = days the market index printed, up to the build vintage
    sessions = [
        r[0] for r in con.execute(
            "SELECT DISTINCT bar_date FROM factors "
            "WHERE factor = 'XU100' AND bar_date <= ? ORDER BY bar_date", [as_of]
        ).fetchall()
    ]
    panel = build_factor_return_panel(L2Store(), as_of=as_of, specs=registry.specs())
    wide = factor_panel(panel)
    strip = [c for c in registry.order_present(list(dict.fromkeys(panel["factor"])))
             if c in wide.columns]
    complete = set(wide[strip].dropna().index)

    emittable = set()
    for i, D in enumerate(sessions):
        if D not in complete:
            continue
        reg = regime_for_date(D)
        win = sessions[max(0, i - _FIT_WINDOW + 1): i + 1]
        usable = sum(1 for s in win if s in complete and regime_for_date(s) == reg)
        if usable >= _FIT_MIN_OBS:
            emittable.add(D)
    return emittable


@pytest.mark.invariant
def test_no_unexplained_residual_hole():
    """Every emittable session has a residual panel. A regime-break blackout (§5) or a
    factor-gap day (§4) is *not* a hole; an emittable-but-missing day is — the signature
    of a crashed/truncated/stale build. Zero tolerance."""
    con = _con()
    try:
        as_of = con.execute("SELECT max(bar_date) FROM residuals").fetchone()[0]
        # fetchall() -> datetime.date, matching the session list (a .df() read would yield
        # pandas Timestamps and silently never intersect).
        covered = {
            r[0] for r in con.execute(
                "SELECT bar_date FROM residuals GROUP BY bar_date HAVING count(*) >= ?",
                [_MIN_RESIDUAL_PANEL],
            ).fetchall()
        }
        emittable = _emittable_sessions(con, as_of)
    finally:
        con.close()

    missing = sorted(d for d in emittable if d not in covered)
    assert not missing, (
        f"{len(missing)} emittable session(s) (panel-complete, past their regime warm-up) "
        f"have no residual panel as of build vintage {as_of} — a real hole, not a §5 "
        f"blackout or §4 factor-gap. First/last: {missing[0]}..{missing[-1]}; "
        f"e.g. {missing[:5]}"
    )


@pytest.mark.invariant
def test_stripped_factors_are_not_degenerate():
    """Every stripped factor must carry variance — its contribution ``mean|β|·σ`` above
    a floor tied to the panel median. Scale-invariant: catches a dead/constant regressor
    (the real §5 failure) without false-flagging small-β-but-large-σ factors (yields,
    CDS, the raw-flow FFLOW), whose tiny coefficients are a units artifact."""
    con = _con()
    try:
        mean_abs = dict(
            con.execute("SELECT factor, avg(abs(beta)) FROM betas GROUP BY factor").fetchall()
        )
        fac = con.execute("SELECT factor, bar_date, value FROM factors").df()
    finally:
        con.close()
    if not mean_abs:
        pytest.skip("no betas in L2 — nothing to guard")

    methods = {f.name: f.method for f in CORE_FACTORS}
    sigma = compute_factor_returns(fac, method=methods).groupby("factor")["ret"].std()

    contribution = {
        f: mb * sigma.get(f, float("nan"))
        for f, mb in mean_abs.items()
    }
    # a factor with no computable σ (≤1 obs) is itself a degeneracy, not a free pass
    import math
    nan_factors = [f for f, c in contribution.items() if math.isnan(c)]
    assert not nan_factors, f"factor(s) with no computable return variance: {nan_factors}"

    median = sorted(contribution.values())[len(contribution) // 2]
    floor = _DEGENERACY_FLOOR_FRAC * median
    degenerate = {f: c for f, c in contribution.items() if c < floor}
    assert not degenerate, (
        f"factor(s) contribute ~no variance (mean|β|·σ < {floor:g} = "
        f"{_DEGENERACY_FLOOR_FRAC:g}×median {median:g}): {degenerate}"
    )

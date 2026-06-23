"""PIT backtester — purge/embargo splits, cost+borrow model, three books (BUILD_PLAN.md M4).

The harness that turns a set of target weights into an *honest* P&L. Three deceptions it is
built to refuse:

  - **Lookahead in cross-validation.** Train/test folds are **purged** (drop train labels whose
    holding window overlaps the test window) and **embargoed** (drop train rows immediately
    after the test window), per López de Prado — otherwise serially-correlated returns leak the
    answer across the split boundary.
  - **"Net of costs" hand-waving.** An explicit per-name **cost + borrow** model and a
    **capacity curve** (how net Sharpe decays as you scale notional), not a flat haircut.
  - **The tradability illusion.** The same weights are run through **three books** (design §3 /
    VERIFICATION §3):
      • ``research``       — frictionless long/short, no constraints (the seductive number);
      • ``venue_feasible`` — enforces ``short_eligible`` (no shorting a banned name), blocks
                             trading on **limit-lock** days (you cannot rebalance into a locked
                             name — the prior weight is carried), and charges cost + borrow;
      • ``stress``         — venue-feasible **plus** a full short-ban (the 2025 toggled 6×),
                             a crowding cost multiplier, and limit-lock blocks.
    A signal that lives only in ``research`` is **not real** (M5 exit gate).

Pure compute: NumPy/pandas only, no L2, no network (L3 — enforced by the AST scan). The runner
in promotion.py reads the weights / returns / ``short_eligible`` / limit-lock panels through
PITAccess and hands them here, mirroring how gate.py wraps the M3 stability core.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tmkg.signals.stats import sharpe_ratio

_EPS = 1e-12


# --- purge + embargo cross-validation splits -------------------------------


@dataclass(frozen=True)
class Split:
    """One purged/embargoed CV fold: integer row positions into the date axis."""
    train: np.ndarray
    test: np.ndarray


def purged_walk_forward_splits(
    n: int,
    *,
    n_splits: int = 5,
    embargo: int = 0,
    purge: int = 0,
) -> list[Split]:
    """Anchored walk-forward folds with purge + embargo over ``n`` ordered observations.

    Test folds are consecutive equal blocks marching forward in time. Each fold trains on
    everything strictly *before* its test block, minus a ``purge`` gap (labels whose holding
    window would overlap the test block) and an ``embargo`` (rows just *after* the test block
    are never used to train an earlier-anchored model — here training is causal so embargo only
    bites the post-test region, kept for parity with purged k-fold). Walk-forward (not k-fold)
    because a trading backtest must respect the arrow of time: never train on the future."""
    if n_splits < 2:
        raise ValueError("n_splits must be >= 2")
    if n < n_splits:
        raise ValueError(f"need >= n_splits ({n_splits}) observations; have {n}")
    fold = n // (n_splits + 1)  # first block reserved as the initial training base
    if fold < 1:
        raise ValueError("too few observations for the requested n_splits")
    splits: list[Split] = []
    for k in range(1, n_splits + 1):
        test_lo = k * fold
        test_hi = (k + 1) * fold if k < n_splits else n
        test = np.arange(test_lo, test_hi)
        train_hi = max(test_lo - purge, 0)
        train = np.arange(0, train_hi)
        if embargo > 0:  # also drop a band trailing the *previous* test block from training
            banned = np.arange(max(test_lo - fold - embargo, 0), test_lo - fold) \
                if test_lo - fold > 0 else np.array([], dtype=int)
            train = np.setdiff1d(train, banned)
        splits.append(Split(train=train, test=test))
    return splits


# --- cost + borrow model and book definitions ------------------------------


@dataclass(frozen=True)
class CostModel:
    """Per-name linear cost + borrow. ``cost_bps`` charged on |Δweight| turnover each period;
    ``borrow_bps_annual`` charged on the short notional carried, pro-rated to the period."""
    cost_bps: float = 10.0           # one-way transaction cost in basis points of turnover
    borrow_bps_annual: float = 100.0  # annual stock-borrow fee on short notional
    periods_per_year: int = 252

    @property
    def cost_rate(self) -> float:
        return self.cost_bps / 1e4

    @property
    def borrow_rate_per_period(self) -> float:
        return (self.borrow_bps_annual / 1e4) / self.periods_per_year


@dataclass(frozen=True)
class BookConfig:
    """A book = which frictions/constraints apply to the same target weights."""
    name: str
    apply_costs: bool = True
    enforce_short_eligible: bool = True   # zero a short on a name flagged short_eligible=False
    ban_all_shorts: bool = False          # the 2025-style blanket short-ban (stress)
    block_limit_lock: bool = False        # cannot rebalance into a limit-locked name (carry prior)
    cost_multiplier: float = 1.0          # crowding / impact uplift (stress)


RESEARCH = BookConfig(name="research", apply_costs=False, enforce_short_eligible=False,
                      ban_all_shorts=False, block_limit_lock=False)
VENUE_FEASIBLE = BookConfig(name="venue_feasible", apply_costs=True, enforce_short_eligible=True,
                            ban_all_shorts=False, block_limit_lock=True)
STRESS = BookConfig(name="stress", apply_costs=True, enforce_short_eligible=True,
                    ban_all_shorts=True, block_limit_lock=True, cost_multiplier=3.0)

BOOKS = {b.name: b for b in (RESEARCH, VENUE_FEASIBLE, STRESS)}


@dataclass(frozen=True)
class BacktestResult:
    """A book's realized P&L and the honesty stats the registry logs."""
    book: str
    pnl: pd.Series                  # net per-period P&L
    gross_pnl: pd.Series
    held_weights: pd.DataFrame      # the *effective* weights after constraints (audit)
    n_periods: int
    gross_sharpe: float
    net_sharpe: float
    total_net_return: float
    avg_turnover: float
    avg_gross_exposure: float

    def summary(self) -> dict:
        return {
            "book": self.book, "n_periods": self.n_periods,
            "gross_sharpe": self.gross_sharpe, "net_sharpe": self.net_sharpe,
            "total_net_return": self.total_net_return, "avg_turnover": self.avg_turnover,
            "avg_gross_exposure": self.avg_gross_exposure,
        }


def _align(weights: pd.DataFrame, other: pd.DataFrame | None) -> pd.DataFrame | None:
    """Reindex a constraint/return panel onto the weights' (index, columns); missing ⇒ neutral."""
    if other is None:
        return None
    return other.reindex(index=weights.index, columns=weights.columns)


def apply_book_constraints(
    weights: pd.DataFrame,
    *,
    book: BookConfig,
    short_eligible: pd.DataFrame | None = None,
    limit_lock: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Turn *target* weights into the *effective* weights a book could actually hold.

    Order: (1) short bans — ``ban_all_shorts`` clips every weight to ≥0; else
    ``enforce_short_eligible`` clips to ≥0 only where ``short_eligible`` is explicitly False;
    (2) limit-lock — on a (date, name) flagged locked, you cannot trade, so the **prior**
    period's effective weight is carried forward (the target is ignored until the lock clears).
    Constraints only ever *reduce* tradability; weights are not renormalized (the lost exposure
    is real, not papered over)."""
    se = _align(weights, short_eligible)
    ll = _align(weights, limit_lock)

    w = weights.copy().astype(float)
    if book.ban_all_shorts:
        w = w.clip(lower=0.0)
    elif book.enforce_short_eligible and se is not None:
        banned = se == False  # noqa: E712 — explicit False only; NaN/unknown left tradable
        w = w.mask(banned & (w < 0), 0.0)

    if book.block_limit_lock and ll is not None:
        locked = ll == True  # noqa: E712
        held = w.copy()
        prev = pd.Series(0.0, index=w.columns)
        for t in held.index:
            row = held.loc[t].copy()
            lk = locked.loc[t].fillna(False)
            row[lk] = prev[lk]      # locked names keep what they already held
            held.loc[t] = row
            prev = row
        w = held
    return w


def run_book(
    weights: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    *,
    book: BookConfig,
    cost_model: CostModel | None = None,
    short_eligible: pd.DataFrame | None = None,
    limit_lock: pd.DataFrame | None = None,
    notional_scale: float = 1.0,
) -> BacktestResult:
    """Run ``weights`` against realized forward returns under one book.

    ``weights`` is (rebalance-date × symbol) target weights decided with info ≤ that date;
    ``fwd_returns.loc[t, i]`` is the return earned by holding name ``i`` over the period that
    starts at ``t`` (the caller aligns the shift — the backtester never peeks). Per period:
    ``gross_t = Σ_i w*_t,i · r_t,i`` on the *effective* (post-constraint) weights;
    ``cost_t = cost_rate · Σ_i |Δw*_t,i|``; ``borrow_t = borrow_rate · Σ_i max(-w*_t,i, 0)``;
    ``net_t = gross_t − cost_t − borrow_t``. ``notional_scale`` multiplies the book (used by the
    capacity curve) — costs scale with it too, so net Sharpe decays as size grows."""
    cm = cost_model or CostModel()
    held = apply_book_constraints(weights, book=book, short_eligible=short_eligible,
                                  limit_lock=limit_lock) * float(notional_scale)
    r = _align(held, fwd_returns).fillna(0.0)

    gross = (held * r).sum(axis=1)

    # turnover vs the previously-held weights (first period trades up from flat)
    prev = held.shift(1).fillna(0.0)
    turnover = (held - prev).abs().sum(axis=1)
    short_notional = held.clip(upper=0.0).abs().sum(axis=1)

    if book.apply_costs:
        cost = cm.cost_rate * book.cost_multiplier * turnover
        borrow = cm.borrow_rate_per_period * short_notional
    else:
        cost = pd.Series(0.0, index=held.index)
        borrow = pd.Series(0.0, index=held.index)

    net = gross - cost - borrow
    gross_expo = held.abs().sum(axis=1)
    return BacktestResult(
        book=book.name, pnl=net, gross_pnl=gross, held_weights=held,
        n_periods=int(len(net)),
        gross_sharpe=sharpe_ratio(gross.to_numpy()),
        net_sharpe=sharpe_ratio(net.to_numpy()),
        total_net_return=float(net.sum()),
        avg_turnover=float(turnover.mean()),
        avg_gross_exposure=float(gross_expo.mean()),
    )


def run_all_books(
    weights: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    *,
    cost_model: CostModel | None = None,
    short_eligible: pd.DataFrame | None = None,
    limit_lock: pd.DataFrame | None = None,
) -> dict[str, BacktestResult]:
    """Run the same weights through all three books (research / venue_feasible / stress)."""
    return {
        name: run_book(weights, fwd_returns, book=cfg, cost_model=cost_model,
                       short_eligible=short_eligible, limit_lock=limit_lock)
        for name, cfg in BOOKS.items()
    }


@dataclass(frozen=True)
class CapacityPoint:
    notional_scale: float
    net_sharpe: float
    total_net_return: float


def capacity_curve(
    weights: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    *,
    book: BookConfig = VENUE_FEASIBLE,
    scales=(0.5, 1.0, 2.0, 5.0, 10.0),
    cost_model: CostModel | None = None,
    short_eligible: pd.DataFrame | None = None,
    limit_lock: pd.DataFrame | None = None,
) -> list[CapacityPoint]:
    """Net Sharpe / net return as a function of notional scale — the capacity curve. With linear
    costs the *net Sharpe* is scale-invariant unless costs are present (they are, in the
    venue/stress books), so this surfaces where scaling stops paying. A flat capacity floor
    that the signal must clear is checked downstream in the promotion gate."""
    out: list[CapacityPoint] = []
    for s in scales:
        res = run_book(weights, fwd_returns, book=book, cost_model=cost_model,
                       short_eligible=short_eligible, limit_lock=limit_lock, notional_scale=s)
        out.append(CapacityPoint(notional_scale=float(s), net_sharpe=res.net_sharpe,
                                 total_net_return=res.total_net_return))
    return out

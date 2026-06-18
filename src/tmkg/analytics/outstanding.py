"""Time-aware *outstanding* debt — computed, not fetched.

`Security.nominal` is the amount **issued**. What a refinancing-wall analysis
actually needs is the amount **outstanding as of a date** — the cash that must
be found when the instrument comes due. The gap between the two has two causes:

  1. The instrument has already **matured** → outstanding is 0 (it's gone).
  2. The instrument **amortizes** → outstanding has stepped down below nominal.

Crucially, the priced universe here is Turkish corporate paper — finansman
bonosu (TRF), short tahvil (TRS), kira sertifikası (TRD) — which is almost
entirely **bullet** (full face repaid at maturity, no amortization). For a
bullet, outstanding == nominal right up to maturity, then 0. So outstanding is a
pure function of (nominal, maturity_date, as_of) plus a bullet/amortizing flag —
all stored on the node. No live balance feed needed, and a graph queried months
later still gives the correct as-of figure because matured paper drops out
automatically.

This is the core that lets the graph go stale-gracefully: the only thing it
*can't* know without fresh data is instruments issued after the snapshot.

Honesty about amortizers: we do NOT have per-instrument redemption schedules, so
for an amortizing instrument we return nominal but tag the basis
'amortizing-upper-bound' — callers must surface it separately and never fold it
into a confident outstanding sum (it would overstate).
"""
from __future__ import annotations

import datetime as _dt

# Turkish corporate debt instrument classes that are bullet (full face at
# maturity). Eurobonds (XS) are excluded — some amortize / are perpetual, and we
# can't assume bullet, so they fall through to 'unknown' and get flagged.
_BULLET_CLASSES = {
    "TRF", "TRS", "TRD",                       # MKK ISIN classes
    "FINANCING_BILL", "BOND", "SUKUK",         # mapped instrument types
    "BILL",
}
_AMORTIZING_HINT_CLASSES = {"XS", "EUROBOND"}  # treat as not-confidently-bullet


def is_amortizing_default(instrument_class: str | None) -> bool | None:
    """Class-based bullet/amortizing guess used when the Security carries no
    explicit is_amortizing flag. Returns False (bullet) for TR corporate classes,
    True for eurobond-style classes, None when the class is unknown."""
    if not instrument_class:
        return None
    c = instrument_class.upper()
    if c in _BULLET_CLASSES:
        return False
    if c in _AMORTIZING_HINT_CLASSES:
        return True
    return None


# The nominal itself is an upper bound (the amount ISSUED, not outstanding) — set
# on the Security as `nominal_basis` by the source loader. An FX eurobond priced at
# issue size is the case in point: even a live, non-matured one can't be summed
# confidently because we hold issue size, not the current balance.
NOMINAL_UPPER_BOUND_BASES = {"fx-issue-size-upper-bound"}


def outstanding_as_of(
    nominal: float | None,
    maturity_date: _dt.date | None,
    as_of: _dt.date,
    is_amortizing: bool | None,
    instrument_class: str | None = None,
    nominal_basis: str | None = None,
) -> tuple[float | None, str]:
    """Outstanding amount of one instrument as of `as_of`.

    Returns (amount, basis) where basis is one of:
      'unpriced'                 – no nominal known (amount None)
      'matured'                  – matured on/before as_of (amount 0.0)
      'bullet'                   – live bullet; outstanding == nominal (confident)
      'fx-issue-size-upper-bound'– live FX paper priced at ISSUE size; upper bound
      'amortizing-upper-bound'   – live amortizer; nominal is an UPPER bound only
      'unknown-repayment'        – live, but bullet/amortizing undetermined → upper bound

    A `nominal_basis` already marking the stored amount as an upper bound (e.g. an
    FX issue size) wins over the bullet/amortizing guess: an issue-size figure is
    never a confident outstanding total even for a live bullet.
    """
    if nominal is None:
        return None, "unpriced"
    if maturity_date is not None and maturity_date <= as_of:
        return 0.0, "matured"

    if nominal_basis in NOMINAL_UPPER_BOUND_BASES:
        return float(nominal), nominal_basis

    flag = is_amortizing
    if flag is None:
        flag = is_amortizing_default(instrument_class)

    if flag is False:
        return float(nominal), "bullet"
    if flag is True:
        return float(nominal), "amortizing-upper-bound"
    return float(nominal), "unknown-repayment"


# Bases whose amount is exact and may be summed into a confident outstanding total.
CONFIDENT_BASES = {"bullet"}
# Bases that are an upper bound only (live, priced, but possibly < nominal).
UPPER_BOUND_BASES = {
    "amortizing-upper-bound", "unknown-repayment", "fx-issue-size-upper-bound",
}

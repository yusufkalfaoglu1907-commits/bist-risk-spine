"""USD-primary total-return construction (BUILD_PLAN.md M1).

The one input every signal shares — and the easiest thing to get silently wrong.
This module is **pure** (DataFrame in, DataFrame out, no network, no L2, no PIT):
ingestion lands bars/FX/dividends, this constructs the clean series, and the
signal layer reads the result back through ``tmkg.pit.PITAccess`` only.

Three correctness rules baked in here:

1. **Corporate-action adjustment is in the price series, not bolted on.** Matriks
   daily bars are back-adjusted for splits/bonus(bedelsiz)/rights(bedelli) (W7).
   So a simple close-to-close return across a capital-increase ex-date carries NO
   dilution gap — it is already a real economic return. The golden masters prove
   this (EREGL 2024-11-27, ASELS 2023-08-25). We do NOT re-adjust; doing so would
   double-count.

2. **Dividends are added as a YIELD, never as a back-adjusted cash amount.** The
   back-adjusted close lives on a rescaled basis; a nominal-TL dividend does not.
   Adding nominal cash to an adjusted close is the classic basis trap. So the
   total return on an ex-dividend date t is::

       TR_t = close_t / close_{t-1} - 1 + y_t

   where ``y_t = cash_dividend / raw_close_on_ex_date`` is basis-invariant (see
   ``dividend_yields_from_raw``). The ingestion layer computes ``y_t`` from RAW
   (unadjusted) bars; this constructor only consumes the yield.

3. **No fabrication on gaps (§4).** A price date with no aligned FX rate yields a
   ``NaN`` ret_usd for that step — never an interpolated/carried rate. Same for the
   real-TRY cross-check when CPI is absent: the column is left null, not invented.
"""
from __future__ import annotations

from datetime import date
from typing import Mapping

import pandas as pd


def dividend_yields_from_raw(
    dividends: Mapping[date, float], raw_close: Mapping[date, float]
) -> dict[date, float]:
    """Convert nominal cash dividends to basis-invariant yields.

    ``dividends``  : {ex_date -> cash dividend per share, in the SAME nominal TL the
                     vendor reports it (net, typically)}.
    ``raw_close``  : {date -> UNADJUSTED close} — the raw bar on the ex-date. The
                     yield ``cash / raw_close[ex_date]`` is invariant to any later
                     back-adjustment, so it can be applied to the adjusted series.

    An ex-date with no raw close available is **refused** (raised), not guessed —
    a fabricated yield would silently corrupt the total return (§4).
    """
    out: dict[date, float] = {}
    for ex, cash in dividends.items():
        if ex not in raw_close or raw_close[ex] in (None, 0):
            raise ValueError(
                f"no raw close for dividend ex-date {ex}; refusing to fabricate a yield"
            )
        out[ex] = cash / raw_close[ex]
    return out


def compute_total_returns(
    prices: pd.DataFrame,
    fx: pd.DataFrame | None = None,
    *,
    symbol: str | None = None,
    dividend_yields: Mapping[date, float] | None = None,
    cpi: pd.DataFrame | None = None,
    knowledge_date: Mapping[date, date] | None = None,
) -> pd.DataFrame:
    """Construct a USD-primary total-return frame for one symbol.

    Parameters
    ----------
    prices : columns ``[bar_date, close]`` (+ optional ``symbol``). ``close`` is the
        corporate-action-adjusted (back-adjusted) series. Sorted internally.
    fx : columns ``[bar_date, close]`` — USD/TRY (TRY per 1 USD). Required for
        ``ret_usd``; when a bar_date has no FX row, ``ret_usd`` is NaN for that step.
    symbol : overrides / supplies the symbol if not a column on ``prices``.
    dividend_yields : {ex_date -> yield} from :func:`dividend_yields_from_raw`,
        added to that date's return to make it a TOTAL return.
    cpi : columns ``[bar_date, value]`` — TR CPI index for the real-TRY cross-check.
        When absent, ``ret_real_try`` is left null (never fabricated).
    knowledge_date : {bar_date -> knowledge_date} override; default = bar_date (a
        daily close is known end-of-that-day).

    Returns a frame matching the L2 ``total_returns`` schema (one row per bar_date,
    first date dropped as it has no prior close). ``limit_lock_adj`` defaults False
    (limit-lock censoring is a later M1 slice).
    """
    if prices.empty:
        raise ValueError("compute_total_returns: empty prices frame")
    sym = symbol or (prices["symbol"].iloc[0] if "symbol" in prices.columns else None)
    if sym is None:
        raise ValueError("compute_total_returns: no symbol (pass symbol= or a column)")

    p = prices[["bar_date", "close"]].copy()
    p["bar_date"] = pd.to_datetime(p["bar_date"]).dt.date
    p = p.sort_values("bar_date").drop_duplicates("bar_date").reset_index(drop=True)

    dy = {pd.Timestamp(k).date(): float(v) for k, v in (dividend_yields or {}).items()}
    yld = p["bar_date"].map(dy).fillna(0.0)

    price_ret = p["close"].pct_change()
    p["ret_nominal_try"] = price_ret + yld

    # USD-primary: convert close to USD first, then return (so the dividend yield,
    # being a fraction of price, carries through to USD unchanged).
    if fx is not None and not fx.empty:
        f = fx[["bar_date", "close"]].copy()
        f["bar_date"] = pd.to_datetime(f["bar_date"]).dt.date
        f = f.rename(columns={"close": "fx"}).drop_duplicates("bar_date")
        p = p.merge(f, on="bar_date", how="left")
        close_usd = p["close"] / p["fx"]                 # NaN where fx missing
        p["ret_usd"] = close_usd.pct_change() + yld
    else:
        p["ret_usd"] = pd.NA

    # Real-TRY cross-check: deflate the nominal return by realised CPI inflation.
    if cpi is not None and not cpi.empty:
        c = cpi[["bar_date", "value"]].copy()
        c["bar_date"] = pd.to_datetime(c["bar_date"]).dt.date
        c = c.rename(columns={"value": "cpi"}).drop_duplicates("bar_date")
        p = p.merge(c, on="bar_date", how="left")
        infl = p["cpi"].pct_change()
        p["ret_real_try"] = (1.0 + p["ret_nominal_try"]) / (1.0 + infl) - 1.0
    else:
        p["ret_real_try"] = pd.NA

    p["symbol"] = sym
    p["limit_lock_adj"] = False
    kd = knowledge_date or {}
    p["knowledge_date"] = p["bar_date"].map(lambda d: kd.get(d, d))

    out = p.iloc[1:].copy()  # first row has no prior close
    return out[
        [
            "symbol",
            "bar_date",
            "ret_usd",
            "ret_real_try",
            "ret_nominal_try",
            "limit_lock_adj",
            "knowledge_date",
        ]
    ].reset_index(drop=True)

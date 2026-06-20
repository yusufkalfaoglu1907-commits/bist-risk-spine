"""M1 end-to-end ingestion pipeline (BUILD_PLAN.md M1).

Ties the verified pieces into one reproducible run:

    MatriksAdapter.fetch        <- the ONLY network hop (§4)
        -> parse_bars            (historicalData payload -> prices/factor rows)
        -> L2Store.write_parquet (bitemporal `prices` + `factors`, PK-idempotent)
        -> PITAccess.series      (PIT-correct read of the just-landed inputs)
        -> compute_total_returns (pure USD-primary constructor; tmkg.returns)
        -> L2Store.write_parquet (`total_returns`)
        -> write_run_report      (data/cache audit, §4)

Why total returns are built *through* PITAccess and not from the raw frames: the
return series must be derivable from exactly what was knowable at ``as_of`` — so the
constructor's inputs come back through the same knowledge_date <= as_of gate that
signal code uses. Nothing here invents data: a missing FX bar yields NaN ret_usd
(never an interpolated rate), and an unreachable source raises in ``fetch`` (§4).

Signal code never imports this module — it reads the resulting L2 tables through
``tmkg.pit.PITAccess``.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

# historicalData returns DOWNSAMPLED `sampledBars` unless rawBars=True is set, and
# caps the count at `limit`. We always demand the raw, complete daily series — a
# sampled series is a silently-fabricated return path (§4). `limit` must cover every
# trading day in the window; calendar-day count is a safe overestimate (trading days
# < calendar days), and the API caps at the bars actually available.
_RAW_BARS = True

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.evds import CPI_TUFE_FACTOR, CPI_TUFE_SERIES, EvdsAdapter
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess
from tmkg.returns.limit_lock import flag_limit_lock
from tmkg.returns.staleness import flag_staleness
from tmkg.returns.total_return import compute_total_returns

_DATE_COLS = ("bar_date", "knowledge_date")
_RET_COLS = ("ret_usd", "ret_real_try", "ret_nominal_try")


def _coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    """DuckDB DATE columns want python ``date`` objects, not strings/Timestamps."""
    for c in _DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def _window_limit(start: str, end: str) -> int:
    """A bar `limit` that safely covers every trading day in [start, end]."""
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _fetch_bars(adapter, symbol, start, end, interval):
    """Fetch the RAW, complete daily bars for one symbol over the window."""
    return adapter.fetch(
        "historicalData",
        symbol=symbol,
        startDate=start,
        endDate=end,
        interval=interval,
        rawBars=_RAW_BARS,
        limit=_window_limit(start, end),
    )


# --- prices ----------------------------------------------------------------
def ingest_prices(
    adapter: MatriksAdapter,
    store: L2Store,
    symbol: str,
    *,
    start: str,
    end: str,
    interval: str = "daily",
    band: float = 0.10,
) -> dict:
    """Fetch one symbol's daily bars, parse, flag, and land them in L2 ``prices``.

    ``band`` is the daily price limit for the limit-lock detection (±10% standard;
    pass a wider value for market-maker names). Returns an audit summary (counts
    only — never the data). Raises on an unreachable source via ``adapter.fetch``
    (§4); never writes a partial-guess row.
    """
    payload = _fetch_bars(adapter, symbol, start, end, interval)
    rows = adapter.parse_bars(payload, symbol=symbol)
    if not rows:
        return {"symbol": symbol, "table": "prices", "n_bars": 0}
    df = _coerce_dates(pd.DataFrame(rows))
    # detection passes (pure): censored ±band days + carried-forward (no-trade) bars
    df = flag_limit_lock(df, band=band)
    df = flag_staleness(df)
    store.write_parquet("prices", df)
    return {
        "symbol": symbol,
        "table": "prices",
        "n_bars": len(df),
        "first_bar": str(df["bar_date"].min()),
        "last_bar": str(df["bar_date"].max()),
        "n_limit_lock": int(df["is_limit_lock"].sum()),
        "n_stale": int(df["is_stale"].sum()),
    }


# --- factors (FX / index series) -------------------------------------------
def ingest_factor_series(
    adapter: MatriksAdapter,
    store: L2Store,
    factor: str,
    symbol: str,
    *,
    start: str,
    end: str,
    interval: str = "daily",
) -> dict:
    """Fetch a market/FX series (e.g. USDTRY) and land it in L2 ``factors``.

    The bars' close becomes ``factors.value``; ``ret`` is left NULL here (factor
    returns are an M2 concern). FX has no turnover, so the FX-shaped ``bars`` payload
    (date+close only) is expected — ``parse_bars`` handles it.
    """
    payload = _fetch_bars(adapter, symbol, start, end, interval)
    bars = adapter.parse_bars(payload, symbol=symbol)
    rows = [
        {
            "factor": factor,
            "bar_date": b["bar_date"],
            "value": b["close"],
            "ret": None,
            "knowledge_date": b["knowledge_date"],
            "source": b["source"],
        }
        for b in bars
        if b["close"] is not None  # a close-less bar is not a usable factor point
    ]
    if not rows:
        return {"factor": factor, "table": "factors", "n_points": 0}
    df = _coerce_dates(pd.DataFrame(rows))
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    store.write_parquet("factors", df)
    return {
        "factor": factor,
        "table": "factors",
        "n_points": len(df),
        "first": str(df["bar_date"].min()),
        "last": str(df["bar_date"].max()),
    }


# --- CPI (EVDS macro series -> factors, the real-TRY deflator) --------------
def ingest_cpi(
    adapter: EvdsAdapter,
    store: L2Store,
    *,
    start: str,
    end: str,
    series: str = CPI_TUFE_SERIES,
    factor: str = CPI_TUFE_FACTOR,
) -> dict:
    """Fetch the TÜFE/CPI series from EVDS and land it in L2 ``factors``.

    The deflator for the CPI-real-TRY cross-check (CLAUDE.md §5). ``parse_cpi``
    already yields ``factors``-schema rows with a PIT-honest ``knowledge_date`` (the
    TÜİK release, ~3rd of the next month) — so a backtest cannot deflate a month by a
    CPI print it could not yet have seen. ``ret`` is left NULL (inflation is derived
    in the return constructor, never stored). Raises on an unreachable source via
    ``adapter.fetch`` (§4); a blank reading is dropped in ``parse_cpi``, never guessed.
    """
    payload = adapter.fetch(series, start=start, end=end)
    rows = adapter.parse_cpi(payload, series=series, factor=factor)
    df = _coerce_dates(pd.DataFrame(rows))
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    store.write_parquet("factors", df)
    return {
        "factor": factor,
        "table": "factors",
        "series": series,
        "n_points": len(df),
        "first": str(df["bar_date"].min()),
        "last": str(df["bar_date"].max()),
    }


# --- total returns (read inputs through PIT, construct, land) ---------------
def build_total_returns(
    store: L2Store,
    symbol: str,
    *,
    as_of: date,
    fx_factor: str | None = "USDTRY",
    cpi_factor: str | None = CPI_TUFE_FACTOR,
    dividend_yields: dict | None = None,
) -> dict:
    """Build and land the USD-primary ``total_returns`` for ``symbol`` as of ``as_of``.

    Inputs are read back through ``PITAccess`` so the series can only depend on bars
    whose ``knowledge_date <= as_of`` — the same gate signal code is held to. FX and
    CPI both live in L2 ``factors`` (selected by name); the CPI deflator carries the
    TÜİK release as its ``knowledge_date``, so a month's ``ret_real_try`` only lands
    once that month's CPI print was knowable at ``as_of`` (else the column stays NULL,
    never an interpolated deflator — §4). The pure ``compute_total_returns`` does the
    financial construction; this function is the L2/PIT plumbing around it.
    """
    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        prices = pit.series("prices", symbol=symbol)
        fx = None
        if fx_factor is not None:
            fx = pit.series("factors", where=f"factor = '{fx_factor}'")
        cpi = None
        if cpi_factor is not None:
            cpi = pit.series("factors", where=f"factor = '{cpi_factor}'")
    finally:
        con.close()

    if prices.empty:
        return {"symbol": symbol, "table": "total_returns", "n_returns": 0,
                "note": f"no prices visible as of {as_of}"}

    fx_frame = None
    if fx is not None and not fx.empty:
        fx_frame = fx.rename(columns={"value": "close"})[["bar_date", "close"]]

    cpi_frame = None
    if cpi is not None and not cpi.empty:
        cpi_frame = cpi[["bar_date", "value"]]

    tr = compute_total_returns(
        prices, fx_frame, symbol=symbol,
        dividend_yields=dividend_yields, cpi=cpi_frame,
    )
    tr = _coerce_dates(tr)
    for c in _RET_COLS:  # pd.NA (object) -> float NaN so DuckDB sees a DOUBLE NULL
        tr[c] = pd.to_numeric(tr[c], errors="coerce")
    store.write_parquet("total_returns", tr)
    return {
        "symbol": symbol,
        "table": "total_returns",
        "n_returns": len(tr),
        "as_of": str(as_of),
        "fx_factor": fx_factor,
        "cpi_factor": cpi_factor,
        "ret_usd_null": int(tr["ret_usd"].isna().sum()),
        "ret_real_try_null": int(tr["ret_real_try"].isna().sum()),
    }


# --- orchestration ---------------------------------------------------------
def run_m1_ingestion(
    adapter: MatriksAdapter,
    store: L2Store,
    *,
    symbols: list[str],
    start: str,
    end: str,
    as_of: date,
    fx: tuple[str, str] = ("USDTRY", "USDTRY"),
    cpi_adapter: EvdsAdapter | None = None,
    cpi_window: tuple[str, str] | None = None,
) -> dict:
    """One M1 run: land prices for ``symbols`` + the FX factor (and, when a
    ``cpi_adapter`` is given, the CPI deflator from EVDS), build total returns for
    each symbol as of ``as_of``, and write a single audit report (§4).

    ``fx = (factor_name, symbol)``. CPI is a separate source (EVDS, not Matriks), so
    it takes its own ``cpi_adapter``; ``cpi_window`` defaults to the price window but
    a wider one is usually wanted (CPI is monthly, and a month's deflator needs the
    prior month too). Stops loud on the first unreachable source — a partial run is
    logged, never silently completed with fabricated gaps.
    """
    store.bootstrap_schema()
    report: dict = {"as_of": str(as_of), "window": [start, end],
                    "prices": [], "factors": [], "total_returns": []}

    factor_name, fx_symbol = fx
    report["factors"].append(
        ingest_factor_series(adapter, store, factor_name, fx_symbol, start=start, end=end)
    )
    if cpi_adapter is not None:
        c_start, c_end = cpi_window or (start, end)
        report["factors"].append(
            ingest_cpi(cpi_adapter, store, start=c_start, end=c_end)
        )
    for sym in symbols:
        report["prices"].append(
            ingest_prices(adapter, store, sym, start=start, end=end)
        )
    for sym in symbols:
        report["total_returns"].append(
            build_total_returns(store, sym, as_of=as_of, fx_factor=factor_name)
        )

    write_run_report("m1_ingestion", report)
    return report

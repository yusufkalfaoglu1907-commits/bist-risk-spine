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
from tmkg.ingest.evds import (
    CPI_TUFE_FACTOR,
    CPI_TUFE_SERIES,
    FOREIGN_FLOW_FACTOR,
    FOREIGN_FLOW_SERIES,
    FOREIGN_FLOW_STOCK_FACTOR,
    FOREIGN_FLOW_STOCK_SERIES,
    EvdsAdapter,
)
from tmkg.ingest.fred import VIX_FACTOR, VIX_SERIES, FredAdapter
from tmkg.ingest.gdelt import GdeltAdapter, gkg_records_to_l2_rows
from tmkg.factors import registry
from tmkg.factors.betas import rolling_factor_betas
from tmkg.factors.neutralize import rolling_residuals
from tmkg.factors.series import compute_factor_returns
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.worldgovbonds import WGB_FACTORS, WorldGovBondsAdapter
from tmkg.l2.store import L2Store
from tmkg.pit.access import PITAccess
from tmkg.returns.accounting_regime import regime_for_period
from tmkg.returns.limit_lock import flag_limit_lock
from tmkg.returns.staleness import flag_staleness
from tmkg.returns.total_return import compute_total_returns

_DATE_COLS = ("bar_date", "knowledge_date", "event_date")
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


# --- rates/CDS rung (WorldGovernmentBonds: Turkey 5y CDS + 2y/10y bond yields) ---
def ingest_wgb_factor(
    adapter: WorldGovBondsAdapter,
    store: L2Store,
    *,
    factor: str,
    start: str,
    end: str,
) -> dict:
    """Fetch a WorldGovernmentBonds rate factor (``TRCDS5Y`` CDS in bps, or ``TRY2Y``/
    ``TRY10Y`` bond yields in %) and land it in L2 ``factors``. The per-factor FUNCTION/tenor
    lives in ``worldgovbonds.WGB_FACTORS``. ``knowledge_date = bar_date`` (a daily close is
    known end-of-day); ``ret`` left null (a rate level -> ``series.DIFF`` at read time).
    Raises on an unreachable source via ``adapter.fetch`` (§4); WGB carries values over
    non-trading days, which ``diff`` correctly reads as a zero change."""
    cfg = WGB_FACTORS[factor]
    result = adapter.fetch(
        function=cfg["function"], durata=cfg["durata"],
        durata_string=cfg["durata_string"])
    rows = adapter.parse_series(result, factor=factor, start=start, end=end)
    df = _coerce_dates(pd.DataFrame(rows))
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    store.write_parquet("factors", df)
    return {
        "factor": factor, "table": "factors", "source": "worldgovbonds",
        "n_points": len(df), "first": str(df["bar_date"].min()),
        "last": str(df["bar_date"].max()),
    }


# --- foreign-flow (EVDS weekly non-resident equity flow -> the §5 driver) ---
def ingest_foreign_flow(
    adapter: EvdsAdapter,
    store: L2Store,
    *,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch the EVDS weekly non-resident equity series and land them in L2 ``factors``.

    Two series: ``FFLOW`` = TP.MKNETHAR.M7 (weekly net non-resident equity FLOW, USD mn —
    the §5 foreign-flow factor; a flow, consumed with ``series.LEVEL``) and ``FFLOW_STOCK``
    = TP.MKNETHAR.M1 (the holdings LEVEL, for normalization / cross-check). The true WEEKLY
    observations are stored — no forward-fill onto a daily calendar here (§4); the weekly→
    daily alignment is a transient step at panel-build. ``knowledge_date`` carries the
    release lag (PIT-honest). Raises on an unreachable source via ``adapter.fetch`` (§4).
    """
    out: list[dict] = []
    for series, factor in (
        (FOREIGN_FLOW_SERIES, FOREIGN_FLOW_FACTOR),
        (FOREIGN_FLOW_STOCK_SERIES, FOREIGN_FLOW_STOCK_FACTOR),
    ):
        payload = adapter.fetch(series, start=start, end=end)
        rows = adapter.parse_weekly_series(payload, series=series, factor=factor)
        df = _coerce_dates(pd.DataFrame(rows))
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        store.write_parquet("factors", df)
        out.append({
            "factor": factor, "table": "factors", "series": series,
            "n_points": len(df), "first": str(df["bar_date"].min()),
            "last": str(df["bar_date"].max()),
        })
    return out


# --- FRED macro series (VIX etc. -> factors, the global-risk leg) -----------
def ingest_fred_series(
    adapter: FredAdapter,
    store: L2Store,
    *,
    start: str,
    end: str,
    series: str = VIX_SERIES,
    factor: str = VIX_FACTOR,
) -> dict:
    """Fetch a FRED macro series (default VIX/VIXCLS) and land it in L2 ``factors``.

    The global-risk leg of the M2 factor set (design §7.1 "VIX (FRED)"). ``knowledge_date``
    = ``bar_date`` (a market index close is known end-of-day and never revised). ``ret`` is
    left NULL (factor returns are derived on read in M2). Raises on an unreachable source
    via ``adapter.fetch`` (§4); FRED's ``"."`` missing-day sentinel is dropped in
    ``parse_observations``, never interpolated.
    """
    payload = adapter.fetch(series, start=start, end=end)
    rows = adapter.parse_observations(payload, series=series, factor=factor)
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


# --- GDELT events (geopolitical event stream -> L2 events + event_targets) --
_EVENTS_COLS = (
    "event_id", "event_date", "date_precision", "event_type", "actors",
    "geography", "severity", "source", "knowledge_date",
)
_EVENT_TARGETS_COLS = (
    "event_id", "channel", "shock_sign", "shock_magnitude", "confidence",
    "evidence_tier", "uncertainty", "source", "knowledge_date",
)


def _land_gdelt_records(store: L2Store, records: list[dict], *, window: str) -> dict:
    """Land parsed GKG records into L2 ``events`` + ``event_targets`` (no network — testable).

    Confidence-tiered (§6): only typed Turkey events are written; non-Turkey / untyped records
    are counted in the audit report, never guessed into a type. Both writes are PK-idempotent,
    so a re-land of the same ``(event_id, knowledge_date)`` is ignored (bitemporal append-only).
    The ``event_targets`` rows are the inferred-tier prior seed (LLM override is a later step).
    """
    event_rows, target_rows, skipped = gkg_records_to_l2_rows(records)
    if event_rows:
        store.write_parquet(
            "events", _coerce_dates(pd.DataFrame(event_rows, columns=list(_EVENTS_COLS)))
        )
    if target_rows:
        store.write_parquet(
            "event_targets",
            _coerce_dates(pd.DataFrame(target_rows, columns=list(_EVENT_TARGETS_COLS))),
        )
    report = {
        "source": "gdelt",
        "table": "events+event_targets",
        "window": window,
        "n_events": len(event_rows),
        "n_targets": len(target_rows),
        "skipped": skipped,
    }
    write_run_report("gdelt_ingestion", report)
    return report


def ingest_gdelt_events(
    adapter: GdeltAdapter, store: L2Store, *, start: date, end: date
) -> dict:
    """Pull the Turkey GKG event stream over ``[start, end]`` and land it in L2 (M6).

    The one network-touching M6 step (§4): ``adapter.fetch`` crawls the raw 15-minute GKG feed
    and returns the Turkey-filtered records; ``_land_gdelt_records`` types them and writes the
    ``events`` / ``event_targets`` rows. ``event_date = knowledge_date = V2.1DATE`` (the GKG
    publication date — a PIT read never sees an event before the news carrying it was published).
    Raises ``SourceUnreachable`` on a transport failure; missing 15-minute slots are skipped.
    """
    records = adapter.fetch(start=start, end=end)
    return _land_gdelt_records(store, records, window=f"{start}..{end}")


# --- accounting_regime (fundamentals declaration dates -> regime tags) ------
def ingest_accounting_regime(
    adapter: MatriksAdapter,
    store: L2Store,
    symbol: str,
) -> dict:
    """Tag every declared fundamental period of ``symbol`` with its accounting_regime
    and land it in L2 ``accounting_regime`` (CLAUDE.md §5, design §3).

    The consumer the regime state machine was missing: it pulls the vendor's
    declaration-date history (``fundamentalAnalysis(includeDeclarationDates=True)``),
    maps each period to ``regime_for_period`` (nominal_pre2023 / ias29_2023_2024 /
    suspended_2025_2027), and sets ``knowledge_date = declarationDate`` so a period is
    invisible until it was actually declared — the regime tag inherits the same PIT
    gate as the fundamental it describes. Raises on an unreachable source (§4); a
    period with no declaration date is dropped in ``parse_declaration_periods``.
    """
    payload = adapter.fetch(
        "fundamentalAnalysis", symbol=symbol, includeDeclarationDates=True
    )
    decls = adapter.parse_declaration_periods(payload, symbol=symbol)
    rows = [
        {
            "symbol": d["symbol"],
            "period": d["period"],
            "regime": regime_for_period(d["period"]),
            "knowledge_date": date.fromisoformat(d["declaration_date"]),
        }
        for d in decls
    ]
    if not rows:
        return {"symbol": symbol, "table": "accounting_regime", "n_periods": 0}
    df = _coerce_dates(pd.DataFrame(rows))
    store.write_parquet("accounting_regime", df)
    return {
        "symbol": symbol,
        "table": "accounting_regime",
        "n_periods": len(df),
        "first_period": df["period"].min(),
        "last_period": df["period"].max(),
        "regimes": sorted(df["regime"].unique().tolist()),
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
    overwrite: bool = False,
) -> dict:
    """Build and land the USD-primary ``total_returns`` for ``symbol`` as of ``as_of``.

    Inputs are read back through ``PITAccess`` so the series can only depend on bars
    whose ``knowledge_date <= as_of`` — the same gate signal code is held to. FX and
    CPI both live in L2 ``factors`` (selected by name); the CPI deflator carries the
    TÜİK release as its ``knowledge_date``, so a month's ``ret_real_try`` only lands
    once that month's CPI print was knowable at ``as_of`` (else the column stays NULL,
    never an interpolated deflator — §4). The pure ``compute_total_returns`` does the
    financial construction; this function is the L2/PIT plumbing around it.

    ``overwrite=True`` DELETEs the symbol's existing ``total_returns`` rows over the
    recomputed ``bar_date`` span before writing, so a *correction* actually lands —
    ``write_parquet`` is ON CONFLICT DO NOTHING and cannot fix a row that was written
    with a NULL ``ret_usd`` because USDTRY was pulled after this ran (the ordering bug,
    BUILD_LOG 2026-07-01). ``total_returns`` is a *derived* table with
    ``knowledge_date = bar_date``, so re-deriving the same ``(symbol, bar_date)`` is a
    recomputation, not a bitemporal-history rewrite. Default (append-only) is unchanged.
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
    if overwrite and not tr.empty:
        # clear the recomputed span so a corrected value replaces a prior NULL/stale row
        lo, hi = str(tr["bar_date"].min()), str(tr["bar_date"].max())
        con = store.connect()
        try:
            con.execute(
                "DELETE FROM total_returns WHERE symbol = ? AND bar_date BETWEEN ? AND ?",
                [symbol, lo, hi],
            )
        finally:
            con.close()
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


# === M2: factor model + neutralization ====================================
# These builders read inputs back through PITAccess (the same gate signal code is
# held to) and land the derived betas/residuals to L2 — the precedent set by
# build_total_returns above. Factor returns are derived on read from the L2 factor
# *levels* (BUILD_LOG 2026-06-20 decision: not persisted into the append-only
# factors.ret rows), so a vintage's factor returns can only use levels knowable then.
#
# The strip order is a sequence of concrete factor *names* (XU100, USDTRY, …), not ladder
# roles — derived from ``factors.registry`` so it follows the §200 rung order without being
# retyped. ``order=None`` (the default) means "derive the name order from the registry for
# whatever factors are actually present"; an explicit ``order`` is honored verbatim.


def factor_coverage(panel: pd.DataFrame, specs: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split the configured factors into (present, missing) given a built panel.
    The basis for the "no factor silently dropped" exit-gate guard."""
    present_set = set(panel["factor"]) if not panel.empty else set()
    present = [f for f in specs if f in present_set]
    missing = [f for f in specs if f not in present_set]
    return present, missing


def _align_weekly_factor(rets: pd.DataFrame, factor: str, grid: list) -> pd.DataFrame:
    """Forward-fill a weekly factor's returns onto the daily ``grid`` (a transient alignment;
    L2 keeps the true weekly observations — §4). The week's value takes effect from its
    **knowledge_date** (release), not its bar_date (the Friday it references) — so a daily
    date gets the most recent weekly reading that was actually PUBLISHED by then, never a
    look-ahead. Falls back to bar_date if no knowledge_date is carried. Empty grid / factor
    -> empty frame (no fabricated rows)."""
    g = rets[rets["factor"] == factor].dropna(subset=["ret"]).copy()
    if g.empty or not grid:
        return pd.DataFrame(columns=["factor", "bar_date", "ret"])
    eff = g["knowledge_date"] if "knowledge_date" in g.columns else g["bar_date"]
    s = pd.Series(g["ret"].to_numpy(), index=pd.to_datetime(eff)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    daily_idx = pd.to_datetime(pd.Index(grid))
    aligned = (
        s.reindex(s.index.union(daily_idx)).ffill().reindex(daily_idx).dropna()
    )
    if aligned.empty:
        return pd.DataFrame(columns=["factor", "bar_date", "ret"])
    return pd.DataFrame({
        "factor": factor,
        "bar_date": [ts.date() for ts in aligned.index],
        "ret": aligned.to_numpy(),
    })


def build_factor_return_panel(
    store: L2Store,
    *,
    as_of: date,
    specs: dict[str, str],
    require_all: bool = False,
) -> pd.DataFrame:
    """Read L2 factor *levels* through PIT and return a long ``[factor, bar_date, ret]``
    panel. ``specs`` maps each factor name to its return method
    (``simple`` / ``log`` / ``diff`` — see ``factors.series``); only the listed factors
    are read (e.g. exclude the CPI deflator, which is not a model factor). A factor with
    no levels visible as of ``as_of`` is simply absent — never a fabricated series.

    ``require_all=True`` makes a missing configured factor a **loud failure** rather than
    a silent thinner model — the M2 exit-gate rule "no factor silently dropped" / §4. The
    caller (and the audit report) should always surface coverage via ``factor_coverage``.
    """
    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        frames = []
        for factor in specs:
            lv = pit.series("factors", where=f"factor = '{factor}'")
            if lv.empty:
                continue
            cols = ["factor", "bar_date", "value"]
            if "knowledge_date" in lv.columns:
                cols.append("knowledge_date")  # needed to align low-freq factors PIT-honestly
            frames.append(lv[cols])
    finally:
        con.close()
    if not frames:
        panel = pd.DataFrame(columns=["factor", "bar_date", "ret"])
    else:
        rets = compute_factor_returns(pd.concat(frames, ignore_index=True), method=specs)
        weekly = registry.weekly_factor_names() & set(specs)
        daily_part = (
            rets[~rets["factor"].isin(weekly)][["factor", "bar_date", "ret"]]
            .dropna(subset=["ret"])
        )
        # daily date grid the lower-freq factors are aligned onto
        grid = sorted(pd.unique(daily_part["bar_date"]))
        weekly_parts = [_align_weekly_factor(rets, wf, grid) for wf in weekly]
        panel = (
            pd.concat([daily_part, *[w for w in weekly_parts if not w.empty]], ignore_index=True)
            .sort_values(["factor", "bar_date"])
            .reset_index(drop=True)
        )
    if require_all:
        _, missing = factor_coverage(panel, specs)
        if missing:
            raise ValueError(
                f"configured factors {missing} have no levels in L2 as of {as_of}; "
                f"refusing to fit a silently-thinner model (M2 'no factor silently dropped' / §4)"
            )
    return panel


def _universe_class(pit: PITAccess, symbol: str) -> str | None:
    """The name's universe_class as known at the PIT vintage (None if unrecorded)."""
    try:
        u = pit.series("universe_membership", symbol=symbol, latest_by="valid_from")
    except Exception:
        return None
    if u.empty or "universe_class" not in u.columns:
        return None
    vals = u["universe_class"].dropna().unique().tolist()
    return vals[0] if vals else None


def _stock_returns(pit: PITAccess, symbol: str) -> pd.DataFrame:
    """USD-primary total returns for one name as a ``[bar_date, ret]`` frame."""
    tr = pit.series("total_returns", symbol=symbol)
    if tr.empty:
        return pd.DataFrame(columns=["bar_date", "ret"])
    return tr[["bar_date", "ret_usd"]].rename(columns={"ret_usd": "ret"})


def build_betas(
    store: L2Store,
    symbol: str,
    *,
    as_of: date,
    specs: dict[str, str],
    panel: pd.DataFrame | None = None,
    window: int = 60,
    min_obs: int | None = None,
    method: str = "ledoit_wolf",
) -> dict:
    """Fit rolling regime-aware factor betas for ``symbol`` as of ``as_of`` and land
    them in L2 ``betas``. Reads the name's USD total returns + the factor panel through
    PIT; tags each row with the name's PIT-known ``universe_class``. Pass a prebuilt
    ``panel`` to avoid re-reading factors per symbol in a batch run.
    """
    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        y = _stock_returns(pit, symbol)
        uclass = _universe_class(pit, symbol)
    finally:
        con.close()
    if panel is None:
        panel = build_factor_return_panel(store, as_of=as_of, specs=specs)
    if y.empty or panel.empty:
        return {"symbol": symbol, "table": "betas", "n_betas": 0,
                "note": f"no returns/factors visible as of {as_of}"}

    betas = rolling_factor_betas(
        y, panel, symbol=symbol, window=window, min_obs=min_obs,
        method=method, universe_class=uclass,
    )
    if betas.empty:
        return {"symbol": symbol, "table": "betas", "n_betas": 0,
                "note": "no full in-regime window"}
    betas = _coerce_dates(betas)
    store.write_parquet("betas", betas)
    return {
        "symbol": symbol, "table": "betas", "n_betas": len(betas),
        "as_of": str(as_of), "universe_class": uclass,
        "factors": sorted(betas["factor"].unique().tolist()),
        "regimes": sorted(betas["regime"].dropna().unique().tolist()),
    }


def build_residuals(
    store: L2Store,
    symbol: str,
    *,
    as_of: date,
    specs: dict[str, str],
    order: tuple[str, ...] | None = None,
    panel: pd.DataFrame | None = None,
    window: int = 60,
    min_obs: int | None = None,
) -> dict:
    """Compute the neutralized residual-return series for ``symbol`` as of ``as_of`` and
    land it in L2 ``residuals``. ``order`` is the concrete factor-*name* strip order; only
    factors present in the panel are stripped, and the order is recorded verbatim on each
    row (``strip_order``) so the ladder is auditable. ``order=None`` derives the name order
    from ``factors.registry`` (§200 rung order) for whatever factors actually landed.
    """
    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        y = _stock_returns(pit, symbol)
        uclass = _universe_class(pit, symbol)
    finally:
        con.close()
    if panel is None:
        panel = build_factor_return_panel(store, as_of=as_of, specs=specs)
    if y.empty or panel.empty:
        return {"symbol": symbol, "table": "residuals", "n_residuals": 0,
                "note": f"no returns/factors visible as of {as_of}"}

    # strip only factors that actually have a return series in the panel, in ladder order
    present = list(dict.fromkeys(panel["factor"]))
    if order is None:
        strip_order = registry.order_present(present)  # registry §200 rung order
    else:
        strip_order = tuple(f for f in order if f in present)  # honor explicit order verbatim
    res = rolling_residuals(
        y, panel, order=strip_order, symbol=symbol, window=window,
        min_obs=min_obs, universe_class=uclass,
    )
    if res.empty:
        return {"symbol": symbol, "table": "residuals", "n_residuals": 0,
                "note": "no full in-regime window"}
    res = _coerce_dates(res)
    store.write_parquet("residuals", res)
    return {
        "symbol": symbol, "table": "residuals", "n_residuals": len(res),
        "as_of": str(as_of), "universe_class": uclass,
        "strip_order": ">".join(strip_order),
    }


def run_m2_factor_model(
    store: L2Store,
    *,
    symbols: list[str],
    as_of: date,
    specs: dict[str, str],
    order: tuple[str, ...] | None = None,
    window: int = 60,
    min_obs: int | None = None,
    method: str = "ledoit_wolf",
    require_all_factors: bool = False,
) -> dict:
    """One M2 run: build the factor-return panel once, then fit betas and neutralized
    residuals for every name as of ``as_of``, landing both to L2 and writing a single
    audit report (§4). Reads only L2 (no network) — the inputs were landed in M1.

    ``require_all_factors=True`` enforces the exit-gate rule "no factor silently
    dropped": the run fails loud if any configured factor has no series as of ``as_of``.
    Either way the report records the present/missing split so coverage is auditable.
    """
    store.bootstrap_schema()
    panel = build_factor_return_panel(store, as_of=as_of, specs=specs,
                                      require_all=require_all_factors)
    present, missing = factor_coverage(panel, specs)
    # the effective strip order is concrete factor names in §200 rung order — derived from
    # the registry when not given explicitly, over only the factors actually present.
    effective_order = order if order is not None else registry.order_present(present)
    report: dict = {
        "as_of": str(as_of), "window": window, "method": method,
        "configured_factors": list(specs), "present_factors": present,
        "missing_factors": missing,  # surfaced, never silently dropped
        "ladder": ">".join(effective_order), "betas": [], "residuals": [],
    }
    for sym in symbols:
        report["betas"].append(
            build_betas(store, sym, as_of=as_of, specs=specs, panel=panel,
                        window=window, min_obs=min_obs, method=method)
        )
        report["residuals"].append(
            build_residuals(store, sym, as_of=as_of, specs=specs, order=effective_order,
                            panel=panel, window=window, min_obs=min_obs)
        )
    write_run_report("m2_factor_model", report)
    return report

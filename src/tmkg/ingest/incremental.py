"""Incremental update core (M9.2) — pull only what is new, and know what is stale.

A daily refresh should not re-pull years of history for every name. This computes, per symbol, the
**incremental window** to fetch (from just before the last bar we hold, to ``as_of``) and reports which
names are **stale** (their latest bar is older than a freshness budget) so the daily runner targets only
those. The window logic is pure/tested; the freshness scan is a single bulk L2 read.

Design choices:
  * an **overlap** (default 5 trading-ish days) is re-fetched before the last held bar so a late-posted
    or revised recent bar is corrected — idempotent PK writes make the overlap a no-op when unchanged;
  * a name already current (last bar >= ``as_of``) returns **no window** (nothing to pull);
  * a name we hold nothing for returns the **full** window (a fresh backfill), so the same path serves
    onboarding and daily top-up.
"""
from __future__ import annotations

from datetime import date, timedelta


def incremental_window(
    last_bar: date | None, as_of: date, *, full_start: str = "2023-01-01", overlap_days: int = 5
) -> tuple[str, str] | None:
    """The (start, end) ISO window to fetch for one symbol.

    ``None`` if already current (``last_bar >= as_of``). With no ``last_bar``, the full backfill window
    ``(full_start, as_of)``. Otherwise ``(last_bar - overlap_days, as_of)`` — re-fetching a small overlap
    to absorb late/revised bars (idempotent)."""
    if last_bar is not None and last_bar >= as_of:
        return None
    if last_bar is None:
        return (full_start, str(as_of))
    start = last_bar - timedelta(days=max(0, overlap_days))
    return (str(start), str(as_of))


def latest_bar_by_symbol(pit, table: str = "total_returns") -> dict[str, date]:
    """{symbol: max bar_date} from L2 ``table`` (one bulk PIT read)."""
    df = pit.series(table, columns="symbol, bar_date")
    if df.empty:
        return {}
    df = df.copy()
    df["bar_date"] = df["bar_date"].astype("datetime64[ns]")
    out = df.groupby("symbol")["bar_date"].max()
    return {str(s): d.date() for s, d in out.items()}


def classify_freshness(
    latest: dict[str, date], symbols: list[str], as_of: date, *, max_age_days: int = 7
) -> dict:
    """Split ``symbols`` into current / stale / missing by their latest held bar (pure).

    ``stale`` = held but the last bar is older than ``max_age_days`` before ``as_of`` (a market-day budget;
    weekends/holidays mean a few days lag is normal). ``missing`` = we hold no bar at all (needs backfill /
    onboarding). ``current`` = fresh enough."""
    cutoff = as_of - timedelta(days=max_age_days)
    current: list[str] = []
    stale: list[dict] = []
    missing: list[str] = []
    for sym in symbols:
        lb = latest.get(sym)
        if lb is None:
            missing.append(sym)
        elif lb < cutoff:
            stale.append({"symbol": sym, "last_bar": str(lb), "age_days": (as_of - lb).days})
        else:
            current.append(sym)
    return {
        "as_of": str(as_of), "max_age_days": max_age_days,
        "n_symbols": len(symbols),
        "n_current": len(current), "n_stale": len(stale), "n_missing": len(missing),
        "stale": sorted(stale, key=lambda r: r["age_days"], reverse=True),
        "missing": missing,
    }


def freshness_report(con, store, *, as_of: date | None = None, max_age_days: int = 7) -> dict:
    """Scan L2 for the latest bar per universe name and classify staleness (L1 + bulk L2 read; no network)."""
    as_of = as_of or date.today()
    res = con.execute("MATCH (c:Company) WHERE c.ticker IS NOT NULL AND c.is_listed = true RETURN c.ticker")
    symbols = []
    while res.has_next():
        symbols.append(res.get_next()[0])

    from tmkg.pit.access import PITAccess
    c2 = store.connect()
    try:
        latest = latest_bar_by_symbol(PITAccess(as_of, l2=c2))
    finally:
        c2.close()

    report = classify_freshness(latest, symbols, as_of, max_age_days=max_age_days)
    report["tool"] = "incremental_freshness"
    return report


# --- daily update runner ----------------------------------------------------------------------


def plan_daily_pulls(
    stale_entries: list[dict], as_of: date, *, overlap_days: int = 5, limit: int | None = None
) -> list[dict]:
    """Pure: turn the freshness report's ``stale`` list into per-symbol incremental pull windows."""
    plan: list[dict] = []
    for e in stale_entries:
        last = date.fromisoformat(e["last_bar"])
        win = incremental_window(last, as_of, overlap_days=overlap_days)
        if win is None:
            continue
        plan.append({"symbol": e["symbol"], "start": win[0], "end": win[1], "age_days": e["age_days"]})
    return plan[:limit] if limit else plan


def run_daily_update(
    con, store, adapter=None, *, as_of: date | None = None, max_age_days: int = 7,
    overlap_days: int = 5, dry_run: bool = True, limit: int | None = None,
) -> dict:
    """Refresh only the **stale** names, each over its incremental window (efficient daily top-up).

    Dry-run by default — computes the freshness report + the per-symbol pull plan, performs no network
    call or mutation. ``dry_run=False`` pulls each stale symbol's incremental window (targeted
    ``ingest_prices`` + ``build_total_returns``), fail-loud per symbol. Does NOT refit factors (that is a
    separate regime-aware step). Returns a report."""
    as_of = as_of or date.today()
    fresh = freshness_report(con, store, as_of=as_of, max_age_days=max_age_days)
    plan = plan_daily_pulls(fresh["stale"], as_of, overlap_days=overlap_days, limit=limit)

    executed: list[dict] = []
    if not dry_run and plan:
        from tmkg.ingest.pipeline import build_total_returns, ingest_prices
        for item in plan:
            sym = item["symbol"]
            try:
                p = ingest_prices(adapter, store, sym, start=item["start"], end=item["end"])
                if p.get("n_bars"):
                    build_total_returns(store, sym, as_of=as_of)
                executed.append({"symbol": sym, "ok": True, "n_bars": p.get("n_bars", 0)})
            except Exception as e:  # fail-loud per symbol; continue the rest
                executed.append({"symbol": sym, "ok": False, "error": str(e)[:160]})

    return {
        "tool": "daily_update",
        "mode": "execute" if not dry_run else "dry-run",
        "as_of": str(as_of),
        "freshness": {k: fresh[k] for k in ("n_current", "n_stale", "n_missing", "n_symbols")},
        "n_to_pull": len(plan),
        "plan": plan[:50],
        "executed": executed,
        "note": ("refreshes only stale names over their incremental window; current names skipped; "
                 "factor refit is a separate regime-aware step. dry-run does no network/mutation."),
    }

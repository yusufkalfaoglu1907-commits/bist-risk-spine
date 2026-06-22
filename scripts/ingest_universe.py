"""Ingest the full BIST equity universe into L2 (M3 residual-survival gate inputs).

The M3 [STOP] gate needs per-name USD total returns + neutralized residuals across the WIDE
universe (the 30-name BIST-30 sample is too small — its within-sector candidate family is tiny,
so the random-overlap floor is inflated and the stability `lift` cannot discriminate). This
driver lands the inputs for the ~600 equity-traded names the v1 graph knows (ticker+ISIN+sector):

  1. daily OHLCV ``prices`` per name (Matriks REST, chunked under the ``historicalData`` 1000-bar
     cap so no bars are silently dropped — §4);
  2. USD-primary ``total_returns`` per name as of ``as_of`` (FX = USDTRY, already in L2);
  3. each name's ``universe_class`` from the v1 graph sector → ``universe_membership``.

**Batch-resilient, never fabricating (§4/§6).** Unlike the BIST-30 driver, one unreachable or
data-less name is *logged and skipped* (a recorded skip is not fabrication) so the batch
completes; but a *burst* of consecutive failures means the gateway is down → abort (a §8 hard
stop, not a silent half-run). Writes ``data/cache/universe_ingestion_report.json``.

    PYTHONPATH=src python scripts/ingest_universe.py [START] [END] [AS_OF] [LIMIT]
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import date, timedelta

import tmkg.config  # noqa: F401  -- triggers load_dotenv() before adapters read env
from tmkg import config
from tmkg.graph.connection import connect as graph_connect
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import build_total_returns, ingest_prices
from tmkg.ingest.universe import graph_sector_resolver, ingest_universe_class
from tmkg.l2.store import L2Store

_MATRIKS_MAX_DAYS = 900
_ABORT_AFTER_CONSECUTIVE_FAILS = 15  # a burst ⇒ gateway down (§8), not just sparse names

DEFAULT_START = "2023-01-02"
DEFAULT_END = date.today().isoformat()
DEFAULT_AS_OF = date.today().isoformat()


def _universe_symbols(limit: int | None = None) -> list[str]:
    """The equity-traded universe from the v1 identity graph: names carrying ticker+ISIN+sector."""
    con = graph_connect()
    r = con.execute(
        "MATCH (c:Company)-[:IN_SECTOR]->(s:Sector) "
        "WHERE c.ticker IS NOT NULL AND c.isin IS NOT NULL "
        "RETURN DISTINCT c.ticker ORDER BY c.ticker"
    )
    syms = []
    while r.has_next():
        syms.append(r.get_next()[0])
    return syms[:limit] if limit else syms


def _chunks(start: str, end: str, max_days: int) -> list[tuple[str, str]]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out, cur = [], s
    while cur <= e:
        stop = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + timedelta(days=1)
    return out


def main(start: str, end: str, as_of: date, limit: int | None) -> int:
    symbols = _universe_symbols(limit)
    matriks = MatriksAdapter()
    store = L2Store()
    store.bootstrap_schema()
    chunks = _chunks(start, end, _MATRIKS_MAX_DAYS)
    print(f"universe: {len(symbols)} names · window {start}..{end} · {len(chunks)} chunk(s)/name")

    prices, price_skips = [], []
    landed_syms: list[str] = []
    consecutive_fails = 0
    for i, sym in enumerate(symbols):
        try:
            n = 0
            for cs, ce in chunks:
                r = ingest_prices(matriks, store, sym, start=cs, end=ce)
                n += r.get("n_bars", 0)
                time.sleep(1.5)  # stay under the Matriks gateway rate limit
            if n > 0:
                prices.append({"symbol": sym, "n_bars": n})
                landed_syms.append(sym)
                consecutive_fails = 0
                print(f"  [{i+1}/{len(symbols)}] prices {sym:7} {n} bars")
            else:
                price_skips.append({"symbol": sym, "reason": "no_data", "n_bars": 0})
                print(f"  [{i+1}/{len(symbols)}] SKIP   {sym:7} no data")
        except Exception as exc:  # noqa: BLE001 — log-and-continue; never fabricate a bar
            consecutive_fails += 1
            price_skips.append({"symbol": sym, "reason": "error", "error": repr(exc)})
            print(f"  [{i+1}/{len(symbols)}] FAIL   {sym:7} {exc!r}  (streak={consecutive_fails})")
            if consecutive_fails >= _ABORT_AFTER_CONSECUTIVE_FAILS:
                print(f"\nABORT: {consecutive_fails} consecutive failures — gateway likely down (§8).")
                _write_report(start, end, as_of, symbols, prices, price_skips, [], None,
                              aborted=True)
                return 2
            time.sleep(2.0)

    print(f"\nprices landed for {len(landed_syms)} names; {len(price_skips)} skipped/failed.")

    total_returns, tr_skips = [], []
    for sym in landed_syms:
        try:
            tr = build_total_returns(store, sym, as_of=as_of, fx_factor="USDTRY", cpi_factor=None)
            total_returns.append(tr)
        except Exception as exc:  # noqa: BLE001
            tr_skips.append({"symbol": sym, "error": repr(exc)})
    print(f"total_returns built for {len(total_returns)} names; {len(tr_skips)} failed.")

    con = graph_connect()
    uclass = ingest_universe_class(
        store, landed_syms, universe="bist_all",
        sector_of=graph_sector_resolver(con),
        valid_from=date.fromisoformat(start), knowledge_date=date.fromisoformat(start),
    )
    print(f"universe_class: landed {uclass['n_landed']} / refused {uclass['n_refused']} -> {uclass['by_class']}")

    out = _write_report(start, end, as_of, symbols, prices, price_skips,
                        total_returns, uclass, tr_skips=tr_skips)
    print(f"\nDONE. {len(landed_syms)} names in L2. Report -> {out}")
    return 0


def _write_report(start, end, as_of, symbols, prices, price_skips, total_returns, uclass,
                  *, aborted=False, tr_skips=None):
    report = {
        "run": "universe_ingestion", "aborted": aborted,
        "window": {"start": start, "end": end}, "as_of": str(as_of),
        "n_symbols_requested": len(symbols),
        "n_prices_landed": len(prices), "n_skipped": len(price_skips),
        "prices": prices, "price_skips": price_skips,
        "total_returns": total_returns, "tr_skips": tr_skips or [],
        "universe_class": uclass,
    }
    out = config.REPO_ROOT / "data" / "cache" / "universe_ingestion_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END
    as_of = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.fromisoformat(DEFAULT_AS_OF)
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
    raise SystemExit(main(start, end, as_of, limit))

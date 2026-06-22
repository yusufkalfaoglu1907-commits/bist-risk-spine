"""Ingest the BIST-30 price sample into L2 (M2 exit-gate inputs).

The factor series are already in L2 (``scripts/ingest_factors.py``); the only missing
gate input is per-name USD total returns. This driver:

  1. lands daily OHLCV ``prices`` for the 30 BIST-30 names (Matriks REST, chunked under
     the ``historicalData`` 1000-bar cap so no bars are silently dropped — §4);
  2. builds USD-primary ``total_returns`` for each name as of ``as_of`` (FX = USDTRY,
     already in L2 ``factors``); read back through PIT, same gate signal code obeys;
  3. derives each name's ``universe_class`` from the v1 graph sector and lands
     ``universe_membership`` (so the gate's variance-share is measured PER class, §129).

    PYTHONPATH=src python scripts/ingest_bist30.py [START] [END] [AS_OF]

The Matriks adapter is the only network hop (§4): an unreachable series raises and
aborts, never a fabricated bar. Writes ``data/cache/bist30_ingestion_report.json``.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta

import tmkg.config  # noqa: F401  -- triggers load_dotenv() before adapters read env
from tmkg import config
from tmkg.graph.connection import connect as graph_connect
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import build_total_returns, ingest_prices
from tmkg.ingest.universe import graph_sector_resolver, ingest_universe_class
from tmkg.l2.store import L2Store

# historicalData caps the bar `limit` at 1000 (the pipeline derives limit from calendar
# days); chunk well under that so the full multi-year window lands without truncation.
_MATRIKS_MAX_DAYS = 900
_BIST30_GOLDEN = config.REPO_ROOT / "tests" / "golden" / "matriks" / "universe_bist30.json"

DEFAULT_START = "2023-01-02"  # lead-in for 60d rolling betas + the 2023 orthodox-turn regime
DEFAULT_END = date.today().isoformat()
DEFAULT_AS_OF = date.today().isoformat()


def _bist30_symbols() -> list[str]:
    doc = json.loads(_BIST30_GOLDEN.read_text())
    return list(doc["data"]["symbols"])


def _chunks(start: str, end: str, max_days: int) -> list[tuple[str, str]]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out, cur = [], s
    while cur <= e:
        stop = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + timedelta(days=1)
    return out


def main(start: str, end: str, as_of: date) -> int:
    symbols = _bist30_symbols()
    matriks = MatriksAdapter()
    store = L2Store()
    store.bootstrap_schema()

    prices, total_returns = [], []
    for sym in symbols:
        n = 0
        for cs, ce in _chunks(start, end, _MATRIKS_MAX_DAYS):
            r = ingest_prices(matriks, store, sym, start=cs, end=ce)
            n += r.get("n_bars", 0)
            time.sleep(1.5)  # stay under the Matriks gateway rate limit
        prices.append({"symbol": sym, "n_bars": n})
        print(f"  prices  {sym:6} {n} bars")

    for sym in symbols:
        # USD-primary; CPI deflator is not in this L2 (USD is the primary base), so the
        # real-TRY cross-check column stays NULL rather than reading an absent factor.
        tr = build_total_returns(store, sym, as_of=as_of, fx_factor="USDTRY", cpi_factor=None)
        total_returns.append(tr)
        print(f"  returns {sym:6} {tr.get('n_returns', 0)} ret  (ret_usd_null={tr.get('ret_usd_null')})")

    con = graph_connect()
    uclass = ingest_universe_class(
        store, symbols, universe="bist_30",
        sector_of=graph_sector_resolver(con),
        valid_from=date.fromisoformat(start), knowledge_date=date.fromisoformat(start),
    )
    print(f"  class   landed {uclass['n_landed']} / refused {uclass['n_refused']} -> {uclass['by_class']}")
    if uclass["refused"]:
        print(f"  REFUSED (no class guessed): {uclass['refused']}")

    report = {
        "run": "bist30_ingestion",
        "window": {"start": start, "end": end}, "as_of": str(as_of),
        "n_symbols": len(symbols),
        "prices": prices, "total_returns": total_returns,
        "universe_class": uclass,
    }
    out = config.REPO_ROOT / "data" / "cache" / "bist30_ingestion_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nLanded prices+total_returns for {len(symbols)} names. Report -> {out}")
    return 0


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END
    as_of = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.fromisoformat(DEFAULT_AS_OF)
    raise SystemExit(main(start, end, as_of))

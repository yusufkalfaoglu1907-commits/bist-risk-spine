"""Ingest the core factor-set series into L2 (M2 live ingestion).

Registry-driven: iterates ``factors.registry.CORE_FACTORS`` and lands each *available*
series in L2 ``factors`` via the verified adapters — Matriks REST for FX/index/commodity
series, FRED for the macro legs (VIX, natural gas). This is the network step (§4): the
adapters are the ONLY network hop; a missing/unreachable series raises and aborts (never a
fabricated point). Blocked factors (FFLOW custody-series, MSCIEM no-source) and not-yet-
wired sources (scrape: rates/CDS; derived: holding) are reported as skipped, not guessed.

    PYTHONPATH=src python scripts/ingest_factors.py [START] [END]

Writes a JSON audit report to data/cache/factor_ingestion_report.json (§4).
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import tmkg.config  # noqa: F401  -- triggers load_dotenv() before the adapters read env
from tmkg.factors import registry
from tmkg.ingest.fred import FredAdapter
from tmkg.ingest.matriks import MatriksAdapter
from tmkg.ingest.pipeline import ingest_factor_series, ingest_fred_series
from tmkg.l2.store import L2Store
from tmkg import config

# Sources this driver can ingest today. 'scrape' (rates/CDS, W3) and 'derived'
# (holding-group) are not yet wired; they are reported skipped, never faked.
_INGESTABLE = {"matriks", "fred"}

# Matriks historicalData caps the bar `limit` at 1000 (the pipeline derives limit from
# calendar days). Chunk Matriks windows well under that so no bars are silently dropped.
_MATRIKS_MAX_DAYS = 900

DEFAULT_START = "2023-01-02"  # lead-in for rolling betas + the 2023 orthodox-turn regime
DEFAULT_END = date.today().isoformat()


def _chunks(start: str, end: str, max_days: int) -> list[tuple[str, str]]:
    """Non-overlapping [start, end] sub-windows each spanning <= max_days calendar days."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        stop = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + timedelta(days=1)
    return out


def main(start: str, end: str) -> int:
    matriks = MatriksAdapter()
    fred = FredAdapter()
    store = L2Store()
    store.bootstrap_schema()  # create L2 tables if absent (idempotent)

    ingested: list[dict] = []
    skipped: list[dict] = []

    for f in registry.CORE_FACTORS:
        if f.status == registry.BLOCKED:
            skipped.append({"factor": f.name, "reason": f"blocked: {f.note}"})
            continue
        if f.source not in _INGESTABLE:
            skipped.append({"factor": f.name, "reason": f"source '{f.source}' not wired yet"})
            continue
        if f.source == "matriks":
            n = 0
            first = last = None
            for cs, ce in _chunks(start, end, _MATRIKS_MAX_DAYS):
                r = ingest_factor_series(
                    matriks, store, factor=f.name, symbol=f.series_id, start=cs, end=ce)
                n += r.get("n_points", 0)
                first = first or r.get("first")
                last = r.get("last") or last
            res = {"factor": f.name, "table": "factors", "n_points": n,
                   "first": first, "last": last}
        else:  # fred
            res = ingest_fred_series(
                fred, store, series=f.series_id, factor=f.name, start=start, end=end)
        ingested.append(res)
        print(f"  OK  {f.name:8} <- {f.source}:{f.series_id:10} "
              f"{res.get('n_points', 0)} pts "
              f"[{res.get('first','?')} .. {res.get('last','?')}]")

    for s in skipped:
        print(f"  --  {s['factor']:8} skipped ({s['reason'][:70]})")

    report = {
        "run": "factor_ingestion",
        "window": {"start": start, "end": end},
        "ingested": ingested,
        "skipped": skipped,
        "n_ingested": len(ingested),
        "n_skipped": len(skipped),
    }
    out = config.REPO_ROOT / "data" / "cache" / "factor_ingestion_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nIngested {len(ingested)} factor series, skipped {len(skipped)}. Report -> {out}")
    return 0


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END
    raise SystemExit(main(start, end))

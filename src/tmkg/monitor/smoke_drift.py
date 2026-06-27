"""Data-source drift monitor (M8.3) — aggregate the per-adapter smoke reports into one health surface.

Each ingestion adapter has a ``smoke_check()`` drift guard that, on a live run, writes
``data/cache/<source>_smoke_report.json`` with a ``drift`` list (empty = the upstream contract still
matches the golden samples). Those checks hit the **network**, so they belong to the ingestion layer
(``make smoke`` / the live drift tests) — *this* monitor never calls them. It reads the **recorded
outcomes** from the local cache (§4: a monitor reads L1/L2, the network stays in the adapters) and
reports, per source: was a smoke recorded, did it record drift, and how stale is it.

This turns N independently-firing smoke guards into a single observable surface, and the paired
invariant fails loudly if any recorded smoke shows drift (or an expected source's report is missing) —
so an upstream contract change that a live run already caught cannot sit unnoticed in the cache.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import tmkg.config as config

# The IngestionAdapter sources whose smoke_check writes a `<source>_smoke_report.json`.
DEFAULT_SMOKE_SOURCES: tuple[str, ...] = ("matriks", "evds", "fred", "worldgovbonds", "gdelt")


def _cache_dir() -> Path:
    return config.REPO_ROOT / "data" / "cache"


def _age_days(written_at: str | None, now: datetime) -> float | None:
    if not written_at:
        return None
    try:
        return (now - datetime.fromisoformat(written_at)).total_seconds() / 86400.0
    except ValueError:
        return None


def smoke_drift_status(
    report_dir: str | Path | None = None,
    *,
    sources: tuple[str, ...] = DEFAULT_SMOKE_SOURCES,
    max_age_days: float = 30.0,
    now: datetime | None = None,
) -> dict:
    """Aggregate ``<source>_smoke_report.json`` outcomes across ``sources``.

    Per source the status is one of: ``drift`` (the report's ``drift`` list is non-empty — an
    upstream contract change a live run already caught), ``missing`` (no report on disk), ``stale``
    (a clean report older than ``max_age_days``), or ``ok``. ``passes`` is True iff no source shows
    **drift** and none is **missing** (staleness is a surfaced warning, not a hard fail — smoke
    cadence is environmental). Pure local read; no network."""
    base = Path(report_dir) if report_dir is not None else _cache_dir()
    now = now or datetime.now()
    rows: list[dict] = []
    for src in sources:
        p = base / f"{src}_smoke_report.json"
        if not p.exists():
            rows.append({"source": src, "status": "missing", "report": p.name})
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            rows.append({"source": src, "status": "unreadable", "report": p.name, "detail": str(e)})
            continue
        drift = data.get("drift") or []
        written = data.get("_written_at")
        age = _age_days(written, now)
        if drift:
            status = "drift"
        elif age is not None and age > max_age_days:
            status = "stale"
        else:
            status = "ok"
        rows.append({
            "source": src, "status": status, "report": p.name,
            "written_at": written,
            "age_days": round(age, 2) if age is not None else None,
            "drift": drift,
            "value_matched": data.get("value_matched"),
        })

    drifted = [r["source"] for r in rows if r["status"] == "drift"]
    missing = [r["source"] for r in rows if r["status"] == "missing"]
    stale = [r["source"] for r in rows if r["status"] == "stale"]
    unreadable = [r["source"] for r in rows if r["status"] == "unreadable"]

    failures: list[str] = []
    if drifted:
        failures.append(f"recorded drift on: {drifted}")
    if missing:
        failures.append(f"no smoke report for: {missing}")
    if unreadable:
        failures.append(f"unreadable smoke report for: {unreadable}")

    return {
        "monitor": "smoke_drift",
        "max_age_days": max_age_days,
        "sources": rows,
        "counts": {"ok": sum(1 for r in rows if r["status"] == "ok"),
                   "drift": len(drifted), "missing": len(missing),
                   "stale": len(stale), "unreadable": len(unreadable)},
        "stale_sources": stale,
        "passes": not failures,
        "failures": failures,
    }


def write_smoke_drift_report(**kwargs) -> tuple[dict, Path]:
    """Run the monitor and write ``data/cache/smoke_drift_report.json`` (§4). Returns (report, path)."""
    from tmkg.ingest.audit import write_run_report
    report = smoke_drift_status(**kwargs)
    path = write_run_report("smoke_drift", report)
    return report, path

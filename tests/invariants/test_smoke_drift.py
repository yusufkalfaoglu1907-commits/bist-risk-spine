"""Data-source drift invariant (M8.3) — no recorded upstream-contract drift sits unnoticed.

The adapters' live ``smoke_check()`` guards run on ``make smoke`` and write
``data/cache/<source>_smoke_report.json`` with a ``drift`` list. This invariant reads those recorded
outcomes (no network) and fails if any shows drift or is unreadable, and if a core network source's
report is missing entirely. Staleness is a surfaced warning, not asserted here (smoke cadence is
environmental).
"""
from __future__ import annotations

import pytest

from tmkg.monitor.smoke_drift import smoke_drift_status

# Core sources whose smoke report is tracked in-repo (gdelt's pilot report is optional).
_CORE_SOURCES = ("matriks", "evds", "fred", "worldgovbonds")


@pytest.mark.invariant
def test_no_recorded_smoke_drift():
    status = smoke_drift_status()
    assert status["counts"]["drift"] == 0, (
        f"a recorded smoke check shows upstream-contract drift: "
        f"{[r for r in status['sources'] if r['status'] == 'drift']}"
    )
    assert status["counts"]["unreadable"] == 0, "an unreadable smoke report is in data/cache"


@pytest.mark.invariant
def test_core_smoke_reports_present():
    status = smoke_drift_status(sources=_CORE_SOURCES)
    missing = [r["source"] for r in status["sources"] if r["status"] == "missing"]
    if len(missing) == len(_CORE_SOURCES):
        pytest.skip("no smoke reports on disk (fresh checkout before any `make smoke`)")
    assert not missing, f"core source smoke report(s) missing from data/cache: {missing}"

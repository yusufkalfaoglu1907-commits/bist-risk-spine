"""JSON run-report writer — extends the v1 data/cache/*_report.json pattern.

Every ingestion run writes one of these (matched/skipped/refused counts, source,
as_of). Invariant tests read them (VERIFICATION.md). Confidence-tiered writes:
only high-confidence results are written; ambiguous cases are logged here, never guessed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import tmkg.config as config


def write_run_report(name: str, payload: dict) -> Path:
    """Write data/cache/<name>_report.json with a write-timestamp stamp."""
    out = config.REPO_ROOT / "data" / "cache" / f"{name}_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    stamped = {"_written_at": datetime.now().isoformat(timespec="seconds"), **payload}
    out.write_text(json.dumps(stamped, indent=2, default=str))
    return out

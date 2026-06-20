"""Survivorship ingestion — land a real delisted name into L2 ``universe_membership``.

The W2 wall (CLAUDE.md §5 / data-sourcing W2): delisted/merged/renamed names stay in
the store with their dead histories, and ``MEMBER_OF`` is time-varying. M0 proved the
mechanism on a synthetic name; this lands a REAL delisting with SOURCED dates from a
committed provenance golden — every date traceable to a Borsa İstanbul announcement.

A delisting is modelled BITEMPORALLY as two rows over the same membership span
(same ``valid_from``):
  1. the open membership as known when listed/announced (``valid_to`` NULL);
  2. the delisting correction as known on the Borsa announcement day (``valid_to`` set,
     a later ``knowledge_date``).
``PITAccess.universe`` resolves the latest correction known by ``as_of`` per span, so a
read dated before the delisting was announced honestly shows the name as still-open —
the market did not yet know it would delist (no look-ahead, §5).

Like every adapter this fails loud rather than guessing: a malformed golden raises,
never lands a half-built row. Dates come from the golden's ``_provenance``, never here.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from tmkg.ingest.audit import write_run_report
from tmkg.l2.store import L2Store

_MEMBERSHIP_COLS = (
    "symbol", "universe", "universe_class",
    "valid_from", "valid_to", "knowledge_date", "source",
)


def _to_date(s: str | None) -> date | None:
    """ISO ``YYYY-MM-DD`` -> ``date``; ``None``/blank -> ``None`` (an open interval)."""
    if s in (None, ""):
        return None
    return date.fromisoformat(s)


def ingest_delisting(store: L2Store, golden_path: str | Path) -> dict:
    """Land the ``universe_membership`` rows from a sourced delisting golden into L2.

    The golden carries ``universe_membership`` rows (schema-shaped) plus a
    ``_provenance`` block citing the source of every date. Returns an audit summary
    (counts + the sourced key dates), and writes the §4 run report. Raises if the
    golden lacks membership rows — never fabricates a row.
    """
    doc = json.loads(Path(golden_path).read_text())
    raw = doc.get("universe_membership")
    if not raw:
        raise ValueError(
            f"{golden_path}: no 'universe_membership' rows — refusing to fabricate a delisting"
        )
    rows = [
        {
            "symbol": r["symbol"],
            "universe": r["universe"],
            "universe_class": r.get("universe_class"),
            "valid_from": _to_date(r["valid_from"]),
            "valid_to": _to_date(r.get("valid_to")),
            "knowledge_date": _to_date(r["knowledge_date"]),
            "source": r["source"],
        }
        for r in raw
    ]
    df = pd.DataFrame(rows, columns=list(_MEMBERSHIP_COLS))
    store.write_parquet("universe_membership", df)

    prov = doc.get("_provenance", {})
    report = {
        "table": "universe_membership",
        "entity": prov.get("entity"),
        "ticker": prov.get("ticker"),
        "n_rows": len(df),
        "symbols": sorted(df["symbol"].unique().tolist()),
        "key_dates": prov.get("key_dates"),
    }
    write_run_report("survivorship_ingestion", report)
    return report

"""Attach issued-nominal amounts to existing debt Securities, keyed on ISIN.

Reads the committed ``data/reference/kap_nominal.json`` (harvested from KAP
issuance disclosures by ``scripts/extract_kap_nominals.py``) and MERGEs the
amount onto the Security node whose ``isin`` matches. Match-only: a nominal for
an ISIN not present in the graph is logged, never used to invent a Security
(consistent with the debt/GLEIF loaders). Idempotent — re-running overwrites
the same Security fields with the same provenance.

Writes: Security.nominal, .nominal_currency, .nominal_source, .nominal_as_of,
.nominal_confidence.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import kuzu

from tmkg.adapters.kap_nominal_adapter import KapNominalReference

LOADER_VERSION = 1


def _set_nominal(conn: kuzu.Connection, rec) -> bool:
    """Set nominal fields on the Security with this ISIN. Returns True if a
    Security existed and was updated."""
    res = conn.execute(
        "MATCH (s:Security) WHERE s.isin = $isin RETURN count(s)",
        {"isin": rec.isin},
    )
    if (res.get_next()[0] or 0) == 0:
        return False
    # as_of is optional; set it only when present (avoids a null date() call).
    if rec.as_of:
        conn.execute(
            """MATCH (s:Security) WHERE s.isin = $isin
               SET s.nominal=$n, s.nominal_currency=$ccy, s.nominal_source=$src,
                   s.nominal_confidence=$conf, s.nominal_as_of=date($asof)""",
            {"isin": rec.isin, "n": float(rec.nominal), "ccy": rec.currency,
             "src": rec.source, "conf": float(rec.confidence), "asof": rec.as_of},
        )
    else:
        conn.execute(
            """MATCH (s:Security) WHERE s.isin = $isin
               SET s.nominal=$n, s.nominal_currency=$ccy, s.nominal_source=$src,
                   s.nominal_confidence=$conf""",
            {"isin": rec.isin, "n": float(rec.nominal), "ccy": rec.currency,
             "src": rec.source, "conf": float(rec.confidence)},
        )
    return True


def backfill_nominals(
    conn: kuzu.Connection,
    reference: KapNominalReference | None = None,
    reference_path: Path | str | None = None,
    report_path: Path | str | None = None,
) -> dict:
    """Apply every reference nominal to its matching Security by ISIN.

    Returns a summary dict; optionally writes a full audit report listing which
    ISINs matched, which were absent from the graph, and which were rejected as
    malformed on load.
    """
    ref = reference or KapNominalReference(reference_path)
    records = ref.all()
    rejected = ref.rejected()

    matched, absent = [], []
    for rec in records:
        if _set_nominal(conn, rec):
            matched.append({"isin": rec.isin, "nominal": rec.nominal,
                            "currency": rec.currency, "confidence": rec.confidence})
        else:
            absent.append({"isin": rec.isin, "nominal": rec.nominal})

    summary = {
        "loader_version": LOADER_VERSION,
        "reference_records": len(records),
        "matched": len(matched),
        "absent_from_graph": len(absent),
        "rejected_on_load": len(rejected),
    }
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({
            "generated_iso": date.today().isoformat(),
            "summary": summary,
            "matched": matched,
            "absent_from_graph": absent,
            "rejected_on_load": rejected,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

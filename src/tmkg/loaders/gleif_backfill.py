"""GLEIF Level-1 back-fill — attaches the canonical join key to Company nodes.

Reads Company nodes already seeded from KAP, matches each to a GLEIF Level-1
record by name (see `gleif_adapter` for the matching caveats), and writes
`lei`, `legal_form`, `jurisdiction`, `registration_authority` for matches at or
above a confidence threshold.

Provenance stance (consistent with the rest of the project): name matching is
FUZZY and INFERRED, not filings-grade. So:
  - only LEIs at/above `threshold` are written to the graph, and
  - EVERY attempt (matched, below-threshold, or no-candidate) is written to an
    audit report on disk so a human can review/override the weak ones.

The graph schema has no per-field provenance columns on Company, by design — the
report IS the provenance record for this back-fill. Re-running is safe and
idempotent (MERGE on uuid, adapter results cached).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.gleif_adapter import GleifAdapter


def _companies_with_lei_needing_isin(conn: kuzu.Connection,
                                     only_missing: bool) -> list[dict]:
    where = ["c.lei IS NOT NULL", "c.lei <> ''"]
    if only_missing:
        where.append("(c.isin IS NULL OR c.isin = '')")
    res = conn.execute(
        "MATCH (c:Company) WHERE " + " AND ".join(where) +
        " RETURN c.uuid, c.name, c.ticker, c.lei ORDER BY c.ticker"
    )
    rows = []
    while res.has_next():
        uuid, name, ticker, lei = res.get_next()
        rows.append({"uuid": uuid, "name": name, "ticker": ticker, "lei": lei})
    return rows


def _companies_needing_lei(conn: kuzu.Connection, only_missing: bool,
                           listed_only: bool) -> list[dict]:
    where = []
    if only_missing:
        where.append("(c.lei IS NULL OR c.lei = '')")
    if listed_only:
        where.append("c.is_listed = true")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    res = conn.execute(
        f"MATCH (c:Company) {clause} "
        "RETURN c.uuid, c.name, c.ticker ORDER BY c.ticker"
    )
    rows = []
    while res.has_next():
        uuid, name, ticker = res.get_next()
        rows.append({"uuid": uuid, "name": name, "ticker": ticker})
    return rows


def backfill_leis(
    conn: kuzu.Connection,
    adapter: GleifAdapter,
    threshold: float = 0.6,
    limit: int | None = None,
    only_missing: bool = True,
    listed_only: bool = True,
    report_path: Path | str | None = None,
) -> dict:
    """Match Company nodes to GLEIF and write LEIs for confident matches.

    Returns a summary dict; writes a full audit report to `report_path`
    (default: data/cache/gleif_backfill_report.json).
    """
    targets = _companies_needing_lei(conn, only_missing, listed_only)
    if limit is not None:
        targets = targets[:limit]

    report: list[dict] = []
    written = below = nocand = errors = 0

    for t in targets:
        if not t["name"]:
            continue
        m = adapter.match_company(t["name"], ticker=t["ticker"], threshold=threshold)
        entry = {
            "uuid": t["uuid"], "ticker": t["ticker"], "kap_name": t["name"],
            "query": m.query, "matched": m.matched, "score": m.score,
            "lei": m.lei, "gleif_name": m.gleif_name, "legal_form": m.legal_form,
            "jurisdiction": m.jurisdiction,
            "registration_authority": m.registration_authority,
            "candidates_seen": m.candidates_seen, "note": m.note,
        }
        report.append(entry)

        if m.note.startswith("http_error"):
            errors += 1
            continue
        if m.matched:
            conn.execute(
                """
                MATCH (c:Company {uuid: $uuid})
                SET c.lei=$lei, c.legal_form=$lf,
                    c.jurisdiction=COALESCE($jur, c.jurisdiction),
                    c.registration_authority=$ra
                """,
                {"uuid": t["uuid"], "lei": m.lei, "lf": m.legal_form,
                 "jur": m.jurisdiction, "ra": m.registration_authority},
            )
            written += 1
        elif m.note == "no_candidates":
            nocand += 1
        else:
            below += 1

    rp = Path(report_path) if report_path else (
        Path(config.RAW_DOCS_PATH).parent / "cache" / "gleif_backfill_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "summary": {"targets": len(targets), "leis_written": written,
                    "below_threshold": below, "no_candidates": nocand,
                    "http_errors": errors},
        "matches": report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"targets": len(targets), "leis_written": written,
            "below_threshold": below, "no_candidates": nocand,
            "http_errors": errors, "report": str(rp)}


def backfill_isins(
    conn: kuzu.Connection,
    adapter: GleifAdapter,
    limit: int | None = None,
    only_missing: bool = True,
    report_path: Path | str | None = None,
) -> dict:
    """Resolve each company's LEI to its canonical BİST equity ISIN and write it
    onto the Company node AND the EQUITY Security it ISSUES.

    Runs only on companies that already carry an LEI (the LEI back-fill must run
    first). ISIN selection is deterministic from GLEIF's instrument list (no
    fuzzy threshold), but is still fully logged for audit. Idempotent.
    """
    targets = _companies_with_lei_needing_isin(conn, only_missing)
    if limit is not None:
        targets = targets[:limit]

    report: list[dict] = []
    written = ambiguous = errors = 0

    for t in targets:
        r = adapter.fetch_primary_isin(t["lei"], ticker=t["ticker"])
        report.append({
            "uuid": t["uuid"], "ticker": t["ticker"], "lei": t["lei"],
            "isin": r.isin, "method": r.method, "confident": r.confident,
            "n_instruments": r.n_instruments, "exhausted": r.exhausted,
            "candidates": r.candidates,
        })
        if r.method.startswith("http_error"):
            errors += 1
            continue
        # Only auto-write confident selections. Ambiguous / non-equity cases are
        # logged with candidates for review rather than guessed — a wrong ISIN
        # would silently corrupt the Phase-2 price join.
        if not (r.confident and r.isin):
            ambiguous += 1
            continue
        # Company.isin (denormalized primary) + the issued EQUITY Security.isin
        conn.execute(
            "MATCH (c:Company {uuid: $uuid}) SET c.isin=$isin",
            {"uuid": t["uuid"], "isin": r.isin},
        )
        conn.execute(
            """
            MATCH (c:Company {uuid: $uuid})-[:ISSUES]->(s:Security)
            WHERE s.type = 'EQUITY' OR s.type IS NULL
            SET s.isin=$isin
            """,
            {"uuid": t["uuid"], "isin": r.isin},
        )
        written += 1

    rp = Path(report_path) if report_path else (
        Path(config.RAW_DOCS_PATH).parent / "cache" / "gleif_isin_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "summary": {"targets": len(targets), "isins_written": written,
                    "needs_review": ambiguous, "http_errors": errors},
        "isins": report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"targets": len(targets), "isins_written": written,
            "needs_review": ambiguous, "http_errors": errors, "report": str(rp)}

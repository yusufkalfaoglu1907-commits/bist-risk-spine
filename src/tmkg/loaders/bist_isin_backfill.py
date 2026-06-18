"""BİST/MKK ISIN back-fill — closes the gap GLEIF refuses to guess.

Reads the authoritative ticker→ISIN reference (`bist_isin_adapter`) and fills
`Company.isin` + the issued EQUITY `Security.isin` for listed companies that the
GLEIF ISIN stage left empty (`ambiguous-multi-equity` / `no-equity-class`).

Provenance stance (same as the GLEIF loaders): nothing is guessed and every
decision is logged to an audit report. Crucially, where GLEIF DID surface
equity-class candidate ISINs for the company's LEI but couldn't pick one, this
loader CROSS-VALIDATES: it only auto-writes the BİST ISIN when it is one of
GLEIF's own candidates ("bist+gleif-agree"). A BİST ISIN that contradicts
GLEIF's candidate set is treated as a conflict and logged for review, never
written — two independent sources disagreeing is exactly when a human should
look. When GLEIF offered no equity candidates at all (only debt, or no LEI),
the authoritative BİST value is written on its own ("bist-authoritative").

Idempotent: MERGE-free writes keyed on uuid; safe to re-run.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.bist_isin_adapter import BistIsinAdapter, is_equity_isin

# Methods we trust enough to auto-write (the rest are logged for review).
_CONFIDENT_METHODS = frozenset(
    {"bist+gleif-agree", "bist-authoritative", "bist-disambiguated"})


def _write_company_isin(conn: kuzu.Connection, uuid: str, isin: str) -> None:
    """Set Company.isin + its EQUITY Security.isin (idempotent)."""
    conn.execute("MATCH (c:Company {uuid:$u}) SET c.isin=$i", {"u": uuid, "i": isin})
    conn.execute(
        "MATCH (c:Company {uuid:$u})-[:ISSUES]->(s:Security) "
        "WHERE s.type = 'EQUITY' OR s.type IS NULL SET s.isin=$i",
        {"u": uuid, "i": isin})


def _companies_needing_isin(conn: kuzu.Connection, only_missing: bool,
                            listed_only: bool) -> list[dict]:
    where = ["c.ticker IS NOT NULL", "c.ticker <> ''"]
    if only_missing:
        where.append("(c.isin IS NULL OR c.isin = '')")
    if listed_only:
        where.append("c.is_listed = true")
    clause = "WHERE " + " AND ".join(where)
    res = conn.execute(
        f"MATCH (c:Company) {clause} "
        "RETURN c.uuid, c.name, c.ticker, c.lei, c.isin ORDER BY c.ticker"
    )
    rows = []
    while res.has_next():
        uuid, name, ticker, lei, isin = res.get_next()
        rows.append({"uuid": uuid, "name": name, "ticker": ticker,
                     "lei": lei, "isin": isin})
    return rows


def _load_gleif_candidates(cache_dir: Path) -> dict[str, list[str]]:
    """LEI -> equity-class candidate ISINs, from the GLEIF ISIN cache, used for
    cross-validation. Absent cache => no cross-check (returns empty)."""
    f = cache_dir / "gleif_isins.json"
    if not f.exists():
        return {}
    try:
        blob = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for lei, rec in (blob.get("isins") or {}).items():
        cands = rec.get("candidates") or []
        # a confidently-picked ISIN is itself the relevant candidate
        if rec.get("isin"):
            cands = list({rec["isin"], *cands})
        out[lei] = [c for c in cands if is_equity_isin(c)]
    return out


def backfill_isins_from_bist(
    conn: kuzu.Connection,
    adapter: BistIsinAdapter,
    only_missing: bool = True,
    listed_only: bool = True,
    limit: int | None = None,
    cross_validate: bool = True,
    cache_dir: Path | str | None = None,
    report_path: Path | str | None = None,
) -> dict:
    """Fill ISINs from the authoritative BİST/MKK reference map.

    Returns a summary dict; writes a full audit report to ``report_path``
    (default ``data/cache/bist_isin_report.json``).
    """
    adapter.load()
    targets = _companies_needing_isin(conn, only_missing, listed_only)
    if limit is not None:
        targets = targets[:limit]

    cache_dir = Path(cache_dir or (Path(config.RAW_DOCS_PATH).parent / "cache"))
    gleif_cands = _load_gleif_candidates(cache_dir) if cross_validate else {}

    report: list[dict] = []
    written = conflicts = not_found = invalid = disambiguated = 0

    for t in targets:
        # F9 / 2.3: a human-confirmed resolution of an ambiguous multi-equity
        # ticker takes precedence — it is the only path that resolves the line
        # GLEIF and the ambiguous map both refused. Cross-validated like any BİST
        # value: written only if it does not contradict GLEIF's candidate set.
        dis = adapter.disambiguated(t["ticker"])
        if dis is not None:
            entry = {"uuid": t["uuid"], "ticker": t["ticker"], "lei": t["lei"],
                     "bist_isin": dis, "found": True, "valid": True,
                     "disambiguated": True}
            cands = gleif_cands.get(t["lei"]) if t["lei"] else None
            if cands and dis not in cands:
                entry["method"] = "disambiguation-conflict-gleif"
                entry["gleif_candidates"] = cands
                conflicts += 1
                report.append(entry)
                continue
            entry["method"] = "bist-disambiguated"
            _write_company_isin(conn, t["uuid"], dis)
            written += 1
            disambiguated += 1
            report.append(entry)
            continue

        r = adapter.lookup(t["ticker"])
        entry = {"uuid": t["uuid"], "ticker": t["ticker"], "lei": t["lei"],
                 "bist_isin": r.isin, "found": r.found, "valid": r.valid}

        if not r.found:
            entry["method"] = "not-in-reference"
            not_found += 1
            report.append(entry)
            continue
        if not (r.valid and r.is_equity):
            entry["method"] = "invalid-or-nonequity"
            invalid += 1
            report.append(entry)
            continue

        # Cross-validate against GLEIF's candidates for this company's LEI.
        cands = gleif_cands.get(t["lei"]) if t["lei"] else None
        if cands:
            if r.isin in cands:
                method = "bist+gleif-agree"
            else:
                method = "bist-conflict-gleif"
                entry.update(method=method, gleif_candidates=cands)
                conflicts += 1
                report.append(entry)
                continue  # disagreement -> log, never write
        else:
            method = "bist-authoritative"  # GLEIF had no equity candidates / no LEI
        entry["method"] = method

        if method in _CONFIDENT_METHODS:
            _write_company_isin(conn, t["uuid"], r.isin)
            written += 1
        report.append(entry)

    rp = Path(report_path) if report_path else (cache_dir / "bist_isin_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "reference_source": adapter.source,
        "reference_tickers": len(adapter),
        "summary": {"targets": len(targets), "isins_written": written,
                    "disambiguated": disambiguated,
                    "disambiguation_skipped": adapter.disambiguation_skipped,
                    "conflicts": conflicts, "not_in_reference": not_found,
                    "invalid_or_nonequity": invalid},
        "results": report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"targets": len(targets), "isins_written": written,
            "disambiguated": disambiguated,
            "conflicts": conflicts, "not_in_reference": not_found,
            "invalid_or_nonequity": invalid, "report": str(rp)}


# --- listing classification ------------------------------------------------

EQUITY_TRADED = "EQUITY_TRADED"
NON_EQUITY_ISSUER = "NON_EQUITY_ISSUER"


def classify_listing_status(
    conn: kuzu.Connection,
    adapter: BistIsinAdapter,
    report_path: Path | str | None = None,
) -> dict:
    """Tag every Company with `listing_status`.

    KAP's `IGS` member list (all 729 carry a stockCode and so were flagged
    `is_listed=true`) mixes BİST-traded equities with debt-only issuers —
    factoring/leasing/financing firms, bank debt arms, and sukuk/lease-cert SPVs
    that disclose on KAP because they ISSUE DEBT, not because their equity
    trades. These are kept (they're real ownership/contagion entities and the
    anchors for the deferred debt stage) but distinguished so analyses can filter:

      - EQUITY_TRADED    : has an equity ISIN, or the ticker is in the BİST/MKK
                           equity reference (resolved or multi-group ambiguous);
      - NON_EQUITY_ISSUER: KAP issuer with no traded equity line.

    Derivation prefers a written equity ISIN on the node, then the reference map.
    Idempotent; safe to re-run after the ISIN back-fill.
    """
    adapter.load()
    res = conn.execute(
        "MATCH (c:Company) RETURN c.uuid, c.ticker, c.isin ORDER BY c.ticker")
    rows = []
    while res.has_next():
        rows.append(res.get_next())

    counts = {EQUITY_TRADED: 0, NON_EQUITY_ISSUER: 0}
    report: list[dict] = []
    for uuid, ticker, isin in rows:
        equity = is_equity_isin(isin) or adapter.is_equity_traded(ticker)
        status = EQUITY_TRADED if equity else NON_EQUITY_ISSUER
        counts[status] += 1
        conn.execute("MATCH (c:Company {uuid:$u}) SET c.listing_status=$s",
                     {"u": uuid, "s": status})
        if status == NON_EQUITY_ISSUER:
            report.append({"uuid": uuid, "ticker": ticker})

    if report_path is not None:
        rp = Path(report_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({
            "generated_iso": datetime.now(timezone.utc).isoformat(),
            "summary": counts, "non_equity_issuers": report,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"total": len(rows), **counts}

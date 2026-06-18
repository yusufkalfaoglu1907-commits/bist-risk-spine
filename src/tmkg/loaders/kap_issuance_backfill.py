"""Create/refresh debt Securities from KAP issuance bulletins.

Unlike the nominal back-fill (which only prices ISINs already present), this
loader can MATERIALISE new instruments straight from KAP — closing the loop so
new issuance enters the graph without an MKK export. It reuses the existing debt
write path (`_security_uuid` = "deb-"+ISIN, `_write_security`), so an instrument
KAP discovers MERGEs cleanly with the same node the MKK layer would have made.

Issuer resolution (the precision gate, in order):
  1. exact match on the extracted exchange **ticker** → Company.ticker;
  2. fallback to the debt brand-token matcher on the issuer name;
  3. unmatched → logged, NOT created (unless create_missing_issuers).

Per instrument it sets: Security (isin/type/maturity/issuer_name/issue_date/
is_amortizing) + ISSUES edge (instrument_class, provenance) + nominal (+currency,
source, confidence). Maturity here is KAP's explicit İtfa Tarihi, so it lands at
high confidence (0.95) rather than the MKK description-inferred value.
Idempotent: re-running MERGEs the same node and overwrites the same fields.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import kuzu

from tmkg.adapters.mkk_debt_adapter import DebtSecurity, DEFAULT_DEBT_CLASSES
from tmkg.analytics.outstanding import is_amortizing_default
from tmkg.loaders.debt_backfill import (
    _load_companies, match_issuer, _security_uuid, _write_security,
)

LOADER_VERSION = 1
_SOURCE = "KAP-issuance"
_KAP_MATURITY_CONFIDENCE = 0.95


def _set_amount_and_flags(conn, isin, rec):
    sid = _security_uuid(isin)
    conn.execute(
        """MATCH (s:Security {uuid:$u})
           SET s.nominal=$n, s.nominal_currency=$ccy, s.nominal_source=$src,
               s.nominal_confidence=$conf, s.is_amortizing=$amort""",
        {"u": sid, "n": float(rec["nominal"]), "ccy": rec.get("currency") or "TRY",
         "src": rec.get("source") or _SOURCE, "conf": 0.9,
         "amort": is_amortizing_default(rec.get("instrument_class"))},
    )
    if rec.get("issue_date"):
        conn.execute("MATCH (s:Security {uuid:$u}) SET s.issue_date=date($d)",
                     {"u": sid, "d": rec["issue_date"]})


_FX_NOMINAL_CONFIDENCE = 0.95  # the figure itself is the official SPK certificate


def backfill_fx_issuances(
    conn: kuzu.Connection,
    records: list[dict],
    report_path: Path | str | None = None,
) -> dict:
    """Price existing XS eurobond Securities from FX issue-certificate records.

    ISIN-exact: the certificate carries the XS ISIN, so we MATCH the existing
    `deb-<ISIN>` Security directly — no issuer resolution. This is **update-only**:
    a eurobond not already in the graph is logged unmatched, NOT created (the
    certificate text gives us no issuer link to attach it to). Per match it SETS
    `nominal` + native `nominal_currency` (USD/EUR) + `nominal_basis`
    ('fx-issue-size-upper-bound') and upgrades `Security.currency` from the 'FX'
    placeholder to the real ISO code. It does NOT touch maturity (the MKK value
    stands). Idempotent: re-running overwrites the same fields.
    """
    priced, unmatched = [], []
    for rec in records:
        isin = (rec.get("isin") or "").upper()
        currency = rec.get("currency")
        nominal = rec.get("nominal")
        if not isin or not currency or not nominal:
            continue
        sid = _security_uuid(isin)
        exists = conn.execute(
            "MATCH (s:Security {uuid:$u}) RETURN count(s)", {"u": sid}
        ).get_next()[0] > 0
        if not exists:
            unmatched.append({"isin": isin, "currency": currency, "nominal": nominal})
            continue
        conn.execute(
            """MATCH (s:Security {uuid:$u})
               SET s.nominal=$n, s.nominal_currency=$ccy, s.currency=$ccy,
                   s.nominal_source=$src, s.nominal_confidence=$conf,
                   s.nominal_basis=$basis""",
            {"u": sid, "n": float(nominal), "ccy": currency,
             "src": rec.get("source") or _SOURCE, "conf": _FX_NOMINAL_CONFIDENCE,
             "basis": rec.get("basis") or "fx-issue-size-upper-bound"},
        )
        if rec.get("issue_date"):
            conn.execute(
                "MATCH (s:Security {uuid:$u}) SET s.nominal_as_of=date($d)",
                {"u": sid, "d": rec["issue_date"]})
        priced.append({"isin": isin, "currency": currency, "nominal": float(nominal)})

    summary = {
        "loader_version": LOADER_VERSION,
        "records_in": len(records),
        "priced": len(priced),
        "unmatched_isin": len(unmatched),
    }
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({
            "generated_iso": date.today().isoformat(),
            "summary": summary, "priced": priced, "unmatched_isin": unmatched,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def backfill_from_issuances(
    conn: kuzu.Connection,
    records: list[dict],
    create_missing_issuers: bool = False,
    report_path: Path | str | None = None,
) -> dict:
    """Apply extracted issuance records to the graph. Returns a summary dict."""
    companies = _load_companies(conn)
    by_ticker = {c["ticker"]: c for c in companies if c.get("ticker")}

    created_or_updated, unmatched, out_of_scope = [], [], []
    new_isins = 0
    for rec in records:
        isin = (rec.get("isin") or "").upper()
        cls = rec.get("instrument_class")
        if cls not in DEFAULT_DEBT_CLASSES:
            out_of_scope.append({"isin": isin, "class": cls})
            continue

        # 1) ticker-exact, 2) name matcher
        company = by_ticker.get((rec.get("ticker") or "").upper())
        method = "ticker"
        if company is None and rec.get("issuer_name"):
            company = match_issuer(rec["issuer_name"], companies)
            method = "name"
        if company is None:
            unmatched.append({"isin": isin, "ticker": rec.get("ticker"),
                              "issuer_name": rec.get("issuer_name")})
            if not create_missing_issuers:
                continue
            method = "created-missing"  # (issuer creation left to a future opt-in)
            continue

        existed = conn.execute(
            "MATCH (s:Security {uuid:$u}) RETURN count(s)",
            {"u": _security_uuid(isin)}).get_next()[0] > 0
        ds = DebtSecurity(
            isin=isin, instrument_class=cls, type=rec.get("type") or "",
            issuer_name=rec.get("issuer_name") or "", description=rec.get("source") or "",
            currency=rec.get("currency") or "TRY",
            maturity_date=rec.get("maturity_date"),
            maturity_confidence=_KAP_MATURITY_CONFIDENCE, maturity_method="kap-explicit",
        )
        _write_security(conn, company["uuid"], ds)
        _set_amount_and_flags(conn, isin, rec)
        if not existed:
            new_isins += 1
        created_or_updated.append({"isin": isin, "ticker": company.get("ticker"),
                                   "match_method": method, "new": not existed,
                                   "nominal": rec["nominal"]})

    summary = {
        "loader_version": LOADER_VERSION,
        "records_in": len(records),
        "written": len(created_or_updated),
        "new_instruments": new_isins,
        "unmatched_issuer": len(unmatched),
        "out_of_scope_class": len(out_of_scope),
    }
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({
            "generated_iso": date.today().isoformat(),
            "summary": summary,
            "written": created_or_updated,
            "unmatched_issuer": unmatched,
            "out_of_scope_class": out_of_scope,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

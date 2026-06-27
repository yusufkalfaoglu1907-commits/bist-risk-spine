"""M7 tier-1 feasibility probe — KAP "Yeni İş İlişkisi" (new-business) disclosures.

The M7 exit gate's FIRST criterion is falsification discipline (BUILD_PLAN M7 / §10):
*measure how many usable tier-1 edges survive before trusting the design.* A
firm-level supply-chain lead-lag PRICE signal needs BOTH legs listed & tradeable
on BIST. This probe pulls the structured KAP new-business feed via the Matriks
REST adapter (the contract-legal ingestion path, §4 — NOT the interactive MCP),
parses the disclosure table, and classifies every counterparty by tradeability.

It writes a coverage report to data/cache; it writes NOTHING to L2 and fabricates
nothing (§4). The go/no-go decision is surfaced to the user (§8), not auto-taken.

Run:  PYTHONPATH=src python scripts/probe_m7_newbusiness.py
"""
from __future__ import annotations

import re
import sys

import kuzu

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.matriks import MatriksAdapter

# Counterparty-type classifiers (applied in priority order). A counterparty is a
# tradeable lead-lag leg ONLY if it is a named, listed, independent BIST company.
_GOVT = re.compile(
    r"cumhurbaşkan|bakanlığ|müdürlüğ|başkanlığ|belediye|üniversite|valilik|"
    r"koordinasyon|il sağlık|hastane|genel müdürlük",
    re.I,
)
_FOREIGN = re.compile(
    r"\b(inc|corp|gmbh|ltd\.?|llc|co\.|limited|company|construction|foodstuff)\b|"
    r"kazakist|kuveyt|çin|china|abd|amerika|almanya|yurtdış",
    re.I,
)
_ANON = re.compile(
    r"yerleşik|mukim|uluslararas|bayiler|müşterimiz|müşteri$|firma$|şirket$|bir ",
    re.I,
)

# Generic corporate tokens that are NOT distinctive enough to assert a listed match.
_GENERIC = {
    "TURKIYE", "ENERJI", "TICARET", "SANAYI", "HOLDING", "YATIRIM", "GAYRIMENKUL",
    "TRADING", "BANKA", "BANKASI", "TEKNOLOJI", "ELEKTRIK", "INSAAT", "GIDA",
    "SAGLIK", "ANONIM", "SIRKETI", "GRUP", "METAL", "CELIK", "URETIM", "MAKINA",
}

_WINDOWS = [
    ("2025-01-01", "2025-03-31"),
    ("2025-09-01", "2025-11-30"),
    (None, None),  # most-recent 100
]


def _norm(s: str) -> str:
    s = (s or "").upper()
    for a, b in [("İ", "I"), ("Ş", "S"), ("Ğ", "G"), ("Ü", "U"), ("Ö", "O"),
                 ("Ç", "C"), ("Â", "A"), ("I", "I")]:
        s = s.replace(a, b)
    return re.sub(r"[^A-Z0-9 ]", "", s)


def _cell(content: str, label: str) -> str | None:
    m = re.search(
        r'<td class="ilk">' + re.escape(label) + r"[^<]*</td><td>(.*?)</td>",
        content,
        re.S,
    )
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _listed_tokens(conn) -> dict[str, tuple[str, str]]:
    res = conn.execute(
        "MATCH (c:Company) WHERE c.is_listed=true RETURN c.ticker, c.name"
    )
    tok: dict[str, tuple[str, str]] = {}
    while res.has_next():
        t, n = res.get_next()
        if not n or len(n) <= 4:
            continue
        for w in _norm(n).split():
            if len(w) > 5 and w not in _GENERIC:
                tok.setdefault(w, (t, n))
    return tok


def _classify(cp: str | None) -> str:
    if not cp or cp.strip() in ("-", ""):
        return "unnamed"
    if _GOVT.search(cp):
        return "government"
    if _FOREIGN.search(cp):
        return "foreign"
    if _ANON.search(cp):
        return "anonymized"
    return "named_domestic"


def main() -> int:
    ad = MatriksAdapter()
    db = kuzu.Database("data/tmkg.kuzu", read_only=True)
    listed_tok = _listed_tokens(kuzu.Connection(db))

    agg = {k: 0 for k in
           ("unnamed", "government", "foreign", "anonymized", "named_domestic")}
    total = 0
    materiality_present = 0
    listed_independent: list[dict] = []
    per_window = []

    for start, end in _WINDOWS:
        params = dict(
            includeNews=True, compactMode=False, newsWithContent=True,
            newsHeadlineSearch="iş ilişkisi", newsSource=["KAP"], newsCount=100,
        )
        if start:
            params.update(newsStartDate=start, newsEndDate=end)
        payload = ad.fetch("news_and_events", **params)
        items = (payload.get("news") or {}).get("items") or []
        nb = [n for n in items if "İş İlişki" in (n.get("headline") or "")]
        wb = {k: 0 for k in agg}
        for n in nb:
            c = n.get("content") or ""
            cp = _cell(c, "Müşterinin/Tedarikçinin Adı Soyadı/Ticaret Ünvanı")
            bucket = _classify(cp)
            wb[bucket] += 1
            agg[bucket] += 1
            total += 1
            ratio = _cell(
                c,
                "Varsa Müşterinin/Tedarikçinin Ortaklığın Kamuya Açıklanan Son "
                "Gelir Tablosundaki Net Satışlar/Satılan Mal Maliyeti İçindeki Payı",
            )
            if ratio and ratio not in ("-", ""):
                materiality_present += 1
            if bucket == "named_domestic":
                cpn = set(_norm(cp).split())
                for w in cpn:
                    if w in listed_tok:
                        decl = n.get("symbol")
                        decl = decl[0] if isinstance(decl, list) else decl
                        listed_independent.append({
                            "declaring": decl,
                            "counterparty": cp[:60],
                            "matched_ticker": listed_tok[w][0],
                            "date": (n.get("date") or "")[:10],
                        })
                        break
        per_window.append({"start": start, "end": end, "n": len(nb), "buckets": wb})

    tradeable_frac = (len(listed_independent) / total) if total else 0.0
    report = {
        "probe": "m7_tier1_newbusiness_feasibility",
        "source": "matriks/newsAndEvents includeNews KAP 'iş ilişkisi'",
        "n_disclosures": total,
        "counterparty_buckets": agg,
        "materiality_ratio_present": materiality_present,
        "materiality_ratio_present_pct": round(100 * materiality_present / total, 1) if total else 0,
        "listed_independent_candidates": listed_independent,
        "listed_independent_count_raw": len(listed_independent),
        "tradeable_leg_fraction": round(tradeable_frac, 4),
        "per_window": per_window,
        "note": (
            "listed_independent_count_raw is an UPPER bound — token matching still "
            "admits false positives (generic tokens) and intra-group counterparties "
            "(the M7 already-priced null). Manual inspection of the 2026 window left "
            "~1 genuine independent listed-BIST counterparty in 100 disclosures. "
            "Materiality ratios are >90% absent; amounts are free-text only. "
            "Conclusion surfaced to user as an M7 tier-1 go/no-go (§8); no L2 write."
        ),
    }
    write_run_report("m7_newbusiness_coverage", report)
    print("M7 tier-1 new-business feasibility probe")
    print(f"  disclosures analysed: {total}")
    print(f"  counterparty buckets: {agg}")
    print(f"  materiality ratio present: {materiality_present}/{total}")
    print(f"  listed-independent (upper bound): {len(listed_independent)}")
    print(f"  tradeable-leg fraction (upper bound): {tradeable_frac:.2%}")
    print("  report -> data/cache/m7_newbusiness_coverage_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

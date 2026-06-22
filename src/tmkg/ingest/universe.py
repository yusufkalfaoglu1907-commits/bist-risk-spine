"""Universe-class ingestion — derive each name's ``universe_class`` and land it in
L2 ``universe_membership``.

The design (``system-design-v2.md`` §129) fits the factor model, residual networks and
event studies **per ``universe_class`` ∈ {operating, gyo_reit, holding,
investment_trust, etf}**, because NAV- and leverage-driven returns (REITs, trusts, ETFs)
obey a different generating process than operating equities and would otherwise distort
the shared covariance. So a name's class is a first-class segmentation, not a label.

The class is **derived from the issuer's sector** — the authoritative segmentation — via a
sector→class **rule table** (the logic), never hardcoded per name. A name whose sector
cannot be resolved is **refused and reported, never assigned a guessed class** (§4 /
confidence-tiered writes). Like ``ingest_delisting`` this lands ``universe_membership``
rows; it is an identity/reference ingestion (the v1 graph is the identity authority, the
same posture as ``IdBridge`` — identity is not a time-varying signal read).
"""
from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd

from tmkg.ingest.audit import write_run_report
from tmkg.l2.store import L2Store

_MEMBERSHIP_COLS = (
    "symbol", "universe", "universe_class",
    "valid_from", "valid_to", "knowledge_date", "source",
)

# Ordered sector→class rules, matched as substrings against the issuer's sector name as
# stored in the v1 graph (canonical uppercase Turkish). The first match wins; anything
# unmatched is an operating equity. This is the derivation *logic* — the per-name result
# falls out of it, so adding a name never means editing a class by hand.
_SECTOR_CLASS_RULES: tuple[tuple[str, str], ...] = (
    ("GAYRİMENKUL YATIRIM ORTAKLIK", "gyo_reit"),          # REITs (GYO)
    ("MENKUL KIYMET YATIRIM ORTAKLIK", "investment_trust"),  # securities investment trusts
    ("GİRİŞİM SERMAYESİ YATIRIM ORTAKLIK", "investment_trust"),  # VC investment trusts
    ("BORSA YATIRIM FONU", "etf"),                         # exchange-traded funds
    ("HOLDİNG", "holding"),                                 # holdings & investment cos.
)
DEFAULT_CLASS = "operating"

# Traded BIST code -> a v1-graph ticker for the SAME issuer. These are share-class code
# variants (A/B/C groups), so they resolve to the same issuer and therefore the same
# sector — this resolves *identity*, never the class (which is still derived from sector).
_TICKER_ALIASES: dict[str, str] = {
    "ISCTR": "ISATR",  # Türkiye İş Bankası (C group traded; A group in graph)
    "VAKBN": "TVB",    # Türkiye Vakıflar Bankası
    "YKBNK": "YKB",    # Yapı ve Kredi Bankası
    "KRDMD": "KRDMA",  # Kardemir (D group traded; A group in graph)
}


def derive_universe_class(sector_name: str | None) -> str | None:
    """Map an issuer's sector name to its ``universe_class``.

    Returns ``None`` when the sector is unknown (so the caller refuses rather than
    guesses). A known sector with no special rule is an ``operating`` equity. Matching is
    on the canonical uppercase Turkish sector strings the v1 graph stores (no case
    transform — Turkish dotted/dotless ``i`` makes ``str.upper`` lossy).
    """
    if not sector_name:
        return None
    for needle, cls in _SECTOR_CLASS_RULES:
        if needle in sector_name:
            return cls
    return DEFAULT_CLASS


def graph_sector_resolver(con) -> Callable[[str], str | None]:
    """A ticker→sector resolver backed by the v1 Kuzu identity graph.

    Tries the traded ticker, then its known share-class alias (same issuer). Returns the
    sector name, or ``None`` if the name is not in the graph (the caller then refuses).
    """
    def resolve(ticker: str) -> str | None:
        for t in (ticker, _TICKER_ALIASES.get(ticker)):
            if t is None:
                continue
            res = con.execute(
                "MATCH (c:Company {ticker: $t})-[:IN_SECTOR]->(s:Sector) "
                "RETURN s.name LIMIT 1",
                {"t": t},
            )
            if res.has_next():
                return res.get_next()[0]
        return None

    return resolve


def ingest_universe_class(
    store: L2Store,
    symbols: list[str],
    *,
    universe: str,
    sector_of: Callable[[str], str | None],
    valid_from: date,
    knowledge_date: date,
    source: str = "v1_graph_sector",
) -> dict:
    """Derive ``universe_class`` for ``symbols`` and land ``universe_membership`` rows.

    ``sector_of`` is the ticker→sector resolver (inject ``graph_sector_resolver(con)`` in
    production; a dict-backed one in tests). Each resolvable name lands one open
    membership row (``valid_to`` NULL) tagged with its derived class; an unresolvable name
    is **refused and reported**, never landed with a guessed class. ``valid_from`` /
    ``knowledge_date`` make the membership visible to a PIT read at the fit's ``as_of``.
    Writes the §4 run report.
    """
    rows: list[dict] = []
    landed: list[dict] = []
    refused: list[dict] = []
    for sym in symbols:
        sector = sector_of(sym)
        cls = derive_universe_class(sector)
        if cls is None:
            refused.append({"symbol": sym, "reason": "sector unresolved"})
            continue
        rows.append({
            "symbol": sym,
            "universe": universe,
            "universe_class": cls,
            "valid_from": valid_from,
            "valid_to": None,
            "knowledge_date": knowledge_date,
            "source": source,
        })
        landed.append({"symbol": sym, "universe_class": cls, "sector": sector})

    if rows:
        df = pd.DataFrame(rows, columns=list(_MEMBERSHIP_COLS))
        store.write_parquet("universe_membership", df)

    report = {
        "table": "universe_membership",
        "universe": universe,
        "n_landed": len(landed),
        "n_refused": len(refused),
        "landed": landed,
        "refused": refused,
        "by_class": (
            pd.DataFrame(landed)["universe_class"].value_counts().to_dict()
            if landed else {}
        ),
    }
    write_run_report("universe_class_ingestion", report)
    return report

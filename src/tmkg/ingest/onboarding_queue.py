"""Onboarding queue + market-data availability (M9.3c) — what still needs onboarding, and why it's stuck.

Two lessons from the first real onboarding (FAIRF, 2026-06-27) drive this module:

  1. **The onboarding driver is "graph names missing a complete quant row," not the 3a "brand-new"
     list.** Once a new name is seeded into the graph (step 1), the detector stops flagging it — yet it
     is still quant-incomplete (no prices/betas). So the retry queue is every *listed* graph name whose
     onboarding stages are not all done.
  2. **A new IPO's market data lags its KAP listing.** KAP registers a company at listing; the market-
     data vendor (Matriks) adds the tradeable symbol's bars feed days/weeks later. FAIRF is in KAP +
     identity-onboarded, but ``symbolSearch`` returns 0 results — Matriks does not carry it yet (the bars
     endpoint reports a generic ``SERVICE_UNAVAILABLE`` for the unknown symbol). That is **vendor-lag, a
     transient *expected* pending state to retry later — not a failure and not a service outage.**

So this module computes the queue (pure ``assemble_queue`` over bulk graph/L2 facts — fast, no per-name
loop of network calls) and classifies a pending-price name's market-data readiness (``classify_market_data``
over a ``symbolSearch`` result — pure; the network call is a thin wrapper). No mutation.
"""
from __future__ import annotations

from datetime import date

_STAGES = ("kap_identity", "gleif_identity", "sector", "universe_prices", "factor_refit")


# --- market-data availability (the vendor-lag classifier) -------------------------------------


def classify_market_data(search_result: dict, symbol: str) -> dict:
    """Classify a Matriks ``symbolSearch`` result for ``symbol``.

    ``carried`` — the vendor lists this exact symbol (bars should work); ``not_carried_yet`` — zero
    results, i.e. vendor-lag (retry later, not a failure); ``carried_other_code`` — the vendor has
    related symbols under a different code (surface them so the id-bridge can be reconciled)."""
    results = search_result.get("results") or []
    codes = [r.get("symbol") for r in results if r.get("symbol")]
    if not codes:
        return {"symbol": symbol, "market_data": "not_carried_yet", "vendor_codes": []}
    if symbol in codes:
        return {"symbol": symbol, "market_data": "carried", "vendor_codes": codes[:8]}
    return {"symbol": symbol, "market_data": "carried_other_code", "vendor_codes": codes[:8]}


def market_data_status(adapter, symbol: str) -> dict:
    """Thin network wrapper: ``symbolSearch`` for ``symbol`` → ``classify_market_data``."""
    res = adapter.fetch("symbolSearch", query=symbol)
    return classify_market_data(res, symbol)


# --- the onboarding queue ---------------------------------------------------------------------


def stages_for(row: dict, has_returns: set[str], has_betas: set[str], has_universe: set[str]) -> dict:
    """Onboarding stage completion for one listed graph name, from bulk facts (pure)."""
    tk = row.get("ticker")
    return {
        "kap_identity": True,  # in the graph by construction
        "gleif_identity": bool(row.get("lei") and row.get("isin")),
        "sector": bool(row.get("has_sector")),
        "universe_prices": bool(tk in has_returns and tk in has_universe),
        "factor_refit": bool(tk in has_betas),
    }


def assemble_queue(
    graph_rows: list[dict], has_returns: set[str], has_betas: set[str], has_universe: set[str]
) -> list[dict]:
    """Pure: every listed graph name whose onboarding is incomplete, with its pending stages."""
    queue: list[dict] = []
    for row in graph_rows:
        stages = stages_for(row, has_returns, has_betas, has_universe)
        if all(stages.values()):
            continue
        queue.append({
            "ticker": row.get("ticker"), "kap_oid": row.get("kap_oid"),
            "pending": [s for s in _STAGES if not stages[s]],
            "next_step": next(s for s in _STAGES if not stages[s]),
        })
    return sorted(queue, key=lambda q: (len(q["pending"]), q["ticker"] or ""), reverse=True)


def _distinct_symbols(pit, table: str) -> set[str]:
    df = pit.series(table, columns="DISTINCT symbol")
    return set(df["symbol"]) if not df.empty else set()


def onboarding_queue(con, store, *, as_of: date | None = None) -> list[dict]:
    """All listed graph names with an incomplete onboarding (reads L1 + bulk L2; no network, no mutation)."""
    as_of = as_of or date.today()
    res = con.execute(
        "MATCH (c:Company) WHERE c.ticker IS NOT NULL AND c.is_listed = true "
        "OPTIONAL MATCH (c)-[:IN_SECTOR]->(s:Sector) "
        "RETURN c.kap_oid, c.ticker, c.lei, c.isin, count(s)")
    graph_rows: list[dict] = []
    while res.has_next():
        oid, tk, lei, isin, nsec = res.get_next()
        graph_rows.append({"kap_oid": oid, "ticker": tk, "lei": lei, "isin": isin,
                           "has_sector": bool(nsec and nsec > 0)})

    from tmkg.pit.access import PITAccess
    c2 = store.connect()
    try:
        pit = PITAccess(as_of, l2=c2)
        has_returns = _distinct_symbols(pit, "total_returns")
        has_betas = _distinct_symbols(pit, "betas")
        has_universe = _distinct_symbols(pit, "universe_membership")
    finally:
        c2.close()

    return assemble_queue(graph_rows, has_returns, has_betas, has_universe)

"""Onboarding queue + market-data classification (M9.3c) — pure logic, deterministic.

Pins the two FAIRF lessons: (1) the queue is every listed graph name with an incomplete onboarding
(not just brand-new ones), surfacing partially-onboarded names; (2) a new IPO's missing market data is
vendor-lag (symbolSearch 0 results), a retry-later pending state distinct from a real failure.
"""
from __future__ import annotations

from tmkg.ingest.onboarding_queue import assemble_queue, classify_market_data, stages_for


# --- market-data vendor-lag classifier --------------------------------------------------------

def test_zero_results_is_vendor_lag():
    c = classify_market_data({"totalResults": 0, "results": []}, "FAIRF")
    assert c["market_data"] == "not_carried_yet"   # retry later, not a failure


def test_exact_symbol_is_carried():
    c = classify_market_data({"results": [{"symbol": "GARAN"}]}, "GARAN")
    assert c["market_data"] == "carried"
    assert "GARAN" in c["vendor_codes"]


def test_related_codes_surface_as_other_code():
    c = classify_market_data({"results": [{"symbol": "GARAN.E"}, {"symbol": "GARANX"}]}, "GARAN")
    assert c["market_data"] == "carried_other_code"
    assert c["vendor_codes"] == ["GARAN.E", "GARANX"]


# --- stage computation + queue ----------------------------------------------------------------

def _row(tk, lei=None, isin=None, has_sector=False, kap_oid="O"):
    return {"ticker": tk, "lei": lei, "isin": isin, "has_sector": has_sector, "kap_oid": kap_oid}


def test_stages_for_fairf_like_name():
    # identity-onboarded but no isin / sector / prices / betas (the real FAIRF state)
    s = stages_for(_row("FAIRF", lei="L"), has_returns=set(), has_betas=set(), has_universe=set())
    assert s == {"kap_identity": True, "gleif_identity": False, "sector": False,
                 "universe_prices": False, "factor_refit": False}


def test_complete_name_not_in_queue():
    rows = [_row("DONE", lei="L", isin="I", has_sector=True)]
    q = assemble_queue(rows, has_returns={"DONE"}, has_betas={"DONE"}, has_universe={"DONE"})
    assert q == []   # everything done -> not queued


def test_incomplete_name_queued_with_pending_and_next():
    rows = [_row("FAIRF", lei="L"), _row("DONE", lei="L", isin="I", has_sector=True)]
    q = assemble_queue(rows, has_returns={"DONE"}, has_betas={"DONE"}, has_universe={"DONE"})
    assert [e["ticker"] for e in q] == ["FAIRF"]
    e = q[0]
    assert e["pending"] == ["gleif_identity", "sector", "universe_prices", "factor_refit"]
    assert e["next_step"] == "gleif_identity"


def test_queue_sorted_by_most_pending_first():
    rows = [
        _row("MOST", kap_oid="1"),                                   # 4 pending
        _row("FEW", lei="L", isin="I", has_sector=True, kap_oid="2"),  # only prices+betas pending
    ]
    q = assemble_queue(rows, has_returns=set(), has_betas=set(), has_universe=set())
    assert [e["ticker"] for e in q] == ["MOST", "FEW"]   # most-incomplete first

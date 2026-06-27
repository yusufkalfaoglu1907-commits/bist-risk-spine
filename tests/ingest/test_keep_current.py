"""Keep-current cycle verdict (M9.1) — the pure composition of sub-tool results, deterministic.

The heartbeat folds the detector + three monitors + the onboarding queue into one ``attention``
verdict. These pin the composition: all-clear is OK; a new listing, a monitor failure, or a non-empty
queue each raise attention; health_ok isolates the monitors from pending onboarding.
"""
from __future__ import annotations

from tmkg.ingest.keep_current import summarize_cycle

_PASS = {"passes": True}
_FAIL = {"passes": False}


def test_all_clear_is_ok():
    v = summarize_cycle(new_listings={"n_new": 0}, idbridge=_PASS, smoke=_PASS, registry=_PASS, queue_len=0)
    assert not v["attention"]
    assert v["health_ok"]
    assert v["reasons"] == []


def test_new_listing_raises_attention():
    v = summarize_cycle(new_listings={"n_new": 1}, idbridge=_PASS, smoke=_PASS, registry=_PASS, queue_len=0)
    assert v["attention"]
    assert v["health_ok"]                       # monitors fine; attention is from the new listing
    assert any("new listing" in r for r in v["reasons"])


def test_monitor_failure_raises_attention_and_drops_health():
    v = summarize_cycle(new_listings={"n_new": 0}, idbridge=_PASS, smoke=_FAIL, registry=_PASS, queue_len=0)
    assert v["attention"]
    assert not v["health_ok"]
    assert any("smoke_drift" in r for r in v["reasons"])


def test_pending_queue_raises_attention_but_not_health():
    v = summarize_cycle(new_listings={"n_new": 0}, idbridge=_PASS, smoke=_PASS, registry=_PASS, queue_len=7)
    assert v["attention"]
    assert v["health_ok"]                       # health is fine; onboarding is a softer follow-up
    assert any("awaiting onboarding" in r for r in v["reasons"])


def test_monitors_map_reflects_each_input():
    v = summarize_cycle(new_listings={"n_new": 0}, idbridge=_FAIL, smoke=_PASS, registry=_FAIL, queue_len=0)
    assert v["monitors"] == {"idbridge": False, "smoke_drift": True, "registry": False}
    assert not v["health_ok"]

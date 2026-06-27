"""Keep-current cycle (M9.1) — the schedulable heartbeat that tells you the substrate's freshness.

Composes the pieces already built into one read-only status pass a scheduler (cron/launchd/CI) runs on
a cadence: detect new listings (3a), check the three M8.3 health monitors (id-bridge / data-drift /
registry), and compute the onboarding queue (3c). It emits a consolidated report and an **attention**
verdict (and a nonzero exit from the script) when anything needs a human or a follow-up job — a new
listing appeared, a monitor failed, or names await onboarding.

**Safe by default: read-only.** It performs no canonical mutation and no onboarding — onboarding stays
an explicit, targeted, vendor-lag-aware action (a new IPO's market data lags its KAP listing, so the
cycle *reports* the new name rather than blindly pulling data the vendor does not have yet). The pure
``summarize_cycle`` composes the verdict from the sub-results so it is unit-testable without I/O.
"""
from __future__ import annotations

from datetime import date, datetime


def summarize_cycle(*, new_listings: dict, idbridge: dict, smoke: dict, registry: dict,
                    queue_len: int) -> dict:
    """Pure: fold the sub-tool results into a consolidated verdict.

    ``attention`` is True when a new listing was detected, any health monitor failed, or the onboarding
    queue is non-empty — i.e. anything a scheduled run should surface. ``health_ok`` isolates the three
    monitors (a substrate-integrity regression is more urgent than a pending onboarding)."""
    n_new = new_listings.get("n_new", 0)
    monitors = {"idbridge": bool(idbridge.get("passes")),
                "smoke_drift": bool(smoke.get("passes")),
                "registry": bool(registry.get("passes"))}
    health_ok = all(monitors.values())
    reasons: list[str] = []
    if n_new:
        reasons.append(f"{n_new} new listing(s) detected")
    for name, ok in monitors.items():
        if not ok:
            reasons.append(f"monitor FAILED: {name}")
    if queue_len:
        reasons.append(f"{queue_len} name(s) awaiting onboarding")
    return {
        "attention": bool(reasons),
        "health_ok": health_ok,
        "monitors": monitors,
        "n_new_listings": n_new,
        "onboarding_queue_len": queue_len,
        "reasons": reasons,
    }


def run_keep_current(con, store, *, as_of: date | None = None, write_report: bool = False) -> dict:
    """Run the full read-only cycle over the real graph + L2 and return the consolidated report.

    No network, no mutation (the KAP member cache + smoke reports are refreshed by their own ingest
    jobs; this cycle reads what they last wrote). Writes ``data/cache/keep_current_report.json`` (§4)
    when ``write_report``."""
    as_of = as_of or date.today()
    from tmkg.ingest.new_listings import detect_new_listings
    from tmkg.ingest.onboarding_queue import onboarding_queue
    from tmkg.monitor.idbridge_health import idbridge_health
    from tmkg.monitor.registry_hygiene import registry_hygiene
    from tmkg.monitor.smoke_drift import smoke_drift_status

    new_listings = detect_new_listings(con, members_path=None)
    idbridge = idbridge_health(con, full_round_trip=False)   # collisions+coverage (fast); sweep is heavier
    smoke = smoke_drift_status()
    registry = registry_hygiene(store, as_of=as_of)
    queue = onboarding_queue(con, store, as_of=as_of)

    verdict = summarize_cycle(new_listings=new_listings, idbridge=idbridge, smoke=smoke,
                              registry=registry, queue_len=len(queue))
    report = {
        "tool": "keep_current_cycle",
        "_generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": str(as_of),
        "verdict": verdict,
        "new_listings": {"n_new": new_listings.get("n_new"),
                         "tickers": [n.get("ticker") for n in new_listings.get("new_listings", [])]},
        "health": {"idbridge": idbridge.get("passes"), "smoke_drift": smoke.get("passes"),
                   "registry": registry.get("passes")},
        "onboarding_queue": {"len": len(queue),
                             "top": [{"ticker": e["ticker"], "next_step": e["next_step"]} for e in queue[:15]]},
        "note": ("read-only heartbeat — no mutation, no onboarding. Onboarding is an explicit targeted "
                 "vendor-lag-aware action; a new IPO's market data lags its KAP listing, so this reports "
                 "the name rather than pulling data the vendor does not carry yet."),
    }
    if write_report:
        from tmkg.ingest.audit import write_run_report
        write_run_report("keep_current", report)
    return report

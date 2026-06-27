"""New-listing / IPO detector (onboarding step 3a) — find names listed upstream but not yet in the substrate.

A newly-listed BIST firm appears in KAP's (weekly-refreshed) member list before it exists in our L1
graph or L2 quant store. Today nothing notices that gap — this closes it: diff the **upstream listed
universe** (the cached KAP member list) against the **known universe** (Company nodes already in the
graph) and surface the new entities to onboard (plus candidate retirements and ticker changes).

**The diff keys on ``kap_oid`` (KAP's stable org-id), NOT the ticker.** Tickers drift — KAP renames and
multi-codes them (e.g. the graph's ``GARAN`` is listed upstream as ``GARAN, TGB``) — so a raw-ticker diff
over-reports massively (44 false "new" / 43 false "retired" on the real data); the kap_oid diff finds the
*one* genuinely new entity. A ticker that changed for a *known* kap_oid is surfaced separately as an
id-bridge reconciliation, not a new listing.

The diff is pure and unit-tested; the readers pull the cached KAP JSON (local, no network — §4) and the
L1 graph (structural, read directly like the id-bridge). Detection only — it does **not** mutate the graph
or pull prices; the onboarding orchestrator (step 3b) consumes its report.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import tmkg.config as config


def _ticker_tokens(raw: str | None) -> set[str]:
    """KAP sometimes multi-codes a ticker ('GARAN, TGB'); split into comparable tokens."""
    if not raw:
        return set()
    return {tok.strip().upper() for tok in raw.replace(",", " ").split() if tok.strip()}


@dataclass(frozen=True)
class ListingDiff:
    new_listings: list[dict]          # upstream kap_oid not in graph — to onboard (carries identity)
    retired_candidates: list[dict]    # graph kap_oid no longer upstream-listed — survivorship keeps, flag
    ticker_changes: list[dict]        # same kap_oid, graph ticker no longer among the upstream tokens
    n_upstream: int
    n_known: int

    @property
    def in_sync(self) -> bool:
        return not self.new_listings and not self.retired_candidates and not self.ticker_changes

    def summary(self) -> dict:
        return {
            "n_upstream_listed": self.n_upstream,
            "n_known_in_graph": self.n_known,
            "n_new": len(self.new_listings),
            "n_retired_candidates": len(self.retired_candidates),
            "n_ticker_changes": len(self.ticker_changes),
            "in_sync": self.in_sync,
        }


def diff_listings(upstream: dict[str, dict], known: dict[str, str]) -> ListingDiff:
    """Pure diff, keyed on ``kap_oid``.

    ``upstream`` maps kap_oid → KAP identity record (incl. ``ticker``/``name``/``mkk_oid``); ``known``
    maps kap_oid → the ticker already stored in the graph. New = upstream − known (carry identity);
    retired = known − upstream; ticker_changes = shared kap_oid whose graph ticker is no longer among
    the upstream ticker tokens (an id-bridge reconciliation, not a new listing)."""
    up_oids = set(upstream)
    known_oids = set(known)

    new_listings = [
        {"kap_oid": oid, "ticker": upstream[oid].get("ticker"),
         "name": upstream[oid].get("name"), "mkk_oid": upstream[oid].get("mkk_oid")}
        for oid in sorted(up_oids - known_oids)
    ]
    retired_candidates = [
        {"kap_oid": oid, "ticker": known[oid]} for oid in sorted(known_oids - up_oids)
    ]
    ticker_changes = []
    for oid in sorted(up_oids & known_oids):
        graph_tk = (known[oid] or "").upper()
        up_tokens = _ticker_tokens(upstream[oid].get("ticker"))
        if graph_tk and up_tokens and graph_tk not in up_tokens:
            ticker_changes.append({"kap_oid": oid, "graph_ticker": known[oid],
                                   "upstream_ticker": upstream[oid].get("ticker")})
    return ListingDiff(new_listings=new_listings, retired_candidates=retired_candidates,
                       ticker_changes=ticker_changes, n_upstream=len(up_oids), n_known=len(known_oids))


# --- readers ---------------------------------------------------------------------------------


def upstream_listed_from_kap(members_path: str | Path | None = None) -> tuple[dict[str, dict], str]:
    """Read the cached KAP member list → {kap_oid: identity record} for currently-listed IGS names.

    Only ``is_listed`` IGS members with a kap_oid are included (a name that delisted upstream drops out
    and becomes a retirement candidate). Returns (mapping, provenance). Local read, no network."""
    path = Path(members_path) if members_path else (config.REPO_ROOT / "data" / "cache" / "kap_members.json")
    if not path.exists():
        raise FileNotFoundError(f"KAP member cache not found at {path} — run scripts/ingest_kap.py --seed")
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for m in data.get("members", []):
        oid = m.get("kap_oid")
        if not oid or not m.get("is_listed", False):
            continue
        if m.get("member_type") and m["member_type"] != "IGS":
            continue
        out[oid] = m
    prov = f"{path.name} (fetched {data.get('fetched_iso', '?')}, {len(out)} listed)"
    return out, prov


def known_from_graph(con) -> dict[str, str]:
    """{kap_oid: ticker} for every Company node carrying a kap_oid (the graph's known KAP entities)."""
    res = con.execute("MATCH (c:Company) WHERE c.kap_oid IS NOT NULL RETURN c.kap_oid, c.ticker")
    out: dict[str, str] = {}
    while res.has_next():
        oid, tk = res.get_next()
        out[oid] = tk
    return out


def detect_new_listings(
    con,
    *,
    members_path: str | Path | None = None,
    write_report: bool = False,
) -> dict:
    """Diff the cached KAP listed universe against the graph (on kap_oid) and build the onboarding report.

    Reads cached KAP + the graph (no network). Returns the report dict; if ``write_report`` writes
    ``data/cache/new_listings_report.json`` (§4)."""
    upstream, prov = upstream_listed_from_kap(members_path)
    known = known_from_graph(con)
    diff = diff_listings(upstream, known)

    report = {
        "tool": "new_listing_detector",
        "_generated_at": datetime.now().isoformat(timespec="seconds"),
        "upstream_source": prov,
        "diff_key": "kap_oid (stable; tickers drift)",
        **diff.summary(),
        "new_listings": diff.new_listings,
        "retired_candidates": diff.retired_candidates,
        "ticker_changes": diff.ticker_changes,
        "note": ("detection only — does not mutate the graph or pull prices. New entities carry KAP "
                 "identity (kap_oid/mkk_oid) for the onboarding orchestrator (step 3b). Retired "
                 "candidates are KEPT (survivorship), only flagged. Ticker changes are id-bridge "
                 "reconciliations, not new listings."),
    }
    if write_report:
        from tmkg.ingest.audit import write_run_report
        write_run_report("new_listings", report)
    return report

"""New-listing onboarding orchestrator (M9.3b) — bring a detected new entity to a tradeable substrate row.

Consumes the 3a detector's new-entity list and drives the existing, tested ingestion entry points in
the correct order until a new name has everything a tradeable substrate row needs:

  1. **kap_identity**   — Company/Security/ISSUES seeded from KAP (idempotent MERGE).
  2. **gleif_identity** — LEI + ISIN back-filled (the id-bridge legs).
  3. **sector**         — IN_SECTOR linked from the committed KAP taxonomy.
  4. **universe_prices**— Matriks OHLCV → `prices`, USD `total_returns`, and `universe_class` (L2).
  5. **factor_refit**   — M2/M3 refit → `betas` / `residuals` for the new name.

The chain is **coherent and idempotent**: each step is a whole-job run that *picks up* the new entity
(once step 1 puts it in the graph, the universe pull and refit include it) and is a no-op for everyone
else (MERGE / PK-idempotent writes). Steps run **in order**; a step is skipped if its artifact already
exists (``onboarding_status``). Default mode is **dry-run** — it plans and verifies but performs no
network call or mutation; ``execute=True`` runs the remaining steps. Fail-loud, never fabricate (§4):
a step that errors stops the chain for that entity rather than leaving a half-onboarded name.

This module defines the *recipe + completion check*; the steps delegate to the existing scripts, so
there is one source of truth for each adapter's logic.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class OnboardingStep:
    name: str
    description: str
    command: tuple[str, ...]      # argv delegated to an existing entry point
    writes: str
    idempotent: bool = True

    def as_dict(self) -> dict:
        return {"step": self.name, "description": self.description,
                "command": " ".join(self.command), "writes": self.writes,
                "idempotent": self.idempotent}


def onboarding_steps(*, as_of: date, price_start: str) -> tuple[OnboardingStep, ...]:
    """The ordered onboarding pipeline. Commands are whole-job (idempotent) — each picks up the new
    entity once step 1 has seeded it into the graph."""
    a = str(as_of)
    return (
        OnboardingStep("kap_identity",
                       "seed Company/Security/ISSUES from the KAP member list (MERGE)",
                       ("scripts/ingest_kap.py", "--seed"), "L1 Company/Security/ISSUES"),
        OnboardingStep("gleif_identity",
                       "back-fill LEI + ISIN (the id-bridge legs) for names still missing them",
                       ("scripts/backfill_gleif.py",), "L1 Company.lei / Company.isin"),
        OnboardingStep("sector",
                       "link IN_SECTOR from the committed KAP sector taxonomy",
                       ("scripts/backfill_sectors.py",), "L1 IN_SECTOR"),
        OnboardingStep("universe_prices",
                       "pull Matriks OHLCV -> prices + USD total_returns + universe_class",
                       ("scripts/ingest_universe.py", price_start, a, a), "L2 prices/total_returns/universe_membership"),
        OnboardingStep("factor_refit",
                       "M2/M3 refit -> betas + residuals for the new name",
                       ("scripts/run_m3_gate.py", a), "L2 betas/residuals"),
    )


# --- completion status (reads real L1 graph + L2; no mutation) --------------------------------


def onboarding_status(con, store, *, kap_oid: str, ticker: str | None) -> dict:
    """Which onboarding stages are already complete for ``kap_oid`` / ``ticker`` (reads only)."""
    # L1: company + identity legs + sector
    res = con.execute(
        "MATCH (c:Company {kap_oid: $oid}) "
        "OPTIONAL MATCH (c)-[:IN_SECTOR]->(s:Sector) "
        "RETURN c.ticker, c.lei, c.isin, count(s)", {"oid": kap_oid})
    row = res.get_next() if res.has_next() else None
    has_company = row is not None
    tk = (row[0] if row else None) or ticker
    has_gleif = bool(row and row[1] and row[2])
    has_sector = bool(row and row[3] and row[3] > 0)

    # L2: total_returns + universe_class + betas for the ticker (latest-known, via PIT)
    has_prices = has_universe = has_betas = False
    if tk is not None:
        from tmkg.pit.access import PITAccess
        c2 = store.connect()
        try:
            pit = PITAccess(date.today(), l2=c2)
            has_prices = not pit.series("total_returns", symbol=tk).empty
            has_betas = not pit.series("betas", symbol=tk).empty
            has_universe = not pit.series("universe_membership", symbol=tk).empty
        finally:
            c2.close()

    stages = {
        "kap_identity": has_company,
        "gleif_identity": has_gleif,
        "sector": has_sector,
        "universe_prices": has_prices and has_universe,
        "factor_refit": has_betas,
    }
    complete = all(stages.values())
    pending = [name for name, done in stages.items() if not done]
    return {"kap_oid": kap_oid, "ticker": tk, "stages": stages,
            "complete": complete, "pending": pending,
            "next_step": pending[0] if pending else None}


# --- plan + run -------------------------------------------------------------------------------


def plan_for_entity(entity: dict, status: dict, steps: tuple[OnboardingStep, ...]) -> dict:
    """The ordered remaining steps for one new entity (skipping already-complete stages)."""
    done = status["stages"]
    remaining = [s.as_dict() for s in steps if not done.get(s.name, False)]
    return {"kap_oid": entity["kap_oid"], "ticker": entity.get("ticker"),
            "name": entity.get("name"), "complete": status["complete"],
            "remaining_steps": remaining}


def run_onboarding(
    con,
    store,
    *,
    new_entities: list[dict] | None = None,
    members_path=None,
    as_of: date | None = None,
    price_start: str = "2023-01-01",
    execute: bool = False,
    report_dir: str | Path | None = None,
) -> dict:
    """Plan (and, if ``execute``, run) onboarding for every detected new entity.

    ``new_entities`` may be injected; otherwise read from the 3a detector. Dry-run by default — no
    network, no mutation; returns the per-entity plan + current completion status. ``execute=True``
    runs each entity's remaining steps in order (idempotent; stops that entity's chain on first error)
    and re-verifies. Writes a §4 report if ``report_dir``."""
    as_of = as_of or date.today()
    steps = onboarding_steps(as_of=as_of, price_start=price_start)
    if new_entities is None:
        from tmkg.ingest.new_listings import detect_new_listings
        new_entities = detect_new_listings(con, members_path=members_path)["new_listings"]

    entities_out: list[dict] = []
    for ent in new_entities:
        status = onboarding_status(con, store, kap_oid=ent["kap_oid"], ticker=ent.get("ticker"))
        plan = plan_for_entity(ent, status, steps)
        if execute and not status["complete"]:
            plan["execution"] = _execute_remaining(plan["remaining_steps"])
            # re-verify after execution
            status = onboarding_status(con, store, kap_oid=ent["kap_oid"], ticker=ent.get("ticker"))
            plan["complete_after"] = status["complete"]
            plan["pending_after"] = status["pending"]
        entities_out.append(plan)

    report = {
        "tool": "new_listing_onboarding",
        "_generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "execute" if execute else "dry-run",
        "as_of": str(as_of),
        "n_entities": len(entities_out),
        "pipeline": [s.as_dict() for s in steps],
        "entities": entities_out,
        "note": ("dry-run plans + verifies only (no network/mutation). execute=True runs the remaining "
                 "idempotent steps in order, fail-loud per entity. Each step is a whole-job run that "
                 "picks up the new entity and no-ops the rest (§4)."),
    }
    if report_dir is not None:
        from tmkg.ingest.audit import write_run_report
        write_run_report("onboarding", report)
    return report


def _execute_remaining(remaining_steps: list[dict]) -> list[dict]:
    """Run the remaining step commands in order via subprocess; stop on first failure (fail-loud)."""
    results: list[dict] = []
    repo_root = Path(__file__).resolve().parents[3]
    for step in remaining_steps:
        argv = [sys.executable, *step["command"].split()]
        proc = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True,
                              env={"PYTHONPATH": "src", **_os_environ()})
        ok = proc.returncode == 0
        results.append({"step": step["step"], "ok": ok, "returncode": proc.returncode,
                        "tail": (proc.stdout or proc.stderr)[-400:]})
        if not ok:
            break  # fail-loud: do not continue a half-onboarded chain
    return results


def _os_environ() -> dict:
    import os
    return dict(os.environ)

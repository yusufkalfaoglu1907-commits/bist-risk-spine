"""New-listing onboarding orchestrator (M9.3b) — pipeline recipe + plan logic, deterministic.

The orchestrator's brain is the ordered, idempotent pipeline and the "skip already-complete stages"
plan logic; the steps themselves delegate to existing scripts. These tests pin the recipe (order,
idempotency, real script targets) and the planning (skips done stages, full plan when nothing done).
The status-against-real-graph path is exercised by the dry-run on the live store, not a heavy fixture.
"""
from __future__ import annotations

import datetime as dt
import pathlib

from tmkg.ingest.onboarding import onboarding_steps, plan_for_entity

_STEP_ORDER = ["kap_identity", "gleif_identity", "sector", "universe_prices", "factor_refit"]


def _steps():
    return onboarding_steps(as_of=dt.date(2026, 6, 27), price_start="2023-01-01")


def test_pipeline_order_and_idempotency():
    steps = _steps()
    assert [s.name for s in steps] == _STEP_ORDER          # identity → prices → refit
    assert all(s.idempotent for s in steps)                 # every step is safe to re-run


def test_commands_reference_real_scripts():
    repo = pathlib.Path(__file__).resolve().parents[2]
    for s in _steps():
        script = s.command[0]
        assert (repo / script).exists(), f"onboarding step {s.name} points at missing {script}"


def test_plan_skips_completed_stages():
    entity = {"kap_oid": "O9", "ticker": "NEWCO", "name": "NewCo A.Ş."}
    status = {"stages": {"kap_identity": True, "gleif_identity": True,
                         "sector": False, "universe_prices": False, "factor_refit": False},
              "complete": False}
    plan = plan_for_entity(entity, status, _steps())
    assert [s["step"] for s in plan["remaining_steps"]] == ["sector", "universe_prices", "factor_refit"]
    assert plan["ticker"] == "NEWCO"


def test_plan_full_when_nothing_done():
    entity = {"kap_oid": "O9", "ticker": "NEWCO", "name": "NewCo"}
    status = {"stages": {n: False for n in _STEP_ORDER}, "complete": False}
    plan = plan_for_entity(entity, status, _steps())
    assert [s["step"] for s in plan["remaining_steps"]] == _STEP_ORDER


def test_plan_empty_when_complete():
    entity = {"kap_oid": "O9", "ticker": "NEWCO"}
    status = {"stages": {n: True for n in _STEP_ORDER}, "complete": True}
    plan = plan_for_entity(entity, status, _steps())
    assert plan["remaining_steps"] == []
    assert plan["complete"]

"""Signal-registry hygiene invariant (M8.3) — the verdict ledger has no incoherent rows.

A teeth test (synthetic, always runs) proves the integrity checks bite — a promoted row that fails
its gate, a missing n_trials haircut, an out-of-range PBO are each flagged. A real-L2 test asserts
the live ledger is clean (skips when the gitignored L2 db is absent).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tmkg.monitor.registry_hygiene import registry_hygiene


def _row(**kw):
    base = dict(signal_id="s", hypothesis="", feature_family="f",
                train_start=None, train_end=None, test_start=None, test_end=None,
                n_trials=12, cost_model="", purge_embargo="", deflated_sharpe=0.1,
                pbo=0.3, beat_baselines=True, book="venue_feasible", promoted=False,
                knowledge_date=dt.date(2026, 6, 1))
    base.update(kw)
    return base


# --- teeth: each check catches its corruption ------------------------------------------------

def test_clean_ledger_passes():
    df = pd.DataFrame([_row(signal_id="m5", promoted=False),
                       _row(signal_id="m6", promoted=False, beat_baselines=False)])  # NO-GO, fine
    rep = registry_hygiene(df=df)
    assert rep["passes"], rep["failures"]
    assert rep["promoted_count"] == 0


def test_incoherent_promotion_is_flagged():
    # promoted=True but did NOT beat baselines -> incoherent
    df = pd.DataFrame([_row(signal_id="x", promoted=True, beat_baselines=False)])
    rep = registry_hygiene(df=df)
    assert not rep["passes"]
    assert any("gate components do not clear" in f for f in rep["failures"])


def test_missing_trial_haircut_is_flagged():
    df = pd.DataFrame([_row(signal_id="x", n_trials=None)])
    rep = registry_hygiene(df=df)
    assert not rep["passes"]
    assert any("n_trials" in f for f in rep["failures"])


def test_out_of_range_pbo_is_flagged():
    df = pd.DataFrame([_row(signal_id="x", pbo=1.4)])
    rep = registry_hygiene(df=df)
    assert not rep["passes"]
    assert any("pbo" in f for f in rep["failures"])


def test_multi_version_signal_is_surfaced_not_failed():
    df = pd.DataFrame([
        _row(signal_id="x", knowledge_date=dt.date(2026, 6, 1), promoted=False),
        _row(signal_id="x", knowledge_date=dt.date(2026, 6, 8), promoted=False),  # re-eval
    ])
    rep = registry_hygiene(df=df)
    assert rep["passes"]                       # versioning is legitimate, not a dup
    assert rep["multi_version_signals"] == {"x": 2}
    assert rep["latest_verdicts"][0]["knowledge_date"] == "2026-06-08"  # latest surfaced


# --- the real ledger is clean ----------------------------------------------------------------

@pytest.mark.invariant
def test_real_registry_is_clean():
    import pathlib

    from tmkg.l2.store import L2Store
    store = L2Store()
    if not pathlib.Path(store.db_path).exists():
        pytest.skip("L2 db not present (gitignored) — run a gate to populate signal_registry")
    store.bootstrap_schema()
    rep = registry_hygiene(store)
    assert rep["passes"], f"signal_registry has incoherent rows: {rep['failures']}"

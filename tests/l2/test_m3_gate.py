"""m3_residual_survival_gate — reads landed residuals back through PIT and emits the
[STOP]-gate verdict. Offline, synthetic L2 seed; proves the runner plumbs residuals into the
stability machinery, honors the within-sector restriction, and respects the PIT gate.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tmkg.l2.store import L2Store
from tmkg.signals.gate import m3_residual_survival_gate, write_gate_report


def _store(tmp_path) -> L2Store:
    store = L2Store(db_path=tmp_path / "l2.duckdb")
    store.bootstrap_schema()
    return store


def _seed_residuals(store: L2Store, *, n=480, seed=1, block_strength=0.85):
    """Two sectors: a 'bank' block sharing a persistent latent factor (genuine residual
    linkage) and a 'noise' set of independent names. knowledge_date = bar_date."""
    dates = [d.date() for d in pd.bdate_range("2023-01-02", periods=n)]
    rng = np.random.default_rng(seed)
    latent = rng.standard_normal(n)
    frames = []
    sectors = {}
    for i in range(6):
        sym = f"BLK{i}"
        resid = block_strength * latent + np.sqrt(1 - block_strength**2) * rng.standard_normal(n)
        sectors[sym] = "bank"
        frames.append(pd.DataFrame({
            "symbol": sym, "bar_date": dates, "residual": resid,
            "strip_order": "XU100>USDTRY>FFLOW", "universe_class": "operating",
            "knowledge_date": dates,
        }))
    for i in range(8):
        sym = f"NZ{i}"
        sectors[sym] = "noise"
        frames.append(pd.DataFrame({
            "symbol": sym, "bar_date": dates, "residual": rng.standard_normal(n),
            "strip_order": "XU100>USDTRY>FFLOW", "universe_class": "operating",
            "knowledge_date": dates,
        }))
    store.write_parquet("residuals", pd.concat(frames, ignore_index=True))
    return dates, sectors


def test_gate_persistent_block_is_GO(tmp_path):
    store = _store(tmp_path)
    dates, sectors = _seed_residuals(store)
    rep = m3_residual_survival_gate(
        store, as_of=dates[-1], sectors=sectors,
        window=120, alpha=0.05, min_obs=80, panel_min_obs=100,
    )
    assert rep["milestone"] == "M3" and rep["stop_gate"] is True
    assert rep["inputs"]["n_symbols_panel"] == 14
    assert rep["inputs"]["n_symbols_with_sector"] == 14
    assert rep["decision"]["decision"] == "GO", rep["summary"]
    # report is JSON-serializable (dates coerced)
    out = write_gate_report(rep, tmp_path / "m3_gate_report.json")
    assert out.exists()


def test_gate_structureless_is_NO_GO(tmp_path):
    store = _store(tmp_path)
    dates = [d.date() for d in pd.bdate_range("2023-01-02", periods=480)]
    rng = np.random.default_rng(2)
    sectors = {}
    frames = []
    for i in range(14):
        sym = f"S{i}"
        sectors[sym] = "bank" if i % 2 == 0 else "steel"
        frames.append(pd.DataFrame({
            "symbol": sym, "bar_date": dates, "residual": rng.standard_normal(480),
            "strip_order": "XU100", "universe_class": "operating", "knowledge_date": dates,
        }))
    store.write_parquet("residuals", pd.concat(frames, ignore_index=True))
    rep = m3_residual_survival_gate(store, as_of=dates[-1], sectors=sectors,
                                    window=120, min_obs=80, panel_min_obs=100)
    assert rep["decision"]["decision"] == "NO-GO", rep["summary"]


def test_gate_pit_gate_hides_future_residuals(tmp_path):
    store = _store(tmp_path)
    dates, sectors = _seed_residuals(store)
    # as_of near the very start: too few residuals visible to form even one window -> NO-GO,
    # never a fabricated verdict from data that wasn't knowable yet.
    rep = m3_residual_survival_gate(store, as_of=dates[10], sectors=sectors,
                                    window=120, min_obs=80, panel_min_obs=100)
    assert rep["inputs"]["n_symbols_panel"] == 0  # <100 obs visible -> all dropped
    assert rep["decision"]["decision"] == "NO-GO"


def test_gate_empty_residuals_is_NO_GO(tmp_path):
    store = _store(tmp_path)
    rep = m3_residual_survival_gate(store, as_of=date(2025, 1, 1), sectors={})
    assert rep["inputs"]["n_residual_rows"] == 0
    assert rep["decision"]["decision"] == "NO-GO"

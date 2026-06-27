"""Scenario-analysis runner — re-price the current exposure tensor against a scenario library (M8.1).

The one L2/PIT-touching piece of the risk tool. All arithmetic lives in the pure modules
(``repricing`` + the audited ``events.channel_stress``); this orchestrates them over the real store
and writes an honest §4 report:

  1. PIT-read ``betas`` (and, for the empirical path, ``factors``) at ``as_of`` — no raw SELECT, no
     network (§4);
  2. build the latest symbol × channel **exposure tensor**;
  3. re-price the **stylized** library + (optionally) an **empirical** scenario derived from real
     factor returns over a historical window, optionally against a ``weights`` book;
  4. report worst/best-exposed names, portfolio stress P&L, per-name coverage, and any unmodelled
     channels — surfacing thin coverage rather than masking it.

This is a **risk** tool, not a signal: no Sharpe, no promotion gate, **no ``signal_registry``
write** (a scenario re-pricing is not a tradeable claim). Inputs are injectable (``inputs=``) so the
self-test runs on a synthetic world with no L2/network.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from tmkg.risk.repricing import (
    CHANNEL_FACTOR,
    latest_exposure_tensor,
    realized_channel_shock,
    reprice_suite,
)
from tmkg.risk.scenarios import Scenario, scenario_from_factor_returns, stylized_library


def _registry_methods() -> dict[str, str]:
    """Factor → return method, read from the M2 factor registry (default simple)."""
    try:
        from tmkg.factors.registry import specs
        return dict(specs(available_only=False))
    except Exception:
        return {}


def _load_inputs(store, as_of: date):
    """PIT-read betas + factors at ``as_of`` (no network, no raw SELECT)."""
    from tmkg.pit.access import PITAccess

    con = store.connect()
    try:
        pit = PITAccess(as_of, l2=con)
        betas_df = pit.series("betas")
        factors_df = pit.series("factors")
    finally:
        con.close()
    return betas_df, factors_df


def run_scenario_analysis(
    store=None,
    *,
    as_of: date,
    inputs=None,
    scenarios: dict[str, Scenario] | None = None,
    weights: pd.Series | None = None,
    channel_factor: dict[str, str] | None = None,
    empirical_window: tuple[date, date] | None = None,
    empirical_name: str = "historical_window",
    report_dir: str | Path | None = None,
    report_name: str = "m8_scenario_report",
) -> dict:
    """Re-price the exposure tensor at ``as_of`` against ``scenarios`` (default: the stylized library).

    ``inputs`` = ``(betas_df, factors_df)`` may be injected (the self-test path); otherwise read from
    ``store`` through PITAccess. When ``empirical_window=(start, end)`` is given, an **empirical**
    scenario built from the real factor returns over that window is added to the suite (unit-correct,
    the trustworthy episode re-pricing). Returns the report dict and, if ``report_dir``, writes the
    §4 JSON audit."""
    channel_factor = channel_factor or CHANNEL_FACTOR
    lib = dict(scenarios) if scenarios is not None else stylized_library()

    if inputs is None:
        if store is None:
            raise ValueError("run_scenario_analysis needs either `store` or injected `inputs`")
        betas_df, factors_df = _load_inputs(store, as_of)
    else:
        betas_df, factors_df = inputs

    exposures = latest_exposure_tensor(betas_df, channel_factor=channel_factor)
    if exposures.empty:
        return _empty_report(as_of, report_name, reason="no betas at as_of — empty exposure tensor")

    empirical_meta = None
    if empirical_window is not None:
        start, end = empirical_window
        shock = realized_channel_shock(
            factors_df, start=start, end=end,
            channel_factor=channel_factor, methods=_registry_methods())
        if shock:
            sc = scenario_from_factor_returns(
                empirical_name, shock,
                description=f"Realized factor returns over {start}→{end} (empirical, unit-correct).",
                provenance=f"factors {start}→{end} via PITAccess @ {as_of}")
            lib[sc.name] = sc
            empirical_meta = {"window": [str(start), str(end)], "shock": sc.as_dict()["shocks"]}
        else:
            empirical_meta = {"window": [str(start), str(end)], "shock": {},
                              "note": "no factor levels in window — empirical scenario skipped"}

    results = reprice_suite(exposures, lib, weights=weights)
    skipped = [name for name in lib if name not in results]

    report = _build_report(as_of, report_name, exposures, channel_factor, lib, results,
                           skipped, weights, empirical_meta)
    if report_dir is not None:
        _write_report(report, Path(report_dir) / f"{report_name}.json")
    return report


def _build_report(as_of, report_name, exposures, channel_factor, lib, results, skipped,
                  weights, empirical_meta) -> dict:
    return {
        "milestone": "M8",
        "tool": "scenario_repricing",
        "report": report_name,
        "as_of": str(as_of),
        "note": ("risk re-pricing, not a signal — no Sharpe / promotion gate / registry write. "
                 "A scenario answers 'if these channels move, what happens to the book', not 'will they'."),
        "exposure_tensor": {
            "n_names": int(exposures.shape[0]),
            "channels": list(exposures.columns),
            "channel_factor": {ch: channel_factor[ch] for ch in channel_factor},
            "min_per_channel_coverage": {
                ch: float(exposures[ch].notna().mean()) for ch in exposures.columns},
        },
        "weighted_book": (weights is not None),
        "empirical": empirical_meta,
        "scenarios": [results[name].summary() for name in lib if name in results],
        "skipped_scenarios": [
            {"name": name, "tier": lib[name].tier,
             "reason": "no shocked channel has an exposure column (all unmodelled) — not re-priced"}
            for name in skipped
        ],
    }


def _empty_report(as_of, report_name, *, reason: str) -> dict:
    return {"milestone": "M8", "tool": "scenario_repricing", "report": report_name,
            "as_of": str(as_of), "scenarios": [], "reason": reason}


def _write_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
    return path

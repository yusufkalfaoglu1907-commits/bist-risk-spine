"""Signal-registry hygiene monitor (M8.3) — the L2 verdict ledger stays coherent.

The ``signal_registry`` is the durable record of every promotion gate run (a rejection is as durable
as a promotion — M5/M6 are both NO-GO rows). It is bitemporal and append-only (PK = signal_id +
knowledge_date), so re-evaluating a signal adds a *new* dated row, not an overwrite. This monitor
sweeps the whole visible ledger and fails loudly on a row that is internally **incoherent** — the
kind of corruption that would let a non-real signal read as promoted:

  * **incoherent promotion** — ``promoted=True`` while the gate components don't actually clear
    (``beat_baselines`` false, or DSR / PBO missing). Promotion is an AND of those; a promoted row
    that fails them is a writer bug, not a verdict.
  * **missing trial-count haircut** — ``n_trials`` null or < 1. The M4 D1 fix made ``n_trials``
    required; a row without it was scored without the data-mining adjustment.
  * **out-of-range statistics** — ``pbo`` outside [0, 1] or a non-finite ``deflated_sharpe``.

It also *surfaces* (not a failure) multi-version signals and the latest verdict per signal, so an old
dated row can't be mistaken for the current one. Reads the ledger through ``PITAccess`` (§5 — the only
sanctioned L2 read path); pure read, no mutation, no network.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def registry_hygiene(store=None, *, as_of: date | None = None, df: pd.DataFrame | None = None) -> dict:
    """Sweep ``signal_registry`` (all rows visible at ``as_of``) for incoherent verdict rows.

    ``df`` may be injected (the teeth test); otherwise the ledger is read from ``store`` through
    ``PITAccess`` at ``as_of`` (default today). ``passes`` is True iff no hard-integrity issue is
    found. Multi-version signals + latest verdicts are surfaced, never failed on."""
    if df is None:
        if store is None:
            raise ValueError("registry_hygiene needs either `store` or an injected `df`")
        from tmkg.pit.access import PITAccess
        as_of = as_of or date.today()
        con = store.connect()
        try:
            df = PITAccess(as_of, l2=con).series("signal_registry")
        finally:
            con.close()

    n = len(df)
    issues: list[dict] = []

    def _tag(row, msg: str) -> None:
        issues.append({"signal_id": row.signal_id, "knowledge_date": str(row.knowledge_date),
                       "issue": msg})

    for row in df.itertuples(index=False):
        # incoherent promotion
        if bool(getattr(row, "promoted", False)):
            if (not bool(getattr(row, "beat_baselines", False))
                    or pd.isna(getattr(row, "deflated_sharpe", np.nan))
                    or pd.isna(getattr(row, "pbo", np.nan))):
                _tag(row, "promoted=True but gate components do not clear (beat_baselines/DSR/PBO)")
        # missing trial-count haircut
        nt = getattr(row, "n_trials", None)
        if nt is None or pd.isna(nt) or int(nt) < 1:
            _tag(row, f"n_trials missing/<1 ({nt!r}) — DSR data-mining haircut not applied")
        # out-of-range statistics
        pbo = getattr(row, "pbo", np.nan)
        if pd.notna(pbo) and not (0.0 <= float(pbo) <= 1.0):
            _tag(row, f"pbo {pbo!r} outside [0,1]")
        dsr = getattr(row, "deflated_sharpe", np.nan)
        if pd.notna(dsr) and not np.isfinite(float(dsr)):
            _tag(row, f"deflated_sharpe non-finite ({dsr!r})")

    # surfaced (soft): version counts + the latest verdict per signal
    multi_version: dict[str, int] = {}
    latest_verdicts: list[dict] = []
    if n:
        vc = df.groupby("signal_id")["knowledge_date"].nunique()
        multi_version = {k: int(v) for k, v in vc.items() if v > 1}
        for sid, grp in df.groupby("signal_id"):
            row = grp.loc[grp["knowledge_date"].idxmax()]
            latest_verdicts.append({"signal_id": sid, "knowledge_date": str(row["knowledge_date"]),
                                    "promoted": bool(row["promoted"]),
                                    "n_versions": int((df["signal_id"] == sid).sum())})

    return {
        "monitor": "registry_hygiene",
        "n_rows": int(n),
        "n_signals": int(df["signal_id"].nunique()) if n else 0,
        "promoted_count": int(df["promoted"].sum()) if n else 0,
        "issues": issues,
        "multi_version_signals": multi_version,
        "latest_verdicts": latest_verdicts,
        "passes": not issues,
        "failures": [f"{i['signal_id']}@{i['knowledge_date']}: {i['issue']}" for i in issues],
    }


def write_registry_hygiene_report(store, **kwargs):
    """Run the monitor and write ``data/cache/registry_hygiene_report.json`` (§4). Returns (report, path)."""
    from tmkg.ingest.audit import write_run_report
    report = registry_hygiene(store, **kwargs)
    path = write_run_report("registry_hygiene", report)
    return report, path

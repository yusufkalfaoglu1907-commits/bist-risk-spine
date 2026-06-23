"""Signal registry — every candidate logged with its honesty stats (BUILD_PLAN.md M4).

The registry is the audit trail that makes "we tried it and it failed" as durable as "we
promoted it". Every candidate that reaches the promotion gate (promotion.py) writes one
bitemporal row to the L2 ``signal_registry`` table (l2/schema.sql) — hypothesis, feature
family, train/test dates, **trial count**, cost assumption, purge/embargo params, **Deflated
Sharpe**, **PBO**, whether it beat the baseline ladder, the book it was judged in, and the
promote/reject verdict. Promotion keys on DSR (the trial-count-adjusted statistic), never raw
Sharpe (VERIFICATION §3).

Writes go through L2Store (append-only, PK-idempotent, Parquet-backed). Reads in signal code
go through PITAccess (``signal_registry`` is on its allow-list). This module composes a row
from a ``PromotionResult`` and metadata; it neither computes statistics nor touches the
network (L3). An ingestion-style JSON audit report is also written under ``data/cache/`` (§4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tmkg.signals.promotion import PromotionResult


@dataclass(frozen=True)
class RegistryEntry:
    """One row of the signal registry (mirrors l2/schema.sql signal_registry)."""
    signal_id: str
    hypothesis: str
    feature_family: str
    train_start: date | None
    train_end: date | None
    test_start: date | None
    test_end: date | None
    n_trials: int
    cost_model: str
    purge_embargo: str
    deflated_sharpe: float
    pbo: float
    beat_baselines: bool
    book: str
    promoted: bool
    knowledge_date: date

    def to_row(self) -> dict:
        return {
            "signal_id": self.signal_id, "hypothesis": self.hypothesis,
            "feature_family": self.feature_family,
            "train_start": self.train_start, "train_end": self.train_end,
            "test_start": self.test_start, "test_end": self.test_end,
            "n_trials": self.n_trials, "cost_model": self.cost_model,
            "purge_embargo": self.purge_embargo,
            "deflated_sharpe": self.deflated_sharpe, "pbo": self.pbo,
            "beat_baselines": self.beat_baselines, "book": self.book,
            "promoted": self.promoted, "knowledge_date": self.knowledge_date,
        }


def build_registry_entry(
    result: PromotionResult,
    *,
    signal_id: str,
    hypothesis: str,
    feature_family: str,
    knowledge_date: date,
    train_start: date | None = None,
    train_end: date | None = None,
    test_start: date | None = None,
    test_end: date | None = None,
    cost_model: str = "",
    purge_embargo: str = "",
) -> RegistryEntry:
    """Compose a registry row from a promotion verdict + the candidate's provenance metadata.
    The verdict's DSR (probability), PBO, beats-baselines flag, book, trial count and the
    promote/reject decision are carried straight through — the registry never re-judges."""
    return RegistryEntry(
        signal_id=signal_id, hypothesis=hypothesis, feature_family=feature_family,
        train_start=train_start, train_end=train_end,
        test_start=test_start, test_end=test_end,
        n_trials=result.n_trials, cost_model=cost_model, purge_embargo=purge_embargo,
        deflated_sharpe=float(result.dsr.dsr), pbo=float(result.pbo.pbo),
        beat_baselines=bool(result.beats_baselines), book=result.book,
        promoted=bool(result.promoted), knowledge_date=knowledge_date,
    )


def write_registry_entry(store, entry: RegistryEntry) -> None:
    """Land one registry row into L2 (bitemporal, append-only, PK = signal_id + knowledge_date).
    Idempotent: re-landing the same (signal_id, knowledge_date) is a no-op (L2Store ON CONFLICT
    DO NOTHING) — the registry is history, never overwritten."""
    store.bootstrap_schema()
    df = pd.DataFrame([entry.to_row()])
    # DATE columns must be real dates for DuckDB; pandas object/None is fine (NULL).
    for col in ("train_start", "train_end", "test_start", "test_end", "knowledge_date"):
        df[col] = pd.to_datetime(df[col]).dt.date.where(df[col].notna(), None)
    store.write_parquet("signal_registry", df)


def write_registry_report(entry: RegistryEntry, path: str | Path) -> Path:
    """Write the §4 JSON audit report for a registry write (matched/verdict/as-of)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = entry.to_row()
    row = {k: (str(v) if isinstance(v, date) else v) for k, v in row.items()}
    report = {
        "milestone": "M4", "artifact": "signal_registry",
        "signal_id": entry.signal_id, "promoted": entry.promoted,
        "knowledge_date": str(entry.knowledge_date), "row": row,
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
    return path

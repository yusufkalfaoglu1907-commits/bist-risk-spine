"""Ingestion adapter contract (CLAUDE.md §4, v1 KAP/GLEIF pattern extended).

The ingestion layer is the only place that may touch the network. Each external
source = one adapter with a smoke_check() drift guard; every run writes a JSON
audit report to data/cache/.
"""
from __future__ import annotations

import abc


class IngestionAdapter(abc.ABC):
    """Base for every external-source adapter. Subclasses must fail loudly on
    unreachable sources (raise SourceUnreachable) and never fabricate data."""

    source_name: str = "unknown"

    @abc.abstractmethod
    def fetch(self, **kw):
        """Pull from the upstream source and return parsed rows.

        On failure raise ``tmkg.pit.SourceUnreachable`` — NEVER return
        placeholder/interpolated/invented data (``FabricationGuard``).
        """

    @abc.abstractmethod
    def smoke_check(self) -> None:
        """Re-fetch a tiny known slice and assert it matches the committed golden
        sample under tests/golden/. Raise ``tmkg.pit.ContractDrift`` on mismatch."""

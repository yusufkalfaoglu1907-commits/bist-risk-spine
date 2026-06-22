"""Foreign-custody member-code reference — the resolved input for the foreign-flow factor.

WHY THIS EXISTS
---------------
The foreign-flow leg is the §5 BIST comovement driver: if non-resident flow is not
stripped, flow-driven comovement masquerades as residual supply-chain linkage. Building
it needs to know *which* MKK member codes represent non-resident holdings. There is **no
official "foreign broker" list** (Matriks support confirmed this) because foreign-ness on
BIST is a property of the **custody account**, not the broker: MKK segregates each
institution's holdings into separate member codes by account purpose — ``(YABANCI)`` =
non-resident/foreign custody, ``(PORTFOY SAKLAMA)`` = domestic custody. One institution
carries several codes at once (Garanti = GRM broker [domestic] + GPS portföy-saklama
[domestic] + OSM YABANCI [foreign]). See BUILD_LOG 2026-06-22 (Q1 resolution).

WHAT THIS MODULE IS (and is NOT)
--------------------------------
Like ``adapters/bist_isin_adapter`` it reads a COMMITTED, DATED reference file
(``data/reference/foreign_custody_codes.json``) rather than scraping a live surface — the
MKK member taxonomy is stable and the classification is a curated judgement, so it is
versioned on disk with its provenance, validated on load, and never silently re-derived.

It is pure data access: no network, no L2, no PIT. The custody-series **ingestion** (the
network hop that nets the YABANCI positions into the L2 ``FFLOW`` series) consumes
``foreign_custody_codes()`` from here; that ingestion is the live-session piece and is not
in this module.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tmkg import config

# Bump when the reference-file schema or validation logic changes.
REFERENCE_SCHEMA_VERSION = 1

DEFAULT_REFERENCE_PATH = (
    config.REPO_ROOT / "data" / "reference" / "foreign_custody_codes.json")

# An MKK member code: 3 uppercase alphanumerics (e.g. CIY, OSM, AE1).
_CODE_RE = re.compile(r"^[A-Z0-9]{3}$")


@dataclass(frozen=True)
class ForeignCustodyReference:
    """The parsed, validated foreign-custody reference.

    ``custody_codes`` — the authoritative ``(YABANCI)`` non-resident custody members; the
    foreign leg of the **custody-based** foreign-flow factor (deep history, >=2011).
    ``execution_brokers`` — curated global-IB execution brokers for the broker-netting
    overlay (recent-only, ~2025+). ``domestic_exclusions`` — broker codes that MUST stay
    domestic despite a foreign parent (the GARANTI-BBVA rule). All maps are code -> name.
    """

    custody_codes: dict[str, str]
    execution_brokers: dict[str, str]
    domestic_exclusions: dict[str, str]
    source: str
    fetched_iso: str


def _validate_codes(codes: dict[str, str], *, where: str) -> None:
    for code in codes:
        if not _CODE_RE.match(code):
            raise ValueError(f"{where}: malformed member code {code!r} (want 3 uppercase alnum)")


def load(path: Path = DEFAULT_REFERENCE_PATH) -> ForeignCustodyReference:
    """Read and validate the committed reference file.

    Validation (reject rather than return a half-trusted reference): schema version match;
    every code well-shaped; at least one custody code; and the custody set is **disjoint**
    from both the execution-broker set and the domestic-exclusion set — a code that landed
    in two buckets would silently double-count or mis-sign foreign flow.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != REFERENCE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {raw.get('schema_version')!r} "
            f"!= expected {REFERENCE_SCHEMA_VERSION}")

    custody = dict(raw["foreign_custody_members"]["codes"])
    execution = dict(raw["foreign_execution_brokers"]["codes"])
    domestic = dict(raw["domestic_despite_foreign_parent"]["codes"])

    for codes, where in (
        (custody, "foreign_custody_members"),
        (execution, "foreign_execution_brokers"),
        (domestic, "domestic_despite_foreign_parent"),
    ):
        _validate_codes(codes, where=where)

    if not custody:
        raise ValueError(f"{path}: no foreign-custody codes — the foreign leg would be empty")

    custody_set = set(custody)
    if custody_set & set(execution):
        raise ValueError(
            f"{path}: codes in BOTH custody and execution buckets: "
            f"{sorted(custody_set & set(execution))}")
    if custody_set & set(domestic):
        raise ValueError(
            f"{path}: codes in BOTH custody and domestic-exclusion buckets: "
            f"{sorted(custody_set & set(domestic))}")

    return ForeignCustodyReference(
        custody_codes=custody,
        execution_brokers=execution,
        domestic_exclusions=domestic,
        source=raw["source"],
        fetched_iso=raw["fetched_iso"],
    )


def foreign_custody_codes(ref: ForeignCustodyReference | None = None) -> frozenset[str]:
    """The authoritative ``(YABANCI)`` non-resident custody member codes — the foreign leg
    of the custody-based foreign-flow factor. Loads the default reference if none passed."""
    ref = ref or load()
    return frozenset(ref.custody_codes)


def foreign_execution_brokers(ref: ForeignCustodyReference | None = None) -> frozenset[str]:
    """Curated global-IB execution broker codes for the broker-netting overlay (~2025+)."""
    ref = ref or load()
    return frozenset(ref.execution_brokers)


def domestic_exclusions(ref: ForeignCustodyReference | None = None) -> frozenset[str]:
    """Broker codes that stay domestic despite a foreign parent (the GARANTI-BBVA rule)."""
    ref = ref or load()
    return frozenset(ref.domestic_exclusions)

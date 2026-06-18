"""KAP sector-classification reference adapter.

WHY THIS EXISTS
---------------
KAP seeds Company identity but exposes no sector field; the live graph therefore
ships with zero Sector nodes / IN_SECTOR edges. KAP's "Sektörler" listing is the
authoritative classification: a two-level taxonomy (main sector -> sub-sector)
where each listed company sits in exactly one leaf sub-sector and, by roll-up,
its parent main sector.

WHAT THIS ADAPTER IS (and is NOT)
---------------------------------
Mirrors `bist_isin_adapter`: it does NOT scrape KAP on every run. It reads a
COMMITTED, DATED reference file (`data/reference/sectors.json`) produced by
`scripts/import_sectors.py` from a KAP Sektörler export — versioned on disk,
carrying its `source`/`fetched_iso`, and validated on load so a broken taxonomy
is REJECTED rather than written to the graph.

The file carries two things: the sector tree (`sectors`: code/name/level/parent)
and `memberships` (ticker -> leaf sector code, including legacy/secondary codes
so any ticker variant the graph holds still resolves).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tmkg import config

# Bump when the reference-file schema or validation logic changes.
REFERENCE_SCHEMA_VERSION = 1

DEFAULT_REFERENCE_PATH = config.REPO_ROOT / "data" / "reference" / "sectors.json"


@dataclass
class Sector:
    code: str
    name: str
    level: int            # 1 = main sector, 2 = sub-sector (leaf)
    parent: str | None    # parent sector code (None for main sectors)


@dataclass
class SectorLookup:
    ticker: str
    leaf: str | None       # leaf (sub-sector) code
    main: str | None       # parent main-sector code (roll-up)
    found: bool
    note: str = ""


class SectorAdapter:
    """Sector tree + ticker->sector lookups from a committed reference file."""

    def __init__(self, reference_path: Path | str | None = None) -> None:
        self.reference_path = Path(reference_path or DEFAULT_REFERENCE_PATH)
        self._source: str | None = None
        self._fetched_iso: str | None = None
        self._sectors: dict[str, Sector] = {}       # code -> Sector
        self._memberships: dict[str, str] = {}      # TICKER -> leaf code
        self._loaded = False

    def __enter__(self) -> "SectorAdapter":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        return None

    # --- loading -----------------------------------------------------------

    def load(self, strict: bool = False) -> "SectorAdapter":
        """Read + validate the reference file. Missing file => empty (the loader
        finds nothing to write). ``strict`` raises on any structural defect."""
        self._sectors, self._memberships = {}, {}
        if not self.reference_path.exists():
            self._loaded = True
            return self
        blob = json.loads(self.reference_path.read_text(encoding="utf-8"))
        self._source = blob.get("source")
        self._fetched_iso = blob.get("fetched_iso")
        for s in blob.get("sectors") or []:
            sec = Sector(code=s["code"], name=s.get("name", ""),
                         level=int(s.get("level", 1)), parent=s.get("parent"))
            self._sectors[sec.code] = sec
        for ticker, leaf in (blob.get("memberships") or {}).items():
            self._memberships[ticker.strip().upper()] = leaf
        self._loaded = True
        if strict:
            self._validate()
        return self

    def _validate(self) -> None:
        """Structural integrity: parent refs resolve, levels are consistent,
        every membership points at a known leaf (level-2) sector."""
        for code, sec in self._sectors.items():
            if sec.parent is not None and sec.parent not in self._sectors:
                raise ValueError(f"sector {code!r} has unknown parent {sec.parent!r}")
            if sec.level == 2 and sec.parent is None:
                raise ValueError(f"sub-sector {code!r} has no parent")
            if sec.level == 1 and sec.parent is not None:
                raise ValueError(f"main sector {code!r} unexpectedly has a parent")
        for ticker, leaf in self._memberships.items():
            if leaf not in self._sectors:
                raise ValueError(f"{ticker} -> unknown sector {leaf!r}")
            if self._sectors[leaf].level != 2:
                raise ValueError(f"{ticker} -> {leaf!r} is not a leaf sub-sector")

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    # --- lookup ------------------------------------------------------------

    def lookup(self, ticker: str | None) -> SectorLookup:
        self._ensure()
        t = (ticker or "").strip().upper()
        if not t:
            return SectorLookup(t, None, None, False, note="empty-ticker")
        leaf = self._memberships.get(t)
        if leaf is None:
            return SectorLookup(t, None, None, False, note="not-in-reference")
        return SectorLookup(t, leaf, self.main_of(leaf), True)

    def main_of(self, code: str | None) -> str | None:
        """Roll a leaf code up to its main (level-1) sector code."""
        self._ensure()
        sec = self._sectors.get(code or "")
        if sec is None:
            return None
        return sec.parent if sec.level == 2 else sec.code

    def sectors(self) -> list[Sector]:
        self._ensure()
        return list(self._sectors.values())

    def get(self, code: str) -> Sector | None:
        self._ensure()
        return self._sectors.get(code)

    def memberships(self) -> dict[str, str]:
        self._ensure()
        return dict(self._memberships)

    def __len__(self) -> int:
        self._ensure()
        return len(self._sectors)

    @property
    def source(self) -> str | None:
        self._ensure()
        return self._source

    @property
    def fetched_iso(self) -> str | None:
        self._ensure()
        return self._fetched_iso

    # --- refresh (documented manual path) ----------------------------------

    def refresh(self) -> None:
        """The reference file is a dated KAP export, not a live feed. Refresh it
        by re-exporting KAP's Sektörler listing and running
        ``scripts/import_sectors.py`` — see ``data/reference/README.md``."""
        raise NotImplementedError(
            "Sector taxonomy is a committed export. Re-run "
            "scripts/import_sectors.py on a fresh KAP Sektörler export to refresh "
            f"{self.reference_path}."
        )

    # --- drift / data-quality guard ----------------------------------------

    def smoke_check(self, require_anchors: bool = False) -> dict:
        """Validate the reference file's integrity (raises on a structural
        defect). With ``require_anchors=True`` also assert a few independently
        known ticker->main-sector pairs, guarding against a wrong/corrupt import.
        Anchors are checked only when present so an unpopulated file still passes
        structural validation."""
        self.load(strict=True)
        anchors = {            # ticker -> expected MAIN sector code
            "GARAN": "MALI_KURULUSLAR",
            "AKBNK": "MALI_KURULUSLAR",
            "THYAO": "ULASTIRMA_VE_DEPOLAMA",
            "TUPRS": "IMALAT",
            "KCHOL": "MALI_KURULUSLAR",
        }
        checked = {}
        for tk, want in anchors.items():
            lk = self.lookup(tk)
            if lk.found:
                assert lk.main == want, f"anchor drift: {tk} -> {lk.main}, expected {want}"
                checked[tk] = lk.main
            elif require_anchors:
                raise AssertionError(f"anchor {tk} missing from reference file")
        n_main = sum(1 for s in self._sectors.values() if s.level == 1)
        n_sub = sum(1 for s in self._sectors.values() if s.level == 2)
        return {"sectors": len(self._sectors), "main": n_main, "sub": n_sub,
                "memberships": len(self._memberships), "source": self._source,
                "fetched_iso": self._fetched_iso, "anchors_checked": checked}

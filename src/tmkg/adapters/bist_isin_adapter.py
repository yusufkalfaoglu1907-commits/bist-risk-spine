"""BİST/MKK ticker→ISIN reference adapter.

WHY THIS EXISTS
---------------
The GLEIF back-fill (`gleif_adapter`) resolves most listed companies' equity
ISINs straight from the LEI's instrument list. It deliberately REFUSES two
cases rather than guess (see `gleif_adapter.pick_equity_isin`):

  - ``ambiguous-multi-equity`` — the LEI maps to several TRA/TRE share lines and
    GLEIF exposes no instrument-type field to pick the listed common share
    (common for GYOs / holdings with multiple share groups), and
  - ``no-equity-class`` — GLEIF lists only debt/rights/warrants for that LEI.

Those refusals are the residual "ISIN gap". Closing it needs an AUTHORITATIVE
ticker→ISIN map — i.e. the BİST/MKK side of the house, where each listed line is
keyed by its exchange ticker rather than inferred from instrument codes.

WHAT THIS ADAPTER IS (and is NOT)
---------------------------------
There is NO public, automatable bulk ticker→ISIN feed from KAP, Borsa İstanbul,
or MKK (verified 2026-06-06: KAP's own company export carries ticker/title/
city/auditor but NO ISIN; MKK's ISIN registry is login-gated; İş Yatırım's data
endpoints return 401). So this adapter does NOT scrape a live endpoint on every
run — that would be both impossible against those surfaces and contrary to the
project's stability stance.

Instead it reads a COMMITTED, DATED reference file
(`data/reference/bist_isin.json`) — exactly how a provenance-first project should
treat an authoritative reference list: versioned on disk, carrying its source and
fetch date, auditable, and never silently re-derived. Populate / refresh that
file from an authoritative export (see `data/reference/README` and `refresh()`).

Every value is validated (Turkish ISIN shape + ISO 6166 check digit) on load, so
a malformed code is REJECTED rather than written to the graph — consistent with
the GLEIF adapter's "never write a guessed/garbage ISIN" rule.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tmkg import config

# Bump when the reference-file schema or validation logic changes.
REFERENCE_SCHEMA_VERSION = 1

DEFAULT_REFERENCE_PATH = config.REPO_ROOT / "data" / "reference" / "bist_isin.json"
# Human-verified resolution of the ambiguous multi-equity tickers (F9 / Phase
# 2.3). Only rows with confirmed=true and a valid candidate ISIN are honored.
DEFAULT_DISAMBIGUATION_PATH = (
    config.REPO_ROOT / "data" / "reference" / "isin_disambiguation.json")

# Turkish ISIN: country prefix TR, a 1-char instrument-class letter, then 9
# alphanumerics (the last is the ISO 6166 check digit). Equity classes are
# TRA (older, full-ticker format) and TRE (newer, abbreviated format).
_ISIN_RE = re.compile(r"^TR[A-Z][0-9A-Z]{8}[0-9]$")
# Generic ISO 6166: 2-letter country code, 9 alphanumerics, numeric check digit.
# Used for non-TR instruments (e.g. XS-prefixed Eurobonds in the debt stage).
_ISIN_ANY_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")
_EQUITY_PREFIXES = ("TRA", "TRE")


def isin_check_digit_ok(isin: str) -> bool:
    """Validate the ISO 6166 (ISIN) check digit via the Luhn-on-digits rule.

    Each letter is expanded to its two-digit ordinal (A=10 ... Z=35), the whole
    string becomes a digit run, and a Luhn mod-10 check must yield 0. This is
    what makes the adapter able to *reject* a transcription error instead of
    propagating it into the price join.
    """
    if not isin or len(isin) != 12:
        return False
    digits = []
    for ch in isin:
        if ch.isdigit():
            digits.append(int(ch))
        elif ch.isalpha():
            v = ord(ch.upper()) - 55  # 'A' -> 10
            digits.append(v // 10)
            digits.append(v % 10)
        else:
            return False
    # Luhn from the right: double every second digit.
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_isin(isin: str | None) -> bool:
    return bool(isin) and bool(_ISIN_RE.match(isin)) and isin_check_digit_ok(isin)


def is_valid_isin_any(isin: str | None) -> bool:
    """Validate ANY-country ISIN (ISO 6166 shape + check digit), not just TR.

    The TR-only ``is_valid_isin`` stays the gate for the equity reference; the
    debt stage also ingests XS-prefixed Eurobonds, which this accepts. The
    check-digit rule is country-agnostic, so we only relax the shape regex."""
    return bool(isin) and bool(_ISIN_ANY_RE.match(isin)) and isin_check_digit_ok(isin)


def is_equity_isin(isin: str | None) -> bool:
    return is_valid_isin(isin) and isin[:3] in _EQUITY_PREFIXES


@dataclass
class BistIsinResult:
    """One ticker lookup against the BİST/MKK reference map."""
    ticker: str
    isin: str | None
    found: bool
    valid: bool            # passed shape + check-digit validation
    is_equity: bool        # TRA/TRE equity class
    source: str | None     # provenance string from the reference file
    note: str = ""


class BistIsinAdapter:
    """Authoritative ticker→ISIN lookups from a committed reference file.

    Construct directly or inside a ``with`` block (no resources held; the
    context-manager form just mirrors the other adapters for symmetry).
    """

    def __init__(self, reference_path: Path | str | None = None,
                 disambiguation_path: Path | str | None = None) -> None:
        self.reference_path = Path(reference_path or DEFAULT_REFERENCE_PATH)
        self.disambiguation_path = Path(
            disambiguation_path or DEFAULT_DISAMBIGUATION_PATH)
        self._source: str | None = None
        self._fetched_iso: str | None = None
        self._map: dict[str, str] = {}      # TICKER -> ISIN (validated)
        self._rejected: dict[str, str] = {}  # TICKER -> raw ISIN that failed validation
        self._ambiguous: dict[str, list[str]] = {}  # TICKER -> equity ISIN candidates
        # TICKER -> human-confirmed ISIN (resolves an ambiguous multi-equity line)
        self._disambiguation: dict[str, str] = {}
        self._disambiguation_skipped: dict[str, str] = {}  # TICKER -> why not honored
        self._loaded = False

    def __enter__(self) -> "BistIsinAdapter":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        return None

    # --- loading -----------------------------------------------------------

    def load(self, strict: bool = False) -> "BistIsinAdapter":
        """Read + validate the reference file. Missing file => empty map (the
        loader will simply find nothing to fill). ``strict`` raises on a malformed
        or check-digit-failing entry instead of quarantining it."""
        self._map, self._rejected, self._ambiguous = {}, {}, {}
        if not self.reference_path.exists():
            self._loaded = True
            return self
        blob = json.loads(self.reference_path.read_text(encoding="utf-8"))
        self._source = blob.get("source")
        self._fetched_iso = blob.get("fetched_iso")
        for ticker, isin in (blob.get("mappings") or {}).items():
            t = ticker.strip().upper()
            code = (isin or "").strip().upper()
            if is_valid_isin(code):
                self._map[t] = code
            else:
                self._rejected[t] = code
                if strict:
                    raise ValueError(f"invalid ISIN for {t!r}: {code!r}")
        for ticker, isins in (blob.get("ambiguous") or {}).items():
            self._ambiguous[ticker.strip().upper()] = [
                (i or "").strip().upper() for i in isins]
        self._load_disambiguation(strict)
        self._loaded = True
        return self

    def _load_disambiguation(self, strict: bool = False) -> None:
        """Read the human-verified ambiguous-ticker resolutions (F9 / 2.3).

        Honors a row ONLY when confirmed=true AND `chosen` is a valid equity ISIN
        that is one of the row's own candidates — so an unconfirmed, malformed, or
        off-list pick is never written. Skipped rows are recorded with a reason
        for the loader's audit report. Missing file => no disambiguations."""
        self._disambiguation, self._disambiguation_skipped = {}, {}
        if not self.disambiguation_path.exists():
            return
        blob = json.loads(self.disambiguation_path.read_text(encoding="utf-8"))
        for ticker, row in (blob.get("disambiguations") or {}).items():
            t = ticker.strip().upper()
            chosen = (row.get("chosen") or "").strip().upper()
            cands = [(c or "").strip().upper() for c in (row.get("candidates") or [])]
            if not row.get("confirmed"):
                self._disambiguation_skipped[t] = "unconfirmed"
                continue
            if not is_equity_isin(chosen):
                self._disambiguation_skipped[t] = "chosen-not-valid-equity-isin"
                if strict:
                    raise ValueError(
                        f"disambiguation {t!r}: chosen {chosen!r} is not a valid equity ISIN")
                continue
            if chosen not in cands:
                self._disambiguation_skipped[t] = "chosen-not-a-candidate"
                if strict:
                    raise ValueError(
                        f"disambiguation {t!r}: chosen {chosen!r} not among candidates {cands}")
                continue
            self._disambiguation[t] = chosen

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    # --- lookup ------------------------------------------------------------

    def lookup(self, ticker: str | None) -> BistIsinResult:
        self._ensure()
        t = (ticker or "").strip().upper()
        if not t:
            return BistIsinResult(t, None, False, False, False, self._source,
                                  note="empty-ticker")
        if t in self._map:
            isin = self._map[t]
            return BistIsinResult(t, isin, True, True, is_equity_isin(isin),
                                  self._source)
        if t in self._rejected:
            return BistIsinResult(t, self._rejected[t], True, False, False,
                                  self._source, note="failed-validation")
        return BistIsinResult(t, None, False, False, False, self._source,
                              note="not-in-reference")

    def is_equity_traded(self, ticker: str | None) -> bool:
        """Whether the BİST/MKK reference knows this ticker as a traded equity —
        either a resolved single ISIN or a multi-group (ambiguous) equity line.
        Used to classify Company.listing_status; debt-only KAP issuers
        (factoring/leasing/SPVs) are absent from both and classify as
        NON_EQUITY_ISSUER."""
        self._ensure()
        t = (ticker or "").strip().upper()
        return bool(t) and (t in self._map or t in self._ambiguous)

    def disambiguated(self, ticker: str | None) -> str | None:
        """The human-confirmed equity ISIN for an ambiguous ticker, or None.

        None means either no disambiguation row, or a row that is unconfirmed /
        invalid (see `disambiguation_skipped`). This is the only path by which an
        ambiguous multi-equity ticker gets a resolved ISIN."""
        self._ensure()
        return self._disambiguation.get((ticker or "").strip().upper())

    @property
    def disambiguation_skipped(self) -> dict[str, str]:
        """Ambiguous tickers present in the disambiguation file but NOT honored,
        with the reason (unconfirmed / chosen-not-a-candidate / ...)."""
        self._ensure()
        return dict(self._disambiguation_skipped)

    def tickers(self) -> list[str]:
        self._ensure()
        return sorted(self._map)

    def ambiguous_tickers(self) -> list[str]:
        self._ensure()
        return sorted(self._ambiguous)

    def __len__(self) -> int:
        self._ensure()
        return len(self._map)

    @property
    def source(self) -> str | None:
        self._ensure()
        return self._source

    @property
    def rejected(self) -> dict[str, str]:
        """Tickers whose reference ISIN failed validation (quarantined, never
        written). Surface these for data-quality review."""
        self._ensure()
        return dict(self._rejected)

    # --- refresh (documented manual path) ----------------------------------

    def refresh(self) -> None:
        """No automatable live BİST/MKK bulk ISIN feed exists (see module
        docstring). Refresh the reference file from an authoritative export:

          1. Official route — export the listed-equity ISIN list from the MKK
             ISIN registry or a Borsa İstanbul equity-list file, then run
             ``scripts/import_bist_isin.py <file>`` to normalize it into
             ``data/reference/bist_isin.json`` (validates every code on import).
          2. Rendered route — drive a logged-in source that displays ISIN per
             ticker via Claude in Chrome and write the same JSON.

        This method intentionally raises so callers don't mistake a stale file
        for a live refresh."""
        raise NotImplementedError(
            "No automatable BİST/MKK bulk ISIN endpoint. Populate "
            f"{self.reference_path} via scripts/import_bist_isin.py — see "
            "data/reference/README.md."
        )

    # --- drift / data-quality guard ----------------------------------------

    def smoke_check(self, require_anchors: bool = False) -> dict:
        """Validate the reference file's integrity. Raises on a malformed entry.

        With ``require_anchors=True`` also assert that a few independently
        verified ticker→ISIN pairs resolve correctly — a guard against a
        corrupted/ wrong-column import. Anchors are only checked when present so
        an as-yet-unpopulated file still passes structural validation."""
        self.load(strict=True)  # raises on any invalid ISIN in the file
        anchors = {
            "TUPRS": "TRATUPRS91E8",
            "THYAO": "TRATHYAO91M5",
            "GARAN": "TRAGARAN91N1",
            "ACSEL": "TREACSS00017",
        }
        checked = {}
        for tk, want in anchors.items():
            got = self._map.get(tk)
            if got is not None:
                assert got == want, f"anchor drift: {tk} -> {got}, expected {want}"
                checked[tk] = got
            elif require_anchors:
                raise AssertionError(f"anchor {tk} missing from reference file")
        return {"tickers": len(self._map), "rejected": len(self._rejected),
                "source": self._source, "fetched_iso": self._fetched_iso,
                "anchors_checked": checked}

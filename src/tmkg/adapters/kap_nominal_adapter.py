"""KAP nominal (face-value) adapter — turns issuance disclosures into money.

The graph's debt `Security` nodes carry maturity + class but no *amount*, so the
blast-radius analytics could only count instruments. This adapter closes that
gap by harvesting the **issued nominal** from KAP disclosure detail pages and
exposing it keyed on ISIN, so a loader can MERGE it onto the matching Security.

WHERE THE NUMBER COMES FROM
---------------------------
KAP renders an issuance / "gerçekleşen ihraç" disclosure (e.g.
https://www.kap.org.tr/tr/Bildirim/<index>) as a GWT page whose field values,
once tags are stripped, sit in document order as

        ... "ISIN Kodu" <ISIN> "Nominal Değer (TL)" <amount> "Vade ..." ...

i.e. each ISIN value is immediately followed by its nominal value. A single
bulletin can list many instruments; each ISIN still pairs with exactly one
amount.

SAFETY GATE (never guess — project ethos)
-----------------------------------------
`extract_nominals` emits a pair ONLY when an ISIN maps to a *single distinct*
amount on the page. If the same ISIN shows conflicting amounts, or an amount
can't be parsed, the ISIN is reported under `ambiguous`/`rejected` and NOT
emitted. The "Nominal Değer (TL)" label gates currency to TRY; pages without it
yield nothing (FX eurobond nominals live in differently-labelled disclosures and
are deliberately out of scope for v1).

CAVEAT — issued, not outstanding.
The nominal is the amount *issued*; amortising/partly-redeemed instruments read
high. Reconcile against issuer financial-statement maturity tables when that
matters. Confidence is tagged (default 0.9) so a stricter consumer can filter.
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

from tmkg import config
from tmkg.adapters.bist_isin_adapter import is_valid_isin_any

DEFAULT_NOMINAL_REFERENCE_PATH = (
    config.REPO_ROOT / "data" / "reference" / "kap_nominal.json"
)
NOMINAL_SCHEMA_VERSION = 1
DEFAULT_CONFIDENCE = 0.9

# A Turkish-formatted amount: 1.400.000.000 or 1.400.000.000,00 (>=1 group).
_TR_AMOUNT = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?"
_ISIN = r"TR[A-Z0-9]{10}"
# ISIN immediately followed (whitespace only) by a Turkish amount.
_ADJ = re.compile(rf"({_ISIN})\s+({_TR_AMOUNT})")
_NOMINAL_TL_LABEL = "Nominal De"  # "Nominal Değer (TL)" — encoding-robust prefix


def parse_tr_amount(text: str) -> float | None:
    """'1.400.000.000,50' -> 1400000000.5 . Returns None if unparseable."""
    if text is None:
        return None
    s = str(text).strip()
    if not re.fullmatch(_TR_AMOUNT, s):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _detag(raw_html: str) -> str:
    """Decode KAP's escaped GWT payload, strip tags, collapse whitespace."""
    # KAP embeds the template as JS-escaped HTML (< == '<').
    u = raw_html.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
    u = re.sub(r"<[^>]+>", " ", u)
    u = _html.unescape(u)
    return re.sub(r"[ \t\r\n]+", " ", u)


@dataclass
class NominalRecord:
    isin: str
    nominal: float
    currency: str
    source: str           # disclosure index / provenance string
    as_of: str | None     # ISO date the figure was published / fetched
    confidence: float
    extraction_method: str = "kap-adjacency"


def extract_nominals(
    raw_html: str,
    source: str,
    as_of: str | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> dict:
    """Parse one disclosure detail page into confident (ISIN -> nominal) records.

    Returns {"records": [NominalRecord...], "ambiguous": [{isin, amounts}...]}.
    Only TRY nominals (pages carrying the 'Nominal Değer (TL)' label) are emitted.
    """
    text = _detag(raw_html)
    if _NOMINAL_TL_LABEL not in text:
        return {"records": [], "ambiguous": []}  # not a TL nominal disclosure

    by_isin: dict[str, set[float]] = {}
    for isin, amt in _ADJ.findall(text):
        isin = isin.upper()
        if not is_valid_isin_any(isin):
            continue
        val = parse_tr_amount(amt)
        if val is None or val <= 0:
            continue
        by_isin.setdefault(isin, set()).add(val)

    records, ambiguous = [], []
    for isin, amounts in by_isin.items():
        if len(amounts) == 1:
            records.append(NominalRecord(
                isin=isin, nominal=next(iter(amounts)), currency="TRY",
                source=source, as_of=as_of, confidence=confidence,
            ))
        else:
            ambiguous.append({"isin": isin, "amounts": sorted(amounts)})
    return {"records": records, "ambiguous": ambiguous}


def write_nominal_reference(
    records: list[NominalRecord],
    out_path: Path | str,
    source: str,
    complete: bool = False,
) -> Path:
    """Serialise harvested nominals to the committed reference file (ISIN-keyed,
    last write wins on duplicate ISIN keeping the higher confidence)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    dedup: dict[str, NominalRecord] = {}
    for r in records:
        prev = dedup.get(r.isin)
        if prev is None or r.confidence >= prev.confidence:
            dedup[r.isin] = r
    out.write_text(json.dumps({
        "source": source,
        "fetched_iso": date.today().isoformat(),
        "method": "kap-detail-extract",
        "schema_version": NOMINAL_SCHEMA_VERSION,
        "complete": complete,
        "nominals": [asdict(dedup[k]) for k in sorted(dedup)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


class KapNominalReference:
    """Loads + validates the committed nominal reference file.

    Mirrors ``MkkDebtReference``: missing file → empty (loader finds nothing);
    every record re-validated on load (valid ISIN, positive numeric nominal) so a
    corrupted entry is quarantined, not written to the graph.
    """

    def __init__(self, reference_path: Path | str | None = None) -> None:
        self.reference_path = Path(reference_path or DEFAULT_NOMINAL_REFERENCE_PATH)
        self._records: list[NominalRecord] = []
        self._rejected: list[dict] = []
        self._source: str | None = None
        self._loaded = False

    def __enter__(self) -> "KapNominalReference":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        return None

    def load(self, strict: bool = False) -> "KapNominalReference":
        self._records, self._rejected = [], []
        if not self.reference_path.exists():
            self._loaded = True
            return self
        blob = json.loads(self.reference_path.read_text(encoding="utf-8"))
        self._source = blob.get("source")
        for rec in (blob.get("nominals") or []):
            isin = (rec.get("isin") or "").strip().upper()
            nominal = rec.get("nominal")
            if not is_valid_isin_any(isin) or not isinstance(nominal, (int, float)) or nominal <= 0:
                self._rejected.append({"isin": isin, "nominal": nominal})
                if strict:
                    raise ValueError(f"invalid nominal record: {isin!r}={nominal!r}")
                continue
            self._records.append(NominalRecord(
                isin=isin, nominal=float(nominal),
                currency=(rec.get("currency") or "TRY"),
                source=rec.get("source") or self._source or "kap",
                as_of=rec.get("as_of"),
                confidence=float(rec.get("confidence") or DEFAULT_CONFIDENCE),
                extraction_method=rec.get("extraction_method") or "kap-adjacency",
            ))
        self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def all(self) -> list[NominalRecord]:
        self._ensure()
        return list(self._records)

    def rejected(self) -> list[dict]:
        self._ensure()
        return list(self._rejected)

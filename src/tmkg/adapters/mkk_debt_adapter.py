"""MKK corporate-debt instrument adapter (Phase-2 debt stage).

WHY THIS EXISTS
---------------
The MKK "Menkul Kıymetler Listesi" already used for the equity ticker→ISIN map
(`bist_isin_adapter`) is a SUPERSET: alongside the equity lines it registers the
issuers' debt — corporate bonds, financing bills, and lease certificates
(sukuk), plus XS-prefixed Eurobonds. Modeling that debt turns the graph from
"who owns whom" into "who owes what, due when" — the substrate for
refinancing-wall and FX-debt contagion analysis named in the project's purpose.

WHAT THIS ADAPTER DOES (and does NOT)
-------------------------------------
Same provenance-first stance as the equity side. It does NOT scrape a live
endpoint (no automatable bulk MKK feed exists — see `bist_isin_adapter`). It
EXTRACTS debt instruments from the authoritative MKK export into a committed,
dated reference file (`data/reference/mkk_debt.json`) via
``scripts/import_mkk_debt.py``, and reads that file at load time.

Every value is validated and confidence-tagged, never guessed:
  - ISIN: ISO 6166 shape + check digit (``is_valid_isin_any`` — TR *and* XS).
    Malformed codes are quarantined, never written.
  - instrument class → Security.type is a deterministic lookup on the ISIN's
    class char (TR) or country prefix (XS).
  - maturity date is INFERRED from the free-text description (DDMMYYYY) and
    carries a confidence + method so a low-confidence parse can be filtered or
    reviewed — it is never silently asserted as fact.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

from tmkg import config
from tmkg.adapters.bist_isin_adapter import is_valid_isin_any

# Bump when the reference-file schema or extraction logic changes.
DEBT_SCHEMA_VERSION = 1

DEFAULT_DEBT_REFERENCE_PATH = config.REPO_ROOT / "data" / "reference" / "mkk_debt.json"

# Turkish ISIN class char (3rd letter) → (instrument_class label, Security.type).
# S = özel sektör tahvili (corporate bond); F = finansman bonosu (financing
# bill); D = kira sertifikası (lease certificate / sukuk).
_TR_DEBT_CLASS = {
    "S": ("TRS", "BOND"),
    "F": ("TRF", "FINANCING_BILL"),
    "D": ("TRD", "SUKUK"),
}
# All debt classes this stage understands. XS = foreign (Eurobond).
DEFAULT_DEBT_CLASSES = ("TRS", "TRF", "TRD", "XS")

# Plausible maturity-year window for a DDMMYYYY date embedded in a description.
_MIN_YEAR, _MAX_YEAR = 1990, 2100
_EIGHT_DIGITS = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def classify_instrument(isin: str | None) -> tuple[str | None, str | None]:
    """Map an ISIN to (instrument_class, Security.type) for the debt stage.

    Returns (None, None) for anything that is not a recognised debt class
    (e.g. equity TRA/TRE, rights TRR, warrants TRW, ELÜS TRX, foreign certs)."""
    if not isin or len(isin) < 3:
        return (None, None)
    if isin.startswith("TR"):
        return _TR_DEBT_CLASS.get(isin[2], (None, None))
    if isin.startswith("XS"):
        return ("XS", "EUROBOND")
    return (None, None)


def parse_maturity(description: str | None) -> tuple[str | None, float, str]:
    """Infer an ISO maturity date from an MKK description's embedded DDMMYYYY run.

    Returns (iso_date | None, confidence, method). MKK debt descriptions embed
    the redemption date as 8 digits, e.g. "…ÖZEL SEKTÖR TAHVİLİ 25052027…" →
    2027-05-25. A description can also embed the ISIN or an issue date, so:
      - exactly one VALID DDMMYYYY in the plausible-year window → conf 0.9;
      - several distinct valid dates → the LATEST is taken as the maturity
        (issue/coupon dates precede it) but flagged conf 0.5 for review;
      - none → (None, 0.0, "none").
    Confidence is never 1.0: this is inferred, not a structured field.
    """
    if not description:
        return (None, 0.0, "none")
    found: set[date] = set()
    for m in _EIGHT_DIGITS.findall(description):
        dd, mm, yyyy = int(m[0:2]), int(m[2:4]), int(m[4:8])
        if not (_MIN_YEAR <= yyyy <= _MAX_YEAR and 1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        try:
            found.add(date(yyyy, mm, dd))
        except ValueError:
            continue
    if not found:
        return (None, 0.0, "none")
    if len(found) == 1:
        return (next(iter(found)).isoformat(), 0.9, "ddmmyyyy-single")
    return (max(found).isoformat(), 0.5, "ddmmyyyy-multi")


def parse_currency(description: str | None, instrument_class: str | None) -> str | None:
    """Best-effort currency from the description; default TRY for TR-class debt.

    XS Eurobonds get the marker ``"FX"`` rather than a specific code. The MKK
    export carries no currency column and its XS descriptions name no currency
    (verified: 0 of 836 XS lines contain a USD/EUR/DOLAR/AVRO token — they read
    only "<issuer> YURTDIŞI TAHVİLİ <maturity>"). So the *specific* currency is
    genuinely unrecoverable from this source, but the *fact* that the paper is
    foreign-currency-denominated (overwhelmingly USD or EUR) is certain. "FX"
    records exactly that: a known foreign-currency instrument whose USD-vs-EUR
    split awaits a source that carries it (KAP FX issuance bulletins, Phase 3).
    It deliberately does NOT collapse into "UNKNOWN" — an FX wall and a genuinely
    unknown-currency wall are different facts and must report differently.

    The explicit-token path below still wins when a description DOES name a
    currency, so a future source that carries one resolves USD/EUR directly."""
    text = (description or "").upper()
    # Word-boundary matches so "EUR" does NOT fire inside "EUROBOND".
    if re.search(r"\bUSD\b", text) or "DOLAR" in text:
        return "USD"
    if re.search(r"\bEUR\b", text) or "AVRO" in text:
        return "EUR"
    if "TÜRK LİRASI" in text or "TURK LIRASI" in text or re.search(r"\bTL\b", text):
        return "TRY"
    if instrument_class in ("TRS", "TRF", "TRD"):
        return "TRY"
    if instrument_class == "XS":
        return "FX"   # foreign-currency (USD/EUR), specific code unresolved here
    return None


@dataclass
class DebtSecurity:
    """One extracted debt instrument, fully validated + confidence-tagged."""
    isin: str
    instrument_class: str          # TRS / TRF / TRD / XS
    type: str                      # BOND / FINANCING_BILL / SUKUK / EUROBOND
    issuer_name: str               # MKKÇ Adı (short issuer name) — for matching
    description: str
    currency: str | None
    maturity_date: str | None      # ISO yyyy-mm-dd, inferred
    maturity_confidence: float
    maturity_method: str


def extract_debt(
    rows: list[dict],
    isin_col: str,
    desc_col: str,
    issuer_col: str,
    classes: tuple[str, ...] = DEFAULT_DEBT_CLASSES,
) -> tuple[list[DebtSecurity], list[dict], int]:
    """Pull debt instruments of the requested classes out of MKK export rows.

    Returns (securities, rejected, skipped):
      - securities: validated DebtSecurity records (deduped by ISIN);
      - rejected:   rows whose code looked like a wanted class but failed ISIN
                    validation (bad shape / check digit) — quarantined;
      - skipped:    rows of other (non-requested / non-debt) classes.
    """
    want = set(classes)
    securities: dict[str, DebtSecurity] = {}
    rejected: list[dict] = []
    skipped = 0
    for r in rows:
        isin = str(r.get(isin_col, "") or "").strip().upper()
        if not isin:
            continue
        desc = str(r.get(desc_col, "") or "").strip() if desc_col else ""
        issuer = str(r.get(issuer_col, "") or "").strip() if issuer_col else ""
        cls, sec_type = classify_instrument(isin)
        if cls is None or cls not in want:
            skipped += 1
            continue
        if not is_valid_isin_any(isin):
            rejected.append({"isin": isin, "issuer_name": issuer})
            continue
        if isin in securities:
            continue
        mat, conf, method = parse_maturity(desc)
        securities[isin] = DebtSecurity(
            isin=isin, instrument_class=cls, type=sec_type, issuer_name=issuer,
            description=desc, currency=parse_currency(desc, cls),
            maturity_date=mat, maturity_confidence=conf, maturity_method=method,
        )
    return list(securities.values()), rejected, skipped


def write_reference(
    securities: list[DebtSecurity],
    out_path: Path | str,
    source: str,
    classes: tuple[str, ...] = DEFAULT_DEBT_CLASSES,
    complete: bool = True,
) -> Path:
    """Serialise extracted debt to the committed, dated reference file."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "source": source,
        "fetched_iso": date.today().isoformat(),
        "method": "official-export",
        "schema_version": DEBT_SCHEMA_VERSION,
        "complete": complete,
        "classes": list(classes),
        "securities": [asdict(s) for s in sorted(securities, key=lambda x: x.isin)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


class MkkDebtReference:
    """Loads + validates the committed debt reference file.

    Mirrors ``BistIsinAdapter``: missing file → empty (loader finds nothing);
    every ISIN re-validated on load so a corrupted entry is quarantined, not
    written to the graph.
    """

    def __init__(self, reference_path: Path | str | None = None) -> None:
        self.reference_path = Path(reference_path or DEFAULT_DEBT_REFERENCE_PATH)
        self._securities: list[DebtSecurity] = []
        self._rejected: list[dict] = []
        self._source: str | None = None
        self._fetched_iso: str | None = None
        self._loaded = False

    def __enter__(self) -> "MkkDebtReference":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        return None

    def load(self, strict: bool = False) -> "MkkDebtReference":
        self._securities, self._rejected = [], []
        if not self.reference_path.exists():
            self._loaded = True
            return self
        blob = json.loads(self.reference_path.read_text(encoding="utf-8"))
        self._source = blob.get("source")
        self._fetched_iso = blob.get("fetched_iso")
        for rec in (blob.get("securities") or []):
            isin = (rec.get("isin") or "").strip().upper()
            if not is_valid_isin_any(isin):
                self._rejected.append({"isin": isin,
                                       "issuer_name": rec.get("issuer_name")})
                if strict:
                    raise ValueError(f"invalid ISIN in debt reference: {isin!r}")
                continue
            self._securities.append(DebtSecurity(
                isin=isin,
                instrument_class=rec.get("instrument_class") or "",
                type=rec.get("type") or "",
                issuer_name=rec.get("issuer_name") or "",
                description=rec.get("description") or "",
                currency=rec.get("currency"),
                maturity_date=rec.get("maturity_date"),
                maturity_confidence=float(rec.get("maturity_confidence") or 0.0),
                maturity_method=rec.get("maturity_method") or "none",
            ))
        self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    @property
    def securities(self) -> list[DebtSecurity]:
        self._ensure()
        return list(self._securities)

    def by_issuer(self) -> dict[str, list[DebtSecurity]]:
        """Group debt by issuer short name (the matching key)."""
        self._ensure()
        out: dict[str, list[DebtSecurity]] = {}
        for s in self._securities:
            out.setdefault(s.issuer_name, []).append(s)
        return out

    @property
    def source(self) -> str | None:
        self._ensure()
        return self._source

    @property
    def rejected(self) -> list[dict]:
        self._ensure()
        return list(self._rejected)

    def __len__(self) -> int:
        self._ensure()
        return len(self._securities)

    def smoke_check(self) -> dict:
        """Structural validation: raises on any malformed ISIN in the file."""
        self.load(strict=True)
        classes: dict[str, int] = {}
        for s in self._securities:
            classes[s.instrument_class] = classes.get(s.instrument_class, 0) + 1
        return {"securities": len(self._securities), "rejected": len(self._rejected),
                "by_class": classes, "source": self._source,
                "fetched_iso": self._fetched_iso}

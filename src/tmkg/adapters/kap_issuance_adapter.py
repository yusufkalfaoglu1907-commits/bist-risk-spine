"""KAP issuance-disclosure extractor — discovers *new* debt instruments.

The nominal adapter ([[kap_nominal_adapter]]) only prices ISINs already in the
graph. This one goes further: it reads the KAP "Borçlanma Araçlarının / Kira
Sertifikalarının İşlem Görmeye Başlaması" bulletins, where each row is a
*complete* instrument record, and turns each row into everything needed to
CREATE the Security from scratch — so new issuance enters the graph straight from
KAP, with no MKK export in the loop.

ROW SHAPE (after tag-strip, in document order)
----------------------------------------------
    … ISSUER NAME  TICKER  TYPE  ISIN  NOMINAL(TL)  D_issue  D_maturity  D_value  - …

The issuer/ticker/type for a given ISIN sit in the text that PRECEDES it (the
flattened table cell order is issuer→ticker→type→ISIN→nominal→dates). Reading the
block *after* an ISIN would attach the next row's issuer — a silent off-by-one
that was caught against a live bulletin. So per row we read: ISIN, nominal (TL),
the date run (maturity = the latest date — robust to field reordering), the
instrument class (from the ISIN's 3rd char), and from the preceding block the
issuer's exchange **ticker** (token right before the type keyword). The ticker is
the reliable issuer key; the free-text issuer name is a fallback (fund names
embed stray all-caps tokens that confuse boundary detection).

FAIL-SAFE
---------
A row is emitted only when ISIN is valid, a positive nominal is present, and at
least one date parses (→ maturity). Rows we can't parse cleanly are dropped, not
guessed. Issuer resolution itself happens in the loader (ticker-exact first,
name-match fallback) and is the final gate on whether a Security is created.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
from pathlib import Path

from tmkg import config
from tmkg.adapters.bist_isin_adapter import is_valid_isin_any
from tmkg.adapters.mkk_debt_adapter import classify_instrument
from tmkg.adapters.kap_nominal_adapter import parse_tr_amount

DEFAULT_ISSUANCE_REFERENCE_PATH = (
    config.REPO_ROOT / "data" / "reference" / "kap_issuance.json"
)
DEFAULT_FX_ISSUANCE_REFERENCE_PATH = (
    config.REPO_ROOT / "data" / "reference" / "kap_fx_issuance.json"
)
ISSUANCE_SCHEMA_VERSION = 1

_ISIN = r"TR[A-Z0-9]{10}"
_TR_AMOUNT = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?"
_DATE = r"\d{2}\.\d{2}\.\d{4}"
_NOMINAL_TL_LABEL = "Nominal De"

# --- FX (eurobond) issuance: the "İhraç Belgesi" / "Tertip İhraç Belgesi" form ---
# Eurobonds (XS…) never appear in the domestic TL listing bulletin above; their
# issue size + currency live in the SPK issue-certificate disclosure as a labelled
# field run (verified live, idx 1246045 / 1258577), rendered Turkish-then-English:
#     ISIN: XS… [/ US…]  TÜRÜ: …  İHRAÇ TARİHİ: dd.mm.yyyy  VADE: dd.mm.yyyy|Vadesiz
#     DÖVİZ: ABD Doları  NOMİNAL TUTAR: 600.000.000
# The number is the ISSUE size (a per-currency UPPER BOUND on what's outstanding —
# eurobonds amortise / are perpetual / get partially bought back), so it carries
# basis `fx-issue-size-upper-bound` and must never be folded into a confident
# single-currency total. Domestic TL certificates use the same template with
# DÖVİZ: TL — those resolve to no ISO code and are dropped here (the TL listing
# path already owns them).
FX_ISSUE_BASIS = "fx-issue-size-upper-bound"
_FX_NOMINAL_LABEL = "NOMİNAL TUTAR"           # page gate (İhraç Belgesi marker)
_FX_XS_ISIN = re.compile(r"XS[0-9]{10}")      # the in-graph eurobond ISIN
_FX_DOVIZ = re.compile(r"DÖV[İI]Z\s*:?\s*([^:]+?)\s+NOM[İI]NAL\s+TUTAR\s*:?\s*"
                       rf"({_TR_AMOUNT})")
_FX_ISSUE_DATE = re.compile(rf"[İI]HRAÇ\s+TAR[İI]H[İI]\s*:?\s*({_DATE})")
_FX_VADE = re.compile(rf"VADE\s*:?\s*(?:({_DATE})|(Vadesiz))")
# DÖVİZ free-text → ISO 4217. Longest/most-specific first; "TL"/"Türk Lirası"
# deliberately absent so domestic certificates yield no currency and are skipped.
_FX_CCY_WORDS = (
    ("ABD Doları", "USD"), ("Amerikan Doları", "USD"),
    ("Avro", "EUR"), ("Euro", "EUR"),
    ("Sterlin", "GBP"), ("İsviçre Frangı", "CHF"), ("Japon Yeni", "JPY"),
)

# Instrument-type keywords (longest first so multiword types win). The token
# right before one of these is the issuer's ticker.
_TYPE_KEYWORDS = [
    "Varlığa Dayalı Menkul Kıymet",
    "Kira Sertifikası",
    "Finansman Bonosu",
    "Tahvil",
    "Bono",
]
# A row anchor: ISIN, nominal, then 2-3 dd.mm.yyyy dates.
_ROW = re.compile(rf"({_ISIN})\s+({_TR_AMOUNT})\s+((?:{_DATE}\s+){{2,3}})")
_TICKERLIKE = re.compile(r"^[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9]{1,6}$")


def _clean(raw_html: str) -> str:
    """Unescape only the \\uXXXX JS escapes (preserve UTF-8 Turkish chars), strip
    tags, collapse whitespace. The instrument table is rendered *inside* the
    Next.js RSC payload, so we keep the whole document (don't cut the tail); each
    row's trailing free-text is length-bounded by the parser instead."""
    u = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw_html)
    u = _html.unescape(re.sub(r"<[^>]+>", " ", u))
    return re.sub(r"[ \t\r\n]+", " ", u)


def _parse_date(s: str) -> _dt.date | None:
    try:
        d, m, y = s.split(".")
        return _dt.date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _issuer_and_ticker(block: str) -> tuple[str | None, str | None]:
    """From the block PRECEDING an ISIN ('… ISSUER NAME TICKER TYPE') pull the
    issuer name and ticker. The instrument-type keyword is the last one in the
    block; the token before it is the ticker; the issuer name precedes that.

    Only the tail of the block belongs to this row (earlier text is the previous
    row's date run), so we anchor on the LAST type keyword and read leftward."""
    block = block.strip()
    last = None
    for kw in _TYPE_KEYWORDS:
        i = block.rfind(kw)
        if i >= 0 and (last is None or i > last):
            last = i
    if last is None:
        return None, None
    head = block[:last].strip()                    # "… ISSUER NAME TICKER"
    head = re.sub(r"^.*\d{2}\.\d{2}\.\d{4}", "", head)  # cut through prev row's last date
    head = re.sub(r"^[\s\-]+", "", head)               # strip leading rate/dash
    toks = head.split()
    if not toks:
        return None, None
    ticker = toks[-1] if _TICKERLIKE.match(toks[-1]) else None
    issuer = " ".join(toks[:-1]) if ticker else head
    return (issuer or None), (ticker or None)


def extract_issuances(raw_html: str, source: str) -> dict:
    """Parse one bulletin into full instrument records.

    Returns {"records": [ {isin, nominal, currency, instrument_class, type,
    issue_date, maturity_date, issuer_name, ticker, source} ], "skipped": [...] }.
    Dates are ISO strings; maturity is the latest date in the row.
    """
    text = _clean(raw_html)
    if _NOMINAL_TL_LABEL not in text:
        return {"records": [], "skipped": []}

    anchors = list(_ROW.finditer(text))
    records, skipped, seen = [], [], set()
    for i, m in enumerate(anchors):
        isin = m.group(1).upper()
        if isin in seen:                            # table renders twice; keep first
            continue
        nominal = parse_tr_amount(m.group(2))
        dates = [d for d in (_parse_date(x) for x in re.findall(_DATE, m.group(3))) if d]
        if not is_valid_isin_any(isin) or not nominal or nominal <= 0 or not dates:
            skipped.append({"isin": isin, "reason": "incomplete-row"})
            continue
        seen.add(isin)
        # issuer/ticker/type PRECEDE this ISIN: block from prev row's end to here
        block_start = anchors[i - 1].end() if i > 0 else 0
        block = text[block_start:m.start()][-400:]
        issuer_name, ticker = _issuer_and_ticker(block)
        cls, sec_type = classify_instrument(isin)
        records.append({
            "isin": isin,
            "nominal": nominal,
            "currency": "TRY",
            "instrument_class": cls,
            "type": sec_type,
            "issue_date": min(dates).isoformat(),
            "maturity_date": max(dates).isoformat(),
            "issuer_name": issuer_name,
            "ticker": ticker,
            "source": source,
        })
    return {"records": records, "skipped": skipped}


def _fx_currency(doviz_text: str) -> str | None:
    """Map a DÖVİZ free-text token ('ABD Doları') to an ISO code, or None for TL /
    anything we don't recognise (so domestic certificates are skipped, not guessed)."""
    t = doviz_text.strip()
    for word, iso in _FX_CCY_WORDS:
        if word.lower() in t.lower():
            return iso
    return None


def extract_fx_issuances(raw_html: str, source: str) -> dict:
    """Parse one SPK issue-certificate ('İhraç Belgesi') bulletin into FX eurobond
    pricing records.

    Returns {"records": [ {isin, nominal, currency, basis, instrument_class, type,
    issue_date, maturity_date, source} ], "skipped": [...] }. `maturity_date` is
    None for perpetuals (VADE: Vadesiz). Currency is an ISO code derived from the
    DÖVİZ field; `basis` is always `fx-issue-size-upper-bound`. Only XS ISINs with
    a recognised non-TL currency and a positive nominal are emitted — the
    certificate carries the ISIN, so the loader can match it ISIN-exact.
    """
    text = _clean(raw_html).replace("\xa0", " ")
    if _FX_NOMINAL_LABEL not in text:
        return {"records": [], "skipped": []}        # not an issue certificate

    records, skipped, seen = [], [], set()
    for m in _FX_XS_ISIN.finditer(text):
        isin = m.group(0).upper()
        if isin in seen:                              # TR+EN render → keep first
            continue
        # the labelled fields sit AFTER the ISIN, before the next ISIN occurrence
        nxt = text.find("XS", m.end())
        window = text[m.end(): nxt if nxt != -1 else m.end() + 400]
        dov = _FX_DOVIZ.search(window)
        if not dov:
            continue                                  # no DÖVİZ/NOMİNAL pair for this ISIN
        currency = _fx_currency(dov.group(1))
        nominal = parse_tr_amount(dov.group(2))
        if not is_valid_isin_any(isin) or currency is None or not nominal or nominal <= 0:
            skipped.append({"isin": isin, "currency_text": dov.group(1).strip(),
                            "reason": "no-iso-currency" if currency is None else "bad-nominal"})
            continue
        seen.add(isin)
        idate = _FX_ISSUE_DATE.search(window)
        vade = _FX_VADE.search(window)
        maturity = _parse_date(vade.group(1)) if (vade and vade.group(1)) else None
        cls, sec_type = classify_instrument(isin)
        records.append({
            "isin": isin,
            "nominal": nominal,
            "currency": currency,
            "basis": FX_ISSUE_BASIS,
            "instrument_class": cls,
            "type": sec_type,
            "issue_date": _parse_date(idate.group(1)).isoformat() if idate else None,
            "maturity_date": maturity.isoformat() if maturity else None,
            "source": source,
        })
    return {"records": records, "skipped": skipped}


def load_issuance_reference(path: Path | str | None = None) -> list[dict]:
    """Read the committed issuance reference (ISIN-keyed records). Missing → []."""
    p = Path(path or DEFAULT_ISSUANCE_REFERENCE_PATH)
    if not p.exists():
        return []
    blob = json.loads(p.read_text(encoding="utf-8"))
    return blob.get("issuances") or []


def write_issuance_reference(
    records: list[dict], path: Path | str | None = None,
    source: str = "KAP issuance bulletins", complete: bool = False,
) -> Path:
    """Serialise issuance records (dedup by ISIN, last wins) to the reference."""
    p = Path(path or DEFAULT_ISSUANCE_REFERENCE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    dedup = {r["isin"]: r for r in records if r.get("isin")}
    p.write_text(json.dumps({
        "source": source,
        "fetched_iso": _dt.date.today().isoformat(),
        "schema_version": ISSUANCE_SCHEMA_VERSION,
        "complete": complete,
        "issuances": [dedup[k] for k in sorted(dedup)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_fx_issuance_reference(path: Path | str | None = None) -> list[dict]:
    """Read the committed FX-issuance reference (ISIN-keyed XS records). Missing → []."""
    p = Path(path or DEFAULT_FX_ISSUANCE_REFERENCE_PATH)
    if not p.exists():
        return []
    blob = json.loads(p.read_text(encoding="utf-8"))
    return blob.get("fx_issuances") or []


def write_fx_issuance_reference(
    records: list[dict], path: Path | str | None = None,
    source: str = "KAP eurobond issue certificates (İhraç Belgesi)",
    complete: bool = False,
) -> Path:
    """Serialise FX-issuance records (dedup by ISIN, last wins) to the reference.
    Always `complete:false` until the issuer sweep is proven exhaustive."""
    p = Path(path or DEFAULT_FX_ISSUANCE_REFERENCE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    dedup = {r["isin"]: r for r in records if r.get("isin")}
    p.write_text(json.dumps({
        "source": source,
        "fetched_iso": _dt.date.today().isoformat(),
        "schema_version": ISSUANCE_SCHEMA_VERSION,
        "complete": complete,
        "fx_issuances": [dedup[k] for k in sorted(dedup)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

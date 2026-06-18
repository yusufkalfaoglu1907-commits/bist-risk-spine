"""KAP subsidiary/affiliate extractor — control & ownership edges from the
"Şirket Genel Bilgi Formu" (Company General Information Form).

WHY THIS CLOSES THE LAST CONTAGION GAP
--------------------------------------
GLEIF Level-2 only links the parents a global entity *files* with GLEIF, so most
intra-Turkish control relationships (private SPVs, group subsidiaries) are
missing — only ~62 of ~196 debt issuers carry any CONTROLS edge, making every
group blast-radius total a lower bound. KAP's general-information form fixes this
at the source: every listed company publishes a table of its
"Bağlı Ortaklıklar, Finansal Duran Varlıklar ile Finansal Yatırımlar" — each
related entity with the company's capital share (%) AND the *nature of the
relationship*, which is the company's OWN consolidation declaration:

    Bağlı Ortaklık   -> consolidated SUBSIDIARY  (control)         -> CONTROLS
    İş Ortaklığı     -> joint venture            (joint control)  -> stake only
    İştirak          -> associate / affiliate    (sig. influence) -> stake only
    Finansal Yatırım -> financial investment      (<influence)     -> stake only

Crucially the "Bağlı Ortaklık" label is the CONTROL signal even when the direct
% is below 50 (e.g. Koç→Arçelik 48.5%, Koç→Yapı Kredi 20.2% — controlled via
shareholder agreements / consolidation), so it captures control a naive ">50%"
rule would miss. The disclosing company is always the PARENT.

ROW SHAPE (after tag-strip, in document order)
----------------------------------------------
    … Ticaret Unvanı  Faaliyet Konusu  ÖdenmişSermaye  Ş.Payı  PB  Pay(%)  İlişki
    <CHILD NAME …A.Ş.> <activity …>     <capital>       <share> <ccy> <pct>  <rel>

The numeric tail (capital, share, 3-letter currency, percent, relationship
keyword) is a stable anchor; the CHILD NAME + activity sit in the text that
PRECEDES it. The name ends at its legal-form suffix ("A.Ş.", "Inc.", "B.V." …),
so the trailing activity phrase is split off there. Requiring a legal suffix also
discards the form's free-text footnotes, which would otherwise parse as garbage
rows.

FAIL-SAFE
---------
A relationship is emitted only when the name carries a legal suffix, the
relationship keyword is one of the four known kinds, and (when present) the
percent parses. The table renders twice in the RSC payload → dedup by child name.
Child→Company resolution itself is the precision gate and lives in the loader
(fuzzy identity-token match + distinctive-token guard); unresolved children are
logged, never invented.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
from pathlib import Path

from tmkg import config
from tmkg.adapters.kap_nominal_adapter import parse_tr_amount

DEFAULT_SUBSIDIARY_REFERENCE_PATH = (
    config.REPO_ROOT / "data" / "reference" / "kap_subsidiary.json"
)
SUBSIDIARY_SCHEMA_VERSION = 1

# The last header cell of the subsidiaries table — everything after it is rows.
_SECTION_ANCHOR = "Şirket ile Olan İlişkinin Niteliği"

# Relationship nature -> edge kind. 'subsidiary' yields CONTROLS; the rest are
# ownership stakes only (joint/associate/financial — not unilateral control).
_RELATION_KIND = {
    "Bağlı Ortaklık": "subsidiary",
    "İş Ortaklığı": "jv",
    "İştirak": "associate",
    "Finansal Yatırım": "investment",
}
# Longest-first so multiword keywords win the alternation.
_REL = r"(Bağlı Ortaklık|İş Ortaklığı|Finansal Yatırım|İştirak)"
# capital  share  CCY  pct  relationship  (capital/share may carry TR separators)
_ROW = re.compile(rf"([\d.,]+)\s+([\d.,]+)\s+([A-Z]{{3}})\s+([\d.,]+)\s+{_REL}")

# Legal-form suffixes that terminate a company name (Turkish + common foreign).
_LEGAL_SUFFIX = re.compile(
    r"(.*?\b(?:A\.Ş\.|A\.Ş|Inc\.|B\.V\.|N\.V\.|Ltd\.\s*Şti\.|Ltd\.|Ltd|GmbH|"
    r"S\.A\.|S\.p\.A\.|LLC|L\.L\.C\.|Co\.|Corp\.|Corporation|Limited|AG|"
    r"Private\s+Ltd\.))",
    re.UNICODE,
)


def _clean(raw_html: str) -> str:
    """Unescape \\uXXXX JS escapes (keep UTF-8 Turkish chars), strip tags, collapse
    whitespace — same normalisation the issuance/nominal adapters use."""
    u = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw_html)
    u = _html.unescape(re.sub(r"<[^>]+>", " ", u))
    return re.sub(r"[ \t\r\n]+", " ", u)


def parse_pct(s: str) -> float | None:
    """Turkish percent string -> float. '48,53'->48.53, '70'->70.0, '100,00'->100.0.

    A thousands dot is only meaningful when a decimal comma is also present
    (percentages rarely exceed 100, but stay robust); otherwise the dot is kept
    as the decimal point for already-dotted inputs."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        return float(s)
    except ValueError:
        return None


def extract_subsidiaries(raw_html: str, source: str) -> dict:
    """Parse one general-information form into related-party records.

    Returns {"records": [ {child_name, relation, relation_kind, pct, currency,
    paid_capital, share_amount, source} ], "skipped": [...] }. Does NOT know the
    parent — the harvester attaches parent_ticker per disclosure.
    """
    text = _clean(raw_html)
    h = text.find(_SECTION_ANCHOR)
    if h < 0:
        return {"records": [], "skipped": []}
    sec = text[h + len(_SECTION_ANCHOR):]

    records, skipped, seen = [], [], set()
    prev = 0
    for m in _ROW.finditer(sec):
        block = sec[prev:m.start()].strip()
        prev = m.end()
        sm = _LEGAL_SUFFIX.match(block)
        if not sm:
            # no legal suffix => footnote / non-row text; skip silently
            continue
        name = sm.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        relation = m.group(5)
        kind = _RELATION_KIND.get(relation)
        if kind is None:
            skipped.append({"child_name": name, "reason": "unknown-relation"})
            continue
        records.append({
            "child_name": name,
            "relation": relation,
            "relation_kind": kind,
            "pct": parse_pct(m.group(4)),
            "currency": m.group(3),
            "paid_capital": parse_tr_amount(m.group(1)),
            "share_amount": parse_tr_amount(m.group(2)),
            "source": source,
        })
    return {"records": records, "skipped": skipped}


def load_subsidiary_reference(path: Path | str | None = None) -> list[dict]:
    """Read the committed subsidiary reference (flat parent->child relations)."""
    p = Path(path or DEFAULT_SUBSIDIARY_REFERENCE_PATH)
    if not p.exists():
        return []
    blob = json.loads(p.read_text(encoding="utf-8"))
    return blob.get("relations") or []


def write_subsidiary_reference(
    relations: list[dict], path: Path | str | None = None,
    source: str = "KAP Şirket Genel Bilgi Formu", complete: bool = False,
) -> Path:
    """Serialise relations (dedup by parent_ticker+child_name, last wins)."""
    p = Path(path or DEFAULT_SUBSIDIARY_REFERENCE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    dedup = {(r.get("parent_ticker"), r.get("child_name")): r for r in relations
             if r.get("parent_ticker") and r.get("child_name")}
    ordered = [dedup[k] for k in sorted(dedup, key=lambda x: (x[0] or "", x[1] or ""))]
    p.write_text(json.dumps({
        "source": source,
        "fetched_iso": _dt.date.today().isoformat(),
        "schema_version": SUBSIDIARY_SCHEMA_VERSION,
        "complete": complete,
        "relations": ordered,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

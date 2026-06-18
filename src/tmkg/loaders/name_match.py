"""Shared Turkish entity-name matching primitives.

Brand-token extraction, identity-token isolation and structural entity-type
classification used to resolve KAP/MKK short names onto Company nodes. Extracted
from the (now archived) debt back-fill so the on-mission KAP-subsidiary loader
keeps a single, tested implementation of this logic. Builds on the ASCII-folding
and normalisation primitives in ``gleif_adapter``.
"""
from __future__ import annotations

from tmkg.adapters.gleif_adapter import _ascii_fold, _norm_for_score, _brand_tokens

# Generic industry/structure tokens that do NOT individuate an entity, so an
# overlap on boilerplate alone can't resolve a match.
_GENERIC_TOKENS = {
    # legal / structural
    "ANONIM", "SIRKETI", "ORTAKLIGI", "ORTAKLIK", "GRUBU", "GRUP", "VE",
    # finance industry
    "YATIRIM", "YATIRIMLAR", "MENKUL", "DEGERLER", "KIYMETLER", "FINANS",
    "FINANSAL", "FINANSMAN", "FAKTORING", "KIRALAMA", "VARLIK", "BANK",
    "BANKA", "BANKASI", "BONOSU", "SUKUK", "GIRISIM", "SERMAYESI", "SIGORTA",
    "EMEKLILIK", "EMEKLI", "HOLDING", "KATILIM", "PORTFOY", "GAYRIMENKUL",
    "GMYO", "GYO", "FON", "FONU",
    # common abbreviations seen in MKK short names
    "FIN", "KIR", "MEN", "DEG", "MENK", "KIR.", "FIN.",
    # generic corporate descriptors
    "SANAYI", "TICARET", "TICARI", "URUNLER", "HISSE",
    # operational/market-member noise
    "OPERASYONEL", "UYE", "IHRACCI", "BANKA BONOSU",
}

# Entity-type buckets that must AGREE for a match. Detected from structural
# tokens so we never attribute one group entity's relation to a sibling of a
# different type (a bank vs its REIT, a leasing arm vs its factoring arm, ...).
# All members are mutually exclusive "structural" types; an operating/industrial
# company has type None (no guard applied beyond brand identity).
_STRUCTURAL_TYPES = {
    "BANK", "FACTORING", "LEASING", "ASSET_LEASING", "BROKER", "VC",
    "INSURANCE", "PENSION", "FINANCING", "REIT", "HOLDING",
}


def _brand_set(name: str) -> set[str]:
    """ASCII-folded brand tokens of a name, split on punctuation.

    The legal-suffix run is stripped by ``_brand_tokens``; each remaining token
    is folded, uppercased and split on non-alphanumeric characters so hyphenated
    or dotted forms ("COCA-COLA", "T.A.Ş") decompose into comparable tokens.
    """
    out: set[str] = set()
    for tok in _brand_tokens(name):
        folded = _ascii_fold(tok).upper()
        for piece in "".join(c if c.isalnum() else " " for c in folded).split():
            if piece:
                out.add(piece)
    return out


def _identity_tokens(name: str) -> set[str]:
    """Identity-bearing brand tokens: non-generic words.

    These individuate an entity ("AK", "NUROL", "COCA", "QNB"); generic
    industry/structure words are excluded so an overlap on boilerplate alone
    can't resolve a match. Single-character tokens are normally dropped (they
    are usually a trailing share-class letter, "İŞ BANKASI A"), BUT if that
    would leave NO identity token they are kept — some real issuers ARE a single
    letter ("D YATIRIM BANKASI", "Q YATIRIM HOLDİNG").
    """
    nongeneric = {t for t in _brand_set(name) if t not in _GENERIC_TOKENS}
    multi = {t for t in nongeneric if len(t) >= 2}
    return multi or nongeneric


def _lead_identity(name: str) -> str | None:
    """The issuer's FIRST identity-bearing brand token (in name order).

    Used as a hard gate: the lead brand must appear in a candidate's brand set,
    so "GURMAT ELEKTRİK ÜRETİM" cannot resolve to "AKENERJİ ELEKTRİK ÜRETİM"
    on the shared sector words alone."""
    ident = _identity_tokens(name)
    for tok in _brand_tokens(name):
        folded = _ascii_fold(tok).upper()
        for piece in "".join(c if c.isalnum() else " " for c in folded).split():
            if piece in ident:
                return piece
    return None


def _entity_type(name: str) -> str | None:
    """Classify a name into a structural entity-type bucket, or None (operating).

    Order matters: more specific signatures (VARLIK KİRALAMA SPV, GİRİŞİM
    SERMAYESİ trust) are tested before the generic BANK/FINANSMAN ones."""
    toks = set(_norm_for_score(name).split())
    has = toks.__contains__

    def bank_token() -> bool:
        return any(t in ("BANK", "BANKA", "BANKASI") or t.endswith("BANK")
                   or t.endswith("BANKASI") for t in toks)

    if has("VARLIK"):
        # "... VARLIK KİRALAMA" sukuk SPV; MKK short names often truncate to
        # just "... VARLIK", so VARLIK alone is enough to mark the SPV type.
        return "ASSET_LEASING"
    if has("GIRISIM") and has("SERMAYESI"):
        return "VC"
    if has("FAKTORING"):
        return "FACTORING"
    if (has("FINANSAL") and has("KIRALAMA")) or (has("FIN") and has("KIR")) \
            or has("LEASING"):
        return "LEASING"
    if has("MENKUL") or (has("MEN") and has("DEG")) or has("MENK"):
        return "BROKER"
    if has("GAYRIMENKUL") or has("GMYO") or has("GYO"):
        return "REIT"
    if has("SIGORTA"):
        return "INSURANCE"
    if has("EMEKLILIK") or has("EMEKLI"):
        return "PENSION"
    if bank_token():
        return "BANK"
    if has("FINANSMAN") or has("FIN"):
        return "FINANCING"
    if has("HOLDING"):
        return "HOLDING"
    return None


def _type_conflict(issuer_type: str | None, company_type: str | None) -> bool:
    """True if the entity-type guard should REJECT this candidate.

    The guard is ASYMMETRIC, keyed on the ISSUER. When the issuer carries a
    structural type (BANK, ASSET_LEASING, REIT, ...), the candidate company MUST
    carry the same type — a bank's debt cannot land on its sibling REIT or on an
    operating company that merely shares the brand (this is what attributed
    ZİRAAT BANKASI's bonds to TÜRK TRAKTÖR). When the issuer has NO structural
    type (an operating company, or a short name the classifier can't place), we
    do NOT reject on the company's type, because MKK short names routinely drop
    the structural suffix (e.g. "TAV HAVALİMANLARI" → "... HOLDİNG",
    "NUROL VARLIK" → "... VARLIK KİRALAMA") and those ARE the same entity."""
    it = issuer_type if issuer_type in _STRUCTURAL_TYPES else None
    ct = company_type if company_type in _STRUCTURAL_TYPES else None
    if it is None:
        return False
    return it != ct

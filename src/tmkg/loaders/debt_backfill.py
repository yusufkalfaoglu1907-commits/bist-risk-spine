"""Corporate-debt back-fill — attach debt Securities to issuer Company nodes.

Reads the committed MKK debt reference (`mkk_debt_adapter.MkkDebtReference`),
matches each debt issuer's short name (MKKÇ Adı, e.g. "AK FAKTORİNG") to a
Company already in the graph using the SAME brand-token coverage matcher the
GLEIF back-fill uses, and creates `Security` nodes + `(Company)-[:ISSUES]->`
edges for confident matches.

Issuer scope (default LISTED-ONLY): only issuers that resolve to an existing
Company node get debt nodes. This is deliberate — the project already seeded the
KAP `IGS` universe, including the 135 NON_EQUITY_ISSUER factoring/leasing/SPV
firms specifically kept as debt-stage anchors, so most real issuers are present.
Debt from issuers NOT in the graph (private SPVs, unlisted banks) is logged to
the audit report, never silently turned into a fuzzy-matched new entity. Pass
``create_missing_issuers=True`` to opt into the fuller (noisier) all-issuers map.

Provenance stance (same as every other loader): nothing is guessed. ISINs are
pre-validated by the reference adapter; maturity dates carry the inferred
confidence from extraction; issuer matches below threshold are logged, not
written. Idempotent: MERGE-keyed on Security.uuid and the ISSUES edge.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.mkk_debt_adapter import MkkDebtReference, DebtSecurity
from tmkg.adapters.gleif_adapter import (
    _ascii_fold, _norm_for_score, _brand_tokens, _NOISE_ENTITY_MARKERS,
)

_DEBT_SOURCE = "MKK Menkul Kıymetler Listesi (debt instruments)"
_EXTRACTION_METHOD = "reference-mkk"

# Bump when the matching logic below changes (so a re-run's audit report is
# clearly attributable to a given matcher generation). v1 = the original reused
# GLEIF brand-coverage matcher; v2 = precision-hardened (identity tokens +
# generic stoplist + entity-type guard + verified alias map); v3 = Phase-2.2
# unmatched split (sovereign blocklist + brand-stub attachment).
MATCHER_VERSION = 3

# --- Phase 2.2: unmatched split (F2) ---------------------------------------
#
# The 613 instruments the precision matcher could not attach are NOT one
# undifferentiated "unmatched" failure. They split three ways, and reporting
# them as one number both overstated the loss and hid a policy decision.

# (a) POLICY BLOCKLIST — sovereign domestic debt (DİBS / Hazine) that rides into
# the MKK *corporate*-debt export and only "matched" anything via token overlap.
# It is OUT OF SCOPE for corporate-contagion analysis, not a matching miss, so
# it is excluded explicitly with a reason code rather than counted as unmatched.
# Detected by issuer-name token (ASCII-folded, uppercase).
_SOVEREIGN_TOKENS = {"DIBS", "HAZINE"}

# (b) BRAND-STUB ATTACHMENT — issuers whose legal entity is not in the listed
# universe but whose GROUP has a curated EXTERNAL_STUB parent (Phase 2.1). Their
# debt attaches to that stub so the group's wall is visible, converging with the
# SPV-parent source on ONE node per real entity. Keyed on _norm_for_score(name);
# value = the stub brand (stub uuid == 'stub-<BRAND>', see
# external_stub_backfill._stub_uuid). Each verified as the same real group.
_ISSUER_STUB_BRAND = {
    "ZIRAAT BANKASI BANKA BONOSU": "ZIRAAT",   # Ziraat group (state bank)
    "DENIZBANK": "DENIZ",                       # Deniz group
    "DESTEK YATIRIM BANKASI": "DESTEK",         # Destek group
    "T EKONOMI BANK": "TEB",                    # Türkiye Ekonomi Bankası
}
# (c) the residual whose entity is genuinely in-graph routes through the verified
# `_ISSUER_ALIASES` map above — no new mechanism needed.


def _is_sovereign(issuer_name: str) -> bool:
    """True if the issuer is sovereign domestic debt (DİBS / Hazine) — a policy
    exclusion, not a matching failure (Phase 2.2 bucket a)."""
    return bool(_SOVEREIGN_TOKENS & set(_norm_for_score(issuer_name).split()))


def _stub_attach_uuid(conn: kuzu.Connection, issuer_name: str) -> str | None:
    """The brand-stub uuid this unmatched issuer should attach to, or None.

    Returns the uuid only when a verified brand mapping exists AND that stub node
    is actually present in the graph (the stubs stage ran) — so a debt re-run on
    a graph without stubs degrades to logging unmatched, never to a dangling edge.
    """
    brand = _ISSUER_STUB_BRAND.get(_norm_for_score(issuer_name))
    if not brand:
        return None
    uuid = "stub-" + brand   # mirrors external_stub_backfill._stub_uuid
    r = conn.execute("MATCH (c:Company {uuid:$u}) RETURN c.uuid", {"u": uuid})
    return uuid if r.has_next() else None

# --- precision hardening ---------------------------------------------------
#
# WHY: the v1 matcher reused the GLEIF brand-coverage scorer, which was tuned for
# long-legal-name vs long-legal-name comparisons with API pre-filtering. On SHORT
# MKK issuer names matched against the WHOLE company set it misfired both ways:
#   - generic industry/structure words (YATIRIM, BANKASI, FAKTORİNG, VARLIK,
#     ENERJİ ...) satisfied "coverage" so unrelated entities collided
#     (e.g. INVEST AZ → A1 CAPİTAL; DESTEK YATIRIM BANKASI → AKTİF YATIRIM BANKASI);
#   - distinctive group brands matched the WRONG legal entity of the same group
#     (e.g. NUROL YATIRIM BANK → NUROL GAYRİMENKUL YO — 41 instruments misattributed).
# Project decision (contagion graph): PRECISION-FIRST — a wrong edge is worse than
# a missing one; rejected issuers are logged for review, never guessed.

# Generic corporate / legal-structure / finance-industry / sector words. These
# are NOT identity-bearing: an overlap on them alone must never resolve a match.
# ASCII-folded, uppercase (matched against folded brand tokens).
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

# Issuer short names verified to have NO correct counterpart in the company
# universe but a brand token that collides with an unrelated group entity. Listed
# here so they are rejected outright (precision-first) rather than attached to the
# wrong entity. Keyed on the _norm_for_score form. Reviewable, like the aliases.
_ISSUER_BLOCKLIST = {
    "SEKER YATIRIM",   # Şeker Yatırım (brokerage) not listed; collides with
                       # ŞEKER GYO / ŞEKER FİN. KİR. / BOR ŞEKER.
}
# NOTE: sector words (ENERJİ/ELEKTRİK/ÜRETİM/YENİLENEBİLİR/ÇİMENTO ...) are NOT
# generic here. They stay identity-bearing so a same-brand-different-sector
# collision is caught by the lead-token + coverage gate (e.g. LİMAK YENİLENEBİLİR
# must not match LİMAK ... ÇİMENTO), while GURMAT/AKENERJİ are already separated
# by their distinct lead brand.

# Geographic words: kept as identity tokens (so "TÜRK FİNANSMAN" still resolves)
# but NOT counted as an "extra" the issuer lacks — so "MERCEDES-BENZ FİNANSMAN"
# prefers "... FİNANSMAN TÜRK" (adds only a geo word) over "... KAMYON FİNANSMAN"
# (adds a product line the issuer never named).
_GEO_SOFT_TOKENS = {"TURK", "TURKIYE"}

# Fund/foundation markers that legitimately embed a brand and cause collisions
# (skip such ISSUERS and COMPANIES). Debt-specific: deliberately EXCLUDES bare
# "VAKIF" — a debt issuer like "VAKIF VARLIK KİRALAMA" is Vakıfbank's SPV, not a
# foundation (those end in the genitive "VAKFI").
_DEBT_FUND_MARKERS = {"FON", "FONU", "VAKFI", "PORTFOY", "EMEKLILIK"}

# Verified manual aliases for issuer short names that cannot be resolved by
# brand/entity logic (acronyms, abbreviations, or brand≠legal-name). Key = the
# issuer name normalized via _norm_for_score; value = the company's PRIMARY
# ticker (Company.ticker, the first stockCode). Each verified against the KAP
# member universe as the SAME legal entity that is the listed issuer.
_ISSUER_ALIASES = {
    "VAKIFBANK": "TVB",        # TÜRKİYE VAKIFLAR BANKASI T.A.O.
    "TURK TELEKOM": "TTKOM",   # TÜRK TELEKOMÜNİKASYON A.Ş.
    "FORD OTOSAN": "FROTO",    # FORD OTOMOTİV SANAYİ A.Ş.
    "T S K B": "TSK",          # TÜRKİYE SINAİ KALKINMA BANKASI A.Ş.
    "IS BANKASI A": "ISATR",   # TÜRKİYE İŞ BANKASI A.Ş. (preserve, short brand)
}

# Entity-type buckets that must AGREE for a match. Detected from structural
# tokens so we never attribute one group entity's debt to a sibling of a
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


def _load_companies(conn: kuzu.Connection) -> list[dict]:
    # Candidate pool for fuzzy issuer matching. EXTERNAL_PARENT nodes (real GLEIF
    # legal entities materialised by the L2 stage, filings-grade names, 0.95) ARE
    # valid targets — e.g. "ZİRAAT BANKASI BANKA BONOSU" correctly resolves to the
    # in-graph "TÜRKİYE CUMHURİYETİ ZİRAAT BANKASI". But EXTERNAL_STUB nodes are
    # *inferred* brand placeholders (0.70); debt attaches to them ONLY through the
    # curated `_ISSUER_STUB_BRAND` map (Phase 2.2 bucket b), never by fuzzy brand
    # overlap — so they are excluded from this pool to keep that attachment curated.
    res = conn.execute(
        "MATCH (c:Company) WHERE c.name IS NOT NULL AND c.name <> '' "
        "AND (c.listing_status IS NULL OR c.listing_status <> 'EXTERNAL_STUB') "
        "RETURN c.uuid, c.name, c.ticker ORDER BY c.name")
    rows = []
    while res.has_next():
        uuid, name, ticker = res.get_next()
        rows.append({"uuid": uuid, "name": name, "ticker": ticker,
                     "brand": _brand_set(name),
                     "identity": _identity_tokens(name),
                     "etype": _entity_type(name)})
    return rows


def match_issuer_detailed(issuer_name: str, companies: list[dict],
                          threshold: float = 0.6) -> tuple[dict | None, str, str]:
    """Resolve a debt issuer's short name to a Company (precision-first).

    Returns ``(match | None, reason, method)``:
      - method "alias": resolved via the verified alias map (score 1.0);
      - method "brand": resolved by identity-token coverage + entity-type guard;
      - on None, ``reason`` is one of: no-identity-tokens, no-brand-overlap,
        lead-brand-mismatch, entity-type-conflict, below-threshold, fund-issuer.

    Gate (precision-first): a candidate qualifies only if its brand contains the
    issuer's LEAD identity token, its entity type is compatible with the issuer's,
    and identity coverage (share of the ISSUER's identity tokens it covers) is
    >= threshold. Among qualifiers, the best is the one with the highest coverage,
    then the FEWEST extra identity tokens the issuer lacks (so a same-group SPV
    resolves to the exact sibling, not a more elaborate one), then the closest
    token count.
    """
    norm = _norm_for_score(issuer_name)

    # 1) verified alias map — exact, takes precedence over fuzzy logic.
    alias_ticker = _ISSUER_ALIASES.get(norm)
    if alias_ticker is not None:
        for c in companies:
            if (c.get("ticker") or "").strip().upper() == alias_ticker:
                return ({"uuid": c["uuid"], "name": c["name"],
                         "ticker": c["ticker"], "score": 1.0}, "alias", "alias")
        # alias declared but the ticker isn't in the graph — fall through to brand
        # logic rather than silently dropping (and note it in the reason).

    issuer_tokens = set(norm.split())
    if _DEBT_FUND_MARKERS & issuer_tokens:
        return (None, "fund-issuer", "")
    if norm in _ISSUER_BLOCKLIST:
        return (None, "blocklisted-no-target", "")

    issuer_identity = _identity_tokens(issuer_name)
    if not issuer_identity:
        return (None, "no-identity-tokens", "")
    issuer_brand = _brand_set(issuer_name)
    lead = _lead_identity(issuer_name)
    issuer_type = _entity_type(issuer_name)
    n_issuer_tokens = len(issuer_tokens)

    saw_overlap = saw_lead = saw_typeok = False
    best = None
    best_key = None  # (cov, full_overlap, -extra, -tokendiff) maximized
    share_count = 0   # how many qualifying companies share the lead brand
    for c in companies:
        c_brand = c.get("brand") or _brand_set(c["name"])
        if c_brand and (_DEBT_FUND_MARKERS
                        & set(_norm_for_score(c["name"]).split())):
            continue  # skip fund/foundation companies (brand-embedding)
        c_identity = c.get("identity")
        if c_identity is None:
            c_identity = _identity_tokens(c["name"])
        matched = issuer_identity & c_identity
        if not matched:
            continue
        saw_overlap = True
        if lead is not None and lead not in c_brand:
            continue  # lead brand must be present
        saw_lead = True
        c_etype = c["etype"] if "etype" in c else _entity_type(c["name"])
        if _type_conflict(issuer_type, c_etype):
            continue
        saw_typeok = True
        share_count += 1
        cov = len(matched) / len(issuer_identity)
        full_overlap = len(issuer_brand & c_brand)       # incl. generic tokens
        # company brand the issuer lacks, ignoring soft geographic additions
        extra = len((c_identity - issuer_identity) - _GEO_SOFT_TOKENS)
        tokendiff = abs(len(set(_norm_for_score(c["name"]).split())) - n_issuer_tokens)
        key = (round(cov, 6), full_overlap, -extra, -tokendiff)
        if best_key is None or key > best_key:
            best, best_key = c, key

    if best is None:
        if not saw_overlap:
            return (None, "no-brand-overlap", "")
        if not saw_lead:
            return (None, "lead-brand-mismatch", "")
        return (None, "entity-type-conflict", "")
    best_cov = best_key[0]
    if best_cov < threshold:
        return (None, "below-threshold", "")
    out = {"uuid": best["uuid"], "name": best["name"],
           "ticker": best["ticker"], "score": round(best_cov, 3)}
    # Flag matches resting on a single brand token shared by several candidates
    # (an ambiguous group brand, e.g. "VESTEL") so the audit can spot-check them.
    if len(issuer_identity) == 1 and share_count > 1:
        out["ambiguous_brand"] = True
    return (out, "matched", "brand")


def match_issuer(issuer_name: str, companies: list[dict],
                 threshold: float = 0.6) -> dict | None:
    """Best Company match for a debt issuer's short name, or None if unresolved.

    Thin wrapper over :func:`match_issuer_detailed` for callers that only need
    the match (the loader uses the detailed form to log reject reasons)."""
    m, _reason, _method = match_issuer_detailed(issuer_name, companies, threshold)
    return m


def _security_uuid(isin: str) -> str:
    return "deb-" + isin


def _write_security(conn: kuzu.Connection, company_uuid: str,
                    s: DebtSecurity) -> None:
    """MERGE the debt Security and its ISSUES edge (idempotent)."""
    sid = _security_uuid(s.isin)
    conn.execute(
        "MERGE (sec:Security {uuid:$u}) "
        "SET sec.isin=$isin, sec.type=$typ, sec.currency=$ccy, "
        "    sec.issuer_name=$iss, sec.description=$descr, "
        "    sec.maturity_confidence=$mconf",
        {"u": sid, "isin": s.isin, "typ": s.type, "ccy": s.currency,
         "iss": s.issuer_name, "descr": s.description,
         "mconf": s.maturity_confidence},
    )
    # DATE column: set only when parsed (avoid writing a NULL cast).
    if s.maturity_date:
        conn.execute(
            "MATCH (sec:Security {uuid:$u}) SET sec.maturity_date=date($d)",
            {"u": sid, "d": s.maturity_date})
    # confidence on the edge = the maturity inference confidence (the only
    # inferred part); the ISIN→issuer link itself is reference-grade.
    conn.execute(
        "MATCH (c:Company {uuid:$cu}), (sec:Security {uuid:$su}) "
        "MERGE (c)-[r:ISSUES]->(sec) "
        "SET r.instrument_class=$cls, r.source=$src, "
        "    r.extraction_method=$meth, r.confidence=$conf",
        {"cu": company_uuid, "su": sid, "cls": s.instrument_class,
         "src": _DEBT_SOURCE, "meth": _EXTRACTION_METHOD,
         "conf": s.maturity_confidence},
    )


def backfill_debt(
    conn: kuzu.Connection,
    reference: MkkDebtReference,
    threshold: float = 0.6,
    create_missing_issuers: bool = False,
    limit: int | None = None,
    report_path: Path | str | None = None,
) -> dict:
    """Attach debt instruments from the reference to their issuer Company nodes.

    Returns a summary dict and writes a full audit report (matched issuers with
    their scores, unmatched issuers with debt counts, low-confidence maturities,
    quarantined ISINs).
    """
    reference.load()
    companies = _load_companies(conn)
    by_issuer = reference.by_issuer()
    issuers = sorted(by_issuer)
    if limit is not None:
        issuers = issuers[:limit]

    matched_report: list[dict] = []
    unmatched_report: list[dict] = []
    sovereign_report: list[dict] = []
    low_conf: list[dict] = []
    securities_written = edges_written = 0
    issuers_matched = 0
    issuers_attached_to_stub = 0
    securities_sovereign_excluded = 0

    for issuer in issuers:
        secs = by_issuer[issuer]

        # (a) policy exclusion: sovereign DİBS/Hazine book is out of corporate
        # scope — reason-coded, never counted as a matching failure (F2).
        if _is_sovereign(issuer):
            sovereign_report.append({"issuer_name": issuer,
                                     "debt_count": len(secs),
                                     "reason": "sovereign-out-of-scope"})
            securities_sovereign_excluded += len(secs)
            continue

        m, reason, method = match_issuer_detailed(issuer, companies,
                                                  threshold=threshold)
        attached_to_stub = False
        if m is None:
            # (b) attach to a curated brand stub if this issuer's group has one
            stub_uuid = _stub_attach_uuid(conn, issuer)
            if stub_uuid is not None:
                m = {"uuid": stub_uuid, "name": issuer, "ticker": None,
                     "score": None}
                method = "stub"
                attached_to_stub = True
            elif create_missing_issuers:
                # create a minimal Company node for the unlisted issuer
                cu = "iss-" + _ascii_fold(issuer).upper().replace(" ", "-")[:48]
                conn.execute(
                    "MERGE (c:Company {uuid:$u}) SET c.name=$n, "
                    "c.listing_status='NON_EQUITY_ISSUER', c.is_listed=false",
                    {"u": cu, "n": issuer})
                m = {"uuid": cu, "name": issuer, "ticker": None,
                     "score": None, "created": True}
                method = "created"
            else:
                unmatched_report.append({"issuer_name": issuer,
                                         "debt_count": len(secs),
                                         "reason": reason,
                                         "isins": [s.isin for s in secs][:25]})
                continue

        issuers_matched += 1
        if attached_to_stub:
            issuers_attached_to_stub += 1
        for s in secs:
            _write_security(conn, m["uuid"], s)
            securities_written += 1
            edges_written += 1
            if s.maturity_confidence < 0.9:
                low_conf.append({"isin": s.isin, "issuer_name": issuer,
                                 "maturity_date": s.maturity_date,
                                 "confidence": s.maturity_confidence,
                                 "method": s.maturity_method})
        matched_report.append({
            "issuer_name": issuer, "company_uuid": m["uuid"],
            "company_name": m["name"], "ticker": m.get("ticker"),
            "score": m.get("score"), "match_method": method,
            "ambiguous_brand": m.get("ambiguous_brand", False),
            "created": m.get("created", False),
            "attached_to_stub": attached_to_stub,
            "debt_count": len(secs),
        })

    # Honest denominator (F2): the sovereign book is a *scoped-out* policy
    # exclusion, so the unmatched RATE that the acceptance gate (≤5%) is measured
    # against is over the in-scope corporate reference only.
    ref_total = len(reference)
    in_scope_total = ref_total - securities_sovereign_excluded
    securities_unmatched = sum(u["debt_count"] for u in unmatched_report)
    summary = {
        "issuers_total": len(issuers),
        "issuers_matched": issuers_matched,
        "issuers_attached_to_stub": issuers_attached_to_stub,
        "issuers_unmatched": len(unmatched_report),
        "issuers_sovereign_excluded": len(sovereign_report),
        "securities_written": securities_written,
        "securities_unmatched": securities_unmatched,
        "securities_sovereign_excluded": securities_sovereign_excluded,
        "edges_written": edges_written,
        "in_scope_reference": in_scope_total,
        "unmatched_rate": round(securities_unmatched / in_scope_total, 4)
                          if in_scope_total else 0.0,
        "low_confidence_maturities": len(low_conf),
        "quarantined_isins": len(reference.rejected),
    }

    cache_dir = Path(config.RAW_DOCS_PATH).parent / "cache"
    rp = Path(report_path) if report_path else (cache_dir / "mkk_debt_report.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "matcher_version": MATCHER_VERSION,
        "reference_source": reference.source,
        "reference_securities": len(reference),
        "threshold": threshold,
        "create_missing_issuers": create_missing_issuers,
        "summary": summary,
        "matched": matched_report,
        "unmatched": unmatched_report,
        "sovereign_excluded": sovereign_report,
        "low_confidence_maturities": low_conf,
        "quarantined_isins": reference.rejected,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {**summary, "report": str(rp)}

"""GLEIF acquisition adapter — LIVE (Level-1 records).

Architecture §3 (GLEIF): "the identity spine (join key)". We attach the GLEIF
LEI plus `legal_form` (ELF code), registered `jurisdiction`, and
`registration_authority` to the Company nodes KAP seeded, so the LEI becomes the
canonical external join key the rest of the system (ISIN/LEI time-series join in
Phase 2, OpenSanctions matching in Phase 4) keys on.

  GLEIF API facts verified 2026-06-06 against api.gleif.org (public, CC0, no key):
  - `filter[entity.legalName]` does case-INSENSITIVE token matching but is
    diacritic-SENSITIVE for Turkish letters: "KOÇ HOLDING" returns 0, but
    "KOÇ HOLDİNG" (dotted İ, as GLEIF stores it) matches. KAP names already carry
    the correct Turkish diacritics, so we DO NOT ASCII-fold the query — folding
    would break more matches than it fixes.
  - GLEIF spells legal forms out ("ANONİM ŞİRKETİ", "ANONİM ORTAKLIĞI"); KAP
    abbreviates ("A.Ş."). So we strip the legal-form suffix from the KAP name
    before querying and match on the distinctive brand tokens.
  - Many Turkish names collide with funds/foundations that embed the brand
    ("... KOÇ HOLDİNG EMEKLİ VAKFI ..."). Name matching is therefore FUZZY and
    INFERRED, never filings-grade — every match is scored and only LEIs at/above
    a confidence threshold are written to the graph; the rest go to an audit
    report for human review (consistent with the project's provenance stance).

`smoke_check()` re-verifies the live endpoint + matcher and raises on drift.
"""
from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from tmkg import config

BASE_URL = "https://api.gleif.org/api/v1"
LEI_RECORDS_URL = f"{BASE_URL}/lei-records"

_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": config.GLEIF_USER_AGENT,
}

_CACHE_TTL_SECONDS = 30 * 24 * 3600  # LEI records are slow-moving; refresh monthly
# Bump when matching/scoring logic changes so stale cached matches are discarded.
_MATCHER_VERSION = 3
# Bump when ISIN-selection logic changes (separate cache).
_ISIN_LOGIC_VERSION = 2
# Bump when parent-fetch logic changes (separate cache).
_PARENT_LOGIC_VERSION = 1
_ISIN_PAGE_SIZE = 200
_ISIN_MAX_PAGES = 6  # bounds the rare high-instrument issuers (e.g. banks)

# Turkish ISIN instrument class is encoded in the 3rd character. Both TRA (older,
# full-ticker format) and TRE (newer, abbreviated-code format) are common/equity
# shares; TRS=rights, TRF=debt/financing, TRW=warrants are NOT the listed equity.
_EQUITY_ISIN_PREFIXES = ("TRA", "TRE")
# Methods we trust enough to auto-write to the graph (the rest are logged for
# review rather than guessed — a wrong ISIN would corrupt the price join).
_CONFIDENT_ISIN_METHODS = frozenset({"TRA+ticker", "single-equity"})

# Legal-form / descriptor tokens common in BİST issuer names. We strip a TRAILING
# run of these so the query keeps the distinctive brand part. Kept uppercased and
# diacritic-bearing to match the raw KAP titles.
_LEGAL_SUFFIX_TOKENS = {
    "A.Ş.", "A.Ş", "AŞ", "A.S.", "A.S", "AS",
    "T.A.Ş.", "T.A.Ş", "TAŞ", "T.A.S.", "T.A.S",      # Türk Anonim Şirketi
    "A.O.", "A.O", "AO", "T.A.O.", "T.A.O", "TAO",     # Anonim Ortaklığı
    "ANONİM", "ŞİRKETİ", "ŞIRKETI", "ORTAKLIĞI", "ORTAKLIGI",
    "SANAYİ", "SANAYI", "TİCARET", "TICARET", "VE",
}
# NOTE: "HOLDİNG"/"YATIRIM"/"GRUBU" are deliberately NOT stripped — they are
# distinctive ("KOÇ HOLDİNG" is a different entity from "KOÇ METALURJİ"). Only
# true legal-form and generic-descriptor boilerplate is removed.

# Tokens marking a fund / pension-fund / foundation rather than the operating
# company. Turkish brand names recur inside these (e.g. "... KOÇ HOLDİNG EMEKLİ
# VAKFI ..."), so a single-brand-token query drags them in. ASCII-folded to match
# the scoring-normalized candidate text.
_NOISE_ENTITY_MARKERS = {"FON", "FONU", "VAKFI", "VAKIF", "EMEKLI",
                         "EMEKLILIK", "PORTFOY"}
# Leading geographic/common prefixes that are NOT distinctive and that GLEIF
# frequently stores with inconsistent diacritics (e.g. "TÜRKİYE" vs "TURKIYE").
# We drop a single leading one to query on the brand tokens instead.
_LEADING_NOISE = {"TÜRKİYE", "TURKIYE", "TÜRK", "TURK", "T."}

# Tokens we never strip even if trailing, because for some names they ARE the
# brand (e.g. a company literally named "... HOLDING"). We keep at least the
# first two tokens regardless (see query_core).


@dataclass
class LeiRecord:
    lei: str
    legal_name: str
    legal_form: str | None          # ELF code, e.g. "W2SQ"
    jurisdiction: str | None        # legalAddress.country, e.g. "TR"
    registration_authority: str | None  # registration.managingLou or registeredAs
    status: str | None              # entity status ACTIVE / INACTIVE


@dataclass
class MatchResult:
    """One Company-to-GLEIF match attempt, with its provenance/score."""
    query: str
    matched: bool
    score: float
    lei: str | None = None
    gleif_name: str | None = None
    legal_form: str | None = None
    jurisdiction: str | None = None
    registration_authority: str | None = None
    candidates_seen: int = 0
    note: str = ""


# --- name normalization ----------------------------------------------------

def _ascii_fold(s: str) -> str:
    """Fold Turkish diacritics to ASCII for SCORING ONLY (not for querying)."""
    repl = {"İ": "I", "ı": "i", "Ş": "S", "ş": "s", "Ç": "C", "ç": "c",
            "Ğ": "G", "ğ": "g", "Ö": "O", "ö": "o", "Ü": "U", "ü": "u"}
    s = "".join(repl.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _norm_for_score(s: str) -> str:
    s = _ascii_fold(s).upper()
    # drop punctuation, collapse whitespace
    s = "".join(c if c.isalnum() or c.isspace() else " " for c in s)
    return " ".join(s.split())


def _brand_tokens(kap_name: str) -> list[str]:
    """Distinctive brand tokens of a KAP title: trailing legal-form run stripped
    (down to the last remaining token), one leading geographic prefix dropped.
    Diacritics preserved."""
    tokens = kap_name.split()
    while len(tokens) > 1 and tokens[-1].upper() in _LEGAL_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) > 2 and tokens[0].upper() in _LEADING_NOISE:
        tokens = tokens[1:]
    return tokens


def query_core(kap_name: str, max_tokens: int = 4) -> str:
    """Primary GLEIF query string: the leading brand tokens, diacritics intact."""
    toks = _brand_tokens(kap_name)
    return " ".join(toks[:max_tokens]).strip() or kap_name


def query_variants(kap_name: str) -> list[str]:
    """Ordered, de-duplicated query strings to try against the diacritic- and
    case-sensitive GLEIF name filter.

    GLEIF's token filter requires each query token to match a stored token
    exactly (diacritics included), and GLEIF's own data mixes diacritic and
    ASCII-folded tokens unpredictably. So we try, in order: the 2 leading brand
    tokens (most distinctive, fewest AND-constraints), then 3, then 4, then an
    ASCII-folded 2-token variant to catch records GLEIF stored without
    diacritics. Candidates are unioned across whichever variants return rows.
    """
    toks = _brand_tokens(kap_name)
    if not toks:
        return [kap_name]
    variants: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in variants:
            variants.append(s)

    # Most precise first (multi-token, diacritics intact), then ASCII-folded
    # multi-token (for records GLEIF stored without diacritics), and finally the
    # single brand token as a last resort (broadest, noisiest — disambiguated by
    # scoring + the fund/foundation penalty).
    for n in (2, 3, 4):
        add(" ".join(toks[:n]))
    add(_ascii_fold(" ".join(toks[:2])))
    add(_ascii_fold(" ".join(toks[:3])))
    add(toks[0])
    add(_ascii_fold(toks[0]))
    return variants


@dataclass
class IsinResult:
    """The chosen primary equity ISIN for an LEI, plus selection provenance."""
    isin: str | None
    method: str               # how it was chosen / why it wasn't (see pick_equity_isin)
    n_instruments: int        # total ISINs seen for the LEI
    exhausted: bool           # True if we paged through everything available
    confident: bool = False   # whether method is trusted enough to auto-write
    candidates: list[str] | None = None  # equity-class ISINs, for review when not confident


def pick_equity_isin(isins: list[str], ticker: str | None) -> tuple[str | None, str]:
    """Select the canonical BİST listed-equity ISIN from an LEI's instrument
    list. Returns (isin, method). Method doubles as a confidence label.

    A GLEIF LEI maps to ALL of an issuer's instruments. Turkish ISINs encode the
    class in the 3rd char: TRA/TRE = common shares, TRS = rights, TRF = debt,
    TRW = warrants. The listed common share is identified, in order:

      - ``TRA+ticker``  : a TRA ISIN embedding the full ticker (e.g. Garanti
        equity ``TRAGARAN91N1`` vs warrant ``TRWGRAN...``). Highest confidence.
      - ``single-equity``: exactly one equity-class (TRA/TRE) ISIN exists — it
        must be the listed line (covers newer TRE-coded issuers whose abbreviated
        code ≠ ticker, e.g. ``TREACSS00017`` for ACSEL). Confident.
      - otherwise REFUSE: ``ambiguous-multi-equity`` (several share classes, no
        type field to choose) or ``no-equity-class`` (only debt/rights/warrants)
        — these are logged with candidates for review, never guessed.
    """
    t = (ticker or "").strip().upper()
    if t:
        tra_tk = sorted(i for i in isins if i.startswith("TRA") and t in i)
        if tra_tk:
            return tra_tk[0], "TRA+ticker"
    equity = sorted(i for i in isins if i[:3] in _EQUITY_ISIN_PREFIXES)
    if len(equity) == 1:
        return equity[0], "single-equity"
    if len(equity) > 1:
        return None, "ambiguous-multi-equity"
    return None, ("no-equity-class" if isins else "none")


@dataclass
class ParentResult:
    """GLEIF Level-2 consolidation parents for one LEI.

    GLEIF parents are FILINGS-GRADE: the entity itself reports its direct and
    ultimate accounting-consolidation parent to its LOU (or files a reporting
    exception). So unlike the Level-1 name match, these need no fuzzy threshold
    — a returned parent LEI is authoritative. `note` is "ok" on success,
    "http_error:<cls>" if the lookup failed (cached either way).
    """
    lei: str
    direct_lei: str | None = None
    direct_name: str | None = None
    ultimate_lei: str | None = None
    ultimate_name: str | None = None
    note: str = "ok"


# --- adapter ---------------------------------------------------------------

class GleifAdapter:
    """Live GLEIF Level-1 lookups. Construct inside a `with` block."""

    def __init__(self, cache_dir: Path | None = None, request_pause: float = 0.25,
                 country: str = "TR") -> None:
        self.cache_dir = Path(cache_dir or (config.RAW_DOCS_PATH.parent / "cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self.cache_dir / "gleif_lookups.json"
        self._cache: dict = self._read_cache()
        self._isin_cache_file = self.cache_dir / "gleif_isins.json"
        self._isin_cache: dict = self._read_isin_cache()
        self._parent_cache_file = self.cache_dir / "gleif_parents.json"
        self._parent_cache: dict = self._read_parent_cache()
        self._http = httpx.Client(headers=_HEADERS, timeout=30.0)
        self._pause = request_pause
        self._country = country

    def __enter__(self) -> "GleifAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self._http.close()
        self._write_cache()
        self._write_isin_cache()
        self._write_parent_cache()

    # --- HTTP with backoff -------------------------------------------------

    def _get(self, params: dict, retries: int = 4) -> dict:
        return self._get_url(LEI_RECORDS_URL, params=params, retries=retries)

    def _get_url(self, url: str, params: dict | None = None, retries: int = 4) -> dict:
        delay = 1.0
        for _ in range(retries):
            r = self._http.get(url, params=params)
            if r.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()  # exhausted retries on 429 -> surface it
        return {}

    def _search_candidates(self, query: str, page_size: int = 8) -> list[LeiRecord]:
        params = {
            "filter[entity.legalName]": query,
            "filter[entity.legalAddress.country]": self._country,
            "page[size]": page_size,
        }
        data = self._get(params).get("data", [])
        out: list[LeiRecord] = []
        for r in data:
            a = r.get("attributes", {})
            e = a.get("entity", {})
            reg = a.get("registration", {})
            out.append(LeiRecord(
                lei=a.get("lei"),
                legal_name=(e.get("legalName") or {}).get("name") or "",
                legal_form=(e.get("legalForm") or {}).get("id"),
                jurisdiction=(e.get("legalAddress") or {}).get("country")
                              or e.get("jurisdiction"),
                registration_authority=reg.get("managingLou")
                              or (e.get("registeredAt") or {}).get("id"),
                status=e.get("status"),
            ))
        return out

    # --- public matching ---------------------------------------------------

    def match_company(self, kap_name: str, ticker: str | None = None,
                      threshold: float = 0.6, use_cache: bool = True) -> MatchResult:
        """Find the best GLEIF Level-1 record for a KAP company name.

        Returns a MatchResult always; `.matched` is True only when the best
        candidate's fuzzy score >= threshold. Results are cached by (name).
        """
        ck = kap_name.strip()
        if use_cache and ck in self._cache:
            return MatchResult(**self._cache[ck])

        variants = query_variants(kap_name)
        by_lei: dict[str, LeiRecord] = {}
        used_query = variants[0]
        try:
            for v in variants:
                found = self._search_candidates(v)
                for c in found:
                    if c.lei and c.lei not in by_lei:
                        by_lei[c.lei] = c
                if by_lei:
                    used_query = v
                    # one good variant is enough; stop hammering the API
                    break
        except httpx.HTTPError as exc:
            return MatchResult(query=used_query, matched=False, score=0.0,
                               note=f"http_error:{exc.__class__.__name__}")
        cands = list(by_lei.values())
        query = used_query

        # Score by BRAND-TOKEN COVERAGE, not raw string similarity: how many of
        # the company's distinctive (legal-form-stripped) tokens appear in the
        # candidate's name. This is robust to the wildly different name lengths
        # GLEIF and KAP use, and refuses to reward a single generic token match.
        target_full = set(_norm_for_score(kap_name).split())
        brand_set = {t for t in (_ascii_fold(x).upper() for x in _brand_tokens(kap_name)) if t}
        target_is_fund = bool(_NOISE_ENTITY_MARKERS & target_full)

        best: LeiRecord | None = None
        best_key = (-1.0, -1.0)
        for c in cands:
            cand_norm = _norm_for_score(c.legal_name)
            cand_toks = set(cand_norm.split())
            # Skip funds / pension funds / foundations unless the KAP entity is
            # itself one — these collide on embedded brand names.
            if (_NOISE_ENTITY_MARKERS & cand_toks) and not target_is_fund:
                continue
            if not brand_set:
                continue
            matched = brand_set & cand_toks
            cov = len(matched) / len(brand_set)
            distinctive = any(len(t) >= 4 for t in matched)
            score = cov if distinctive else cov * 0.4  # generic-only match is weak
            if c.status and c.status.upper() != "ACTIVE":
                score -= 0.05
            # tie-break: among equal coverage, prefer the closer full-name match
            seq = SequenceMatcher(None, _norm_for_score(kap_name), cand_norm).ratio()
            key = (round(score, 4), round(seq, 4))
            if key > best_key:
                best_key, best = key, c
        best_score = max(best_key[0], 0.0)

        res = MatchResult(
            query=query, matched=bool(best and best_score >= threshold),
            score=round(best_score, 3), candidates_seen=len(cands),
        )
        if best:
            res.lei = best.lei
            res.gleif_name = best.legal_name
            res.legal_form = best.legal_form
            res.jurisdiction = best.jurisdiction
            res.registration_authority = best.registration_authority
            if not res.matched:
                res.note = "below_threshold"
        else:
            res.note = "no_candidates"

        self._cache[ck] = asdict(res)
        time.sleep(self._pause)
        return res

    # --- ISIN lookup -------------------------------------------------------

    def fetch_primary_isin(self, lei: str, ticker: str | None = None,
                           use_cache: bool = True) -> IsinResult:
        """Resolve an LEI to its canonical BİST equity ISIN.

        Pages through GLEIF's per-LEI ISIN list, stopping as soon as a
        ``TRA<TICKER>`` equity ISIN is seen (so high-instrument issuers like
        banks don't force a full crawl), then applies `pick_equity_isin`.
        Cached by LEI.
        """
        if use_cache and lei in self._isin_cache:
            return IsinResult(**self._isin_cache[lei])

        collected: list[str] = []
        url = f"{LEI_RECORDS_URL}/{lei}/isins"
        params: dict | None = {"page[size]": _ISIN_PAGE_SIZE}
        exhausted = True
        early = None
        t = (ticker or "").strip().upper()
        try:
            for _ in range(_ISIN_MAX_PAGES):
                blob = self._get_url(url, params=params)
                for r in blob.get("data", []):
                    isin = (r.get("attributes") or {}).get("isin")
                    if not isin:
                        continue
                    collected.append(isin)
                    if t and isin.startswith("TRA") and t in isin:
                        early = isin  # exact equity ISIN — no need to keep paging
                if early:
                    break
                nxt = (blob.get("links") or {}).get("next")
                if not nxt:
                    break
                url, params = nxt, None  # links.next is a full URL
            else:
                exhausted = False  # hit page cap before running out
        except httpx.HTTPError as exc:
            res = IsinResult(isin=None, method=f"http_error:{exc.__class__.__name__}",
                             n_instruments=len(collected), exhausted=False)
            self._isin_cache[lei] = asdict(res)
            return res

        if early:
            isin, method = early, "TRA+ticker"
        else:
            isin, method = pick_equity_isin(collected, ticker)
        confident = method in _CONFIDENT_ISIN_METHODS
        # surface equity-class candidates when we couldn't confidently choose
        cands = None
        if not confident:
            cands = sorted(i for i in collected if i[:3] in _EQUITY_ISIN_PREFIXES)[:10]
        res = IsinResult(isin=isin, method=method, n_instruments=len(collected),
                         exhausted=exhausted, confident=confident, candidates=cands)
        self._isin_cache[lei] = asdict(res)
        time.sleep(self._pause)
        return res

    # --- Level-2 parent lookup ---------------------------------------------

    def _fetch_one_parent(self, lei: str, rel: str) -> tuple[str | None, str | None]:
        """Fetch one consolidation parent (`rel` = 'direct-parent' or
        'ultimate-parent'). Returns (parent_lei, parent_name), or (None, None)
        when GLEIF reports no such parent — a 404, which here means "no parent /
        reporting exception", NOT an error.
        """
        url = f"{LEI_RECORDS_URL}/{lei}/{rel}"
        delay = 1.0
        for _ in range(4):
            r = self._http.get(url)
            if r.status_code == 404:
                return None, None          # no reported parent — expected, common
            if r.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            a = data.get("attributes", {})
            e = a.get("entity", {})
            return a.get("lei"), (e.get("legalName") or {}).get("name")
        r.raise_for_status()
        return None, None

    def fetch_parents(self, lei: str, use_cache: bool = True) -> ParentResult:
        """Resolve an LEI's GLEIF Level-2 direct + ultimate consolidation parents.

        Filings-grade and deterministic (no fuzzy threshold). Cached by LEI.
        A 404 on either endpoint means GLEIF has no reported parent of that kind
        (the entity is a top holdco, or filed a reporting exception) — handled
        as "no parent", not an error.
        """
        if use_cache and lei in self._parent_cache:
            return ParentResult(**self._parent_cache[lei])
        try:
            d_lei, d_name = self._fetch_one_parent(lei, "direct-parent")
            u_lei, u_name = self._fetch_one_parent(lei, "ultimate-parent")
        except httpx.HTTPError as exc:
            res = ParentResult(lei=lei, note=f"http_error:{exc.__class__.__name__}")
            self._parent_cache[lei] = asdict(res)
            return res
        res = ParentResult(lei=lei, direct_lei=d_lei, direct_name=d_name,
                           ultimate_lei=u_lei, ultimate_name=u_name, note="ok")
        self._parent_cache[lei] = asdict(res)
        time.sleep(self._pause)
        return res

    # --- cache -------------------------------------------------------------

    def _read_cache(self) -> dict:
        if self._cache_file.exists():
            try:
                blob = json.loads(self._cache_file.read_text(encoding="utf-8"))
                fresh = time.time() - blob.get("fetched_at", 0) <= _CACHE_TTL_SECONDS
                same_logic = blob.get("matcher_version") == _MATCHER_VERSION
                if fresh and same_logic:
                    return blob.get("lookups", {})
            except Exception:
                pass
        return {}

    def _write_cache(self) -> None:
        self._cache_file.write_text(
            json.dumps({"fetched_at": time.time(),
                        "fetched_iso": datetime.now(timezone.utc).isoformat(),
                        "matcher_version": _MATCHER_VERSION,
                        "lookups": self._cache}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_isin_cache(self) -> dict:
        if self._isin_cache_file.exists():
            try:
                blob = json.loads(self._isin_cache_file.read_text(encoding="utf-8"))
                fresh = time.time() - blob.get("fetched_at", 0) <= _CACHE_TTL_SECONDS
                same_logic = blob.get("isin_logic_version") == _ISIN_LOGIC_VERSION
                if fresh and same_logic:
                    return blob.get("isins", {})
            except Exception:
                pass
        return {}

    def _write_isin_cache(self) -> None:
        self._isin_cache_file.write_text(
            json.dumps({"fetched_at": time.time(),
                        "fetched_iso": datetime.now(timezone.utc).isoformat(),
                        "isin_logic_version": _ISIN_LOGIC_VERSION,
                        "isins": self._isin_cache}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_parent_cache(self) -> dict:
        if self._parent_cache_file.exists():
            try:
                blob = json.loads(self._parent_cache_file.read_text(encoding="utf-8"))
                fresh = time.time() - blob.get("fetched_at", 0) <= _CACHE_TTL_SECONDS
                same_logic = blob.get("parent_logic_version") == _PARENT_LOGIC_VERSION
                if fresh and same_logic:
                    return blob.get("parents", {})
            except Exception:
                pass
        return {}

    def _write_parent_cache(self) -> None:
        self._parent_cache_file.write_text(
            json.dumps({"fetched_at": time.time(),
                        "fetched_iso": datetime.now(timezone.utc).isoformat(),
                        "parent_logic_version": _PARENT_LOGIC_VERSION,
                        "parents": self._parent_cache}, ensure_ascii=False),
            encoding="utf-8",
        )

    # --- drift guard -------------------------------------------------------

    def smoke_check(self) -> dict:
        """Verify the live endpoint + matcher still behave. Raises on drift."""
        tupras = self.match_company(
            "TÜRKİYE PETROL RAFİNERİLERİ A.Ş.", ticker="TUPRS", use_cache=False)
        assert tupras.matched, f"TÜPRAŞ no longer matches (score={tupras.score})"
        assert tupras.jurisdiction == "TR", "GLEIF country field changed"
        assert tupras.lei and len(tupras.lei) == 20, "LEI shape changed"
        isin = self.fetch_primary_isin(tupras.lei, ticker="TUPRS", use_cache=False)
        assert isin.isin == "TRATUPRS91E8", f"ISIN endpoint/selection drifted ({isin.isin})"
        return {"tupras_lei": tupras.lei, "tupras_score": tupras.score,
                "tupras_legal_form": tupras.legal_form, "tupras_isin": isin.isin}

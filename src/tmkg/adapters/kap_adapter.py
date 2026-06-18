"""KAP acquisition adapter — LIVE.

Architecture §11 warned: "Treat the endpoints as unstable. Pin kap-client,
isolate it behind your own kap_adapter, and add a smoke test so you notice fast
if KAP changes its API." That warning paid off immediately:

  FINDINGS (verified 2026-06-06 against www.kap.org.tr):
  - kap-client 1.1.1's member-list loader is BROKEN: it queries stale member-type
    codes and its row model expects field names KAP no longer returns, so
    fetch_companies()/find_company() return nothing. We therefore fetch the member
    list ourselves from the working endpoint  /tr/api/company/items/{TYPE}/{A|P}
    using the CURRENT field names (kapMemberOid, mkkMemberOid, stockCode,
    kapMemberTitle, financialType, ...).
  - kap-client's fetch_disclosures()/fetch_attachments() WORK — but the disclosure
    query keys on **mkkMemberOid**, not kapMemberOid. We delegate to it, passing
    the mkk OID, so we still benefit from its retry/back-off/typed models.

  IMPORTANT KEY DISTINCTION:
    kap_oid (kapMemberOid)  -> Company.kap_oid   (KAP's member identity)
    mkk_oid (mkkMemberOid)  -> required to query that company's disclosures

`smoke_check()` re-verifies both halves and raises if KAP drifts again.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tmkg import config

BASE_URL = "https://www.kap.org.tr"
COMPANY_ITEMS_URL = f"{BASE_URL}/tr/api/company/items"  # + /{memberType}/{A|P}
DISCLOSURE_DETAIL_URL = f"{BASE_URL}/tr/Bildirim"        # + /{index}

# Member-type codes that currently return companies (verified 2026-06-06).
# IGS = listed issuers (the BİST core); the rest cover banks/intermediaries/other.
DEFAULT_MEMBER_TYPES = ("IGS",)
ALL_MEMBER_TYPES = ("IGS", "DK", "YK", "PYS", "BDK", "DDK", "DCS", "KVH")

_HEADERS = {
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/tr/bildirim-sorgu",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

_CACHE_TTL_SECONDS = 7 * 24 * 3600  # refresh member list weekly (architecture §11)


@dataclass
class CompanyRef:
    kap_oid: str          # kapMemberOid -> Company.kap_oid
    mkk_oid: str | None    # mkkMemberOid -> needed to fetch disclosures
    ticker: str | None     # stockCode (may be comma-separated)
    name: str              # kapMemberTitle
    member_type: str       # IGS / DK / ...
    financial_type: str | None
    city: str | None
    is_listed: bool

    @property
    def primary_ticker(self) -> str | None:
        if not self.ticker:
            return None
        return self.ticker.split(",")[0].strip().upper() or None


def _row_to_ref(raw: dict, member_type: str) -> CompanyRef:
    stock = (raw.get("stockCode") or "").strip()
    return CompanyRef(
        kap_oid=raw.get("kapMemberOid"),
        mkk_oid=raw.get("mkkMemberOid"),
        ticker=stock or None,
        name=raw.get("kapMemberTitle") or "",
        member_type=raw.get("kapMemberType") or member_type,
        financial_type=raw.get("financialType"),
        city=raw.get("cityName"),
        is_listed=bool(stock),
    )


class KapAdapter:
    """Live KAP acquisition. Construct inside a `with` block."""

    def __init__(self, cache_dir: Path | None = None, request_pause: float = 0.4) -> None:
        self.cache_dir = Path(cache_dir or (config.RAW_DOCS_PATH.parent / "cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._members_file = self.cache_dir / "kap_members.json"
        self._http = httpx.Client(headers=_HEADERS, timeout=30.0)
        self._kap = None  # lazy kap-client for disclosures/attachments
        self._pause = request_pause

    def __enter__(self) -> "KapAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self._http.close()
        if self._kap is not None:
            self._kap.__exit__(*exc)

    # --- member list (our own fetch; kap-client is broken here) ------------

    def _kap_client(self):
        if self._kap is None:
            from kap_client import Kap
            self._kap = Kap().__enter__()
        return self._kap

    def fetch_members(
        self,
        member_types: tuple[str, ...] = DEFAULT_MEMBER_TYPES,
        include_pref: bool = False,
        refresh: bool = False,
    ) -> list[CompanyRef]:
        """Return KAP member companies, cached on disk and refreshed weekly."""
        cached = self._read_member_cache()
        if cached is not None and not refresh:
            return cached

        flags = ("A", "P") if include_pref else ("A",)
        seen: dict[str, CompanyRef] = {}
        for mt in member_types:
            for flag in flags:
                rows = self._http.get(f"{COMPANY_ITEMS_URL}/{mt}/{flag}")
                rows.raise_for_status()
                for raw in rows.json():
                    ref = _row_to_ref(raw, mt)
                    if ref.kap_oid:
                        seen[ref.kap_oid] = ref
                time.sleep(self._pause)
        refs = list(seen.values())
        self._write_member_cache(refs)
        return refs

    def find(self, ticker: str, members: list[CompanyRef] | None = None) -> CompanyRef:
        members = members or self.fetch_members()
        t = ticker.strip().upper()
        for m in members:
            if m.ticker and t in [c.strip().upper() for c in m.ticker.split(",")]:
                return m
        raise KeyError(f"ticker {ticker!r} not found in KAP member list")

    def _read_member_cache(self) -> list[CompanyRef] | None:
        if not self._members_file.exists():
            return None
        blob = json.loads(self._members_file.read_text(encoding="utf-8"))
        if time.time() - blob.get("fetched_at", 0) > _CACHE_TTL_SECONDS:
            return None
        return [CompanyRef(**r) for r in blob["members"]]

    def _write_member_cache(self, refs: list[CompanyRef]) -> None:
        self._members_file.write_text(
            json.dumps(
                {"fetched_at": time.time(),
                 "fetched_iso": datetime.now(timezone.utc).isoformat(),
                 "members": [asdict(r) for r in refs]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # --- disclosures / attachments (delegate to kap-client; pass mkk_oid) --

    def fetch_disclosures(self, mkk_oid: str, start: str, end: str, subject_oids=None):
        """Disclosure METADATA for one company. NOTE: pass the mkkMemberOid.
        Date range should sit within one calendar year — loop years for history."""
        return self._kap_client().fetch_disclosures(
            mkk_oid, start, end, subject_oids=subject_oids
        )

    def fetch_attachments(self, disclosure_index: int):
        return self._kap_client().fetch_attachments(disclosure_index)

    def fetch_detail_html(self, disclosure_index: int | str) -> str:
        """Raw disclosure detail page — store on disk so LLM extraction (Phase 3)
        can re-run without re-hitting KAP (architecture §11)."""
        r = self._http.get(f"{DISCLOSURE_DETAIL_URL}/{disclosure_index}")
        r.raise_for_status()
        return r.text

    # --- drift guard -------------------------------------------------------

    def smoke_check(self) -> dict:
        """Verify both halves of the live API still behave. Raises on drift."""
        members = self.fetch_members(refresh=True)
        assert members, "member list empty — company/items endpoint changed"
        kchol = self.find("KCHOL", members)
        assert kchol.mkk_oid, "KCHOL has no mkk_oid — member row shape changed"
        disc = self.fetch_disclosures(kchol.mkk_oid, "2025-01-01", "2025-03-31")
        assert disc, "disclosure query returned nothing — byCriteria changed"
        return {"members": len(members), "kchol_mkk_oid": kchol.mkk_oid,
                "kchol_disclosures_q1_2025": len(disc)}

"""EVDS adapter — TCMB macro series (CPI/TÜFE), the CPI-real-TRY cross-check source.

The ingestion layer is the ONLY place that touches the network (§4). This adapter
fetches macro time series from the Turkish Central Bank's EVDS over plain httpx —
reproducible headless Python, same posture as the Matriks adapter.

RESOLVED evds3 REST contract (live-verified 2026-06-20; see BUILD_LOG):
  EVDS migrated evds2 → evds3. The legacy ``evds2.tcmb.gov.tr/service/evds/...``
  path is DEAD — it unconditionally 302-redirects to the evds3 React SPA, and
  ``evds3.tcmb.gov.tr/service/evds/...`` is caught by the SPA and returns HTML, not
  data. The live programmatic API moved to the gateway prefix ``/igmevdsms-dis``:

    GET  https://evds3.tcmb.gov.tr/igmevdsms-dis/series=<CODE>&startDate=DD-MM-YYYY&endDate=DD-MM-YYYY&type=json
    headers:  key: <EVDS_API_KEY>          (post-2024 the key is a HEADER, not a query param)
    response: {"totalCount": N,
               "items": [{"Tarih": "2023-1", "TP_FG_J0": "1203.48000000",
                          "UNIXTIME": {"$numberLong": "1672520400"}}, ...]}

  The ``items`` field carrying the values is the series code with dots → underscores
  (``TP.FG.J0`` -> ``TP_FG_J0``). ``Tarih`` is the period label ``YYYY-M`` (monthly).

A wrong endpoint serves the SPA HTML with HTTP 200 — so ``fetch`` treats an HTML /
non-JSON body as ``SourceUnreachable`` (fail loud, §4), never parsing it as data.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import httpx

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.base import IngestionAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

# Headline CPI: TÜFE genel endeks (all items, 2003=100), monthly. The real-TRY
# deflator for the total-return cross-check (CLAUDE.md §5).
CPI_TUFE_SERIES = "TP.FG.J0"
CPI_TUFE_FACTOR = "CPI_TUFE"

# Foreign-flow factor (§5 driver) — TCMB "Menkul Kıymet İstatistikleri" weekly non-resident
# holdings. M7 = "2.1.1 Hisse Senedi" net değişim (weekly net non-resident equity FLOW, USD mn);
# M1 = "1.1.1 Hisse Senedi" stok (the holdings LEVEL). Tarih = the Friday week-ending; the
# weekly stats are released the following Thursday (~6-day lag) -> PIT knowledge_date.
FOREIGN_FLOW_SERIES = "TP.MKNETHAR.M7"
FOREIGN_FLOW_FACTOR = "FFLOW"
FOREIGN_FLOW_STOCK_SERIES = "TP.MKNETHAR.M1"
FOREIGN_FLOW_STOCK_FACTOR = "FFLOW_STOCK"
WEEKLY_RELEASE_LAG_DAYS = 6

_DEFAULT_BASE = "https://evds3.tcmb.gov.tr/igmevdsms-dis"


def _evds_date(s: str) -> str:
    """Accept an ISO ``YYYY-MM-DD`` (or already-EVDS ``DD-MM-YYYY``) and return the
    ``DD-MM-YYYY`` the EVDS query string wants. No other formats are guessed."""
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(f"unparseable date {s!r}; want YYYY-MM-DD or DD-MM-YYYY")
    if len(parts[0]) == 4:  # ISO YYYY-MM-DD
        y, m, d = parts
        return f"{int(d):02d}-{int(m):02d}-{y}"
    return s  # already DD-MM-YYYY


def _item_field(series: str) -> str:
    """EVDS names the value column after the series code with dots → underscores."""
    return series.replace(".", "_")


def _release_knowledge_date(ref_year: int, ref_month: int) -> date:
    """PIT knowledge_date for a monthly CPI reading: TÜİK releases month M's index on
    ~the 3rd of month M+1, so a backtest cannot know it before then. Conventional
    release day (3rd of the following month); the exact TÜİK-calendar date is a later
    refinement (BUILD_LOG open thread) — 3rd is the standing convention, not a guess."""
    y, m = (ref_year + 1, 1) if ref_month == 12 else (ref_year, ref_month + 1)
    return date(y, m, 3)


class EvdsAdapter(IngestionAdapter):
    source_name = "evds"

    def __init__(self, *, timeout: float = 40.0) -> None:
        self.timeout = timeout
        self.api_key = os.getenv("EVDS_API_KEY", "")
        self.base_url = os.getenv("EVDS_BASE_URL", _DEFAULT_BASE).rstrip("/")

    # --- pure helpers (testable offline) ----------------------------------
    def _series_url(self, series: str, start: str, end: str, rtype: str) -> str:
        return (
            f"{self.base_url}/series={series}"
            f"&startDate={_evds_date(start)}&endDate={_evds_date(end)}&type={rtype}"
        )

    def _headers(self) -> dict[str, str]:
        """EVDS authenticates on a single ``key`` header (post-2024-04-05 change)."""
        return {"key": self.api_key}

    # --- network ----------------------------------------------------------
    def fetch(self, series: str, *, start: str, end: str, rtype: str = "json") -> dict:
        """GET one EVDS series over [start, end] and return the parsed payload dict.

        Raises ``SourceUnreachable`` on missing creds / transport / HTTP error /
        the SPA-HTML fallback (a wrong endpoint serves index.html at HTTP 200) —
        NEVER returns placeholder/interpolated data (§4). ``ContractDrift`` if a JSON
        body lacks the expected ``items`` envelope.
        """
        if not self.api_key:
            raise SourceUnreachable(
                "EVDS_API_KEY missing. Load .env: `set -a && source .env && set +a`."
            )
        url = self._series_url(series, start, end, rtype)
        try:
            resp = httpx.get(
                url, headers=self._headers(), timeout=self.timeout, follow_redirects=True
            )
        except httpx.HTTPError as e:  # network / DNS / timeout
            raise SourceUnreachable(f"EVDS {series} GET failed: {e}") from e
        if resp.status_code != 200:
            raise SourceUnreachable(
                f"EVDS {series} HTTP {resp.status_code}: {resp.text[:200]}"
            )
        ctype = resp.headers.get("content-type", "")
        body = resp.text.lstrip()
        # The dead legacy path / a moved endpoint returns the React SPA at HTTP 200.
        # That is an unreachable API, not data — fail loud, never parse HTML as a series.
        if "json" not in ctype.lower() or body[:1] == "<":
            raise SourceUnreachable(
                f"EVDS {series}: non-JSON response (content-type {ctype!r}); the API "
                f"endpoint likely moved again or served the SPA. Body: {body[:120]!r}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise ContractDrift(f"EVDS {series}: undecodable JSON") from e
        if not isinstance(payload, dict) or "items" not in payload:
            raise ContractDrift(
                f"EVDS {series}: unexpected envelope (no 'items'): {str(payload)[:200]}"
            )
        return payload

    # --- parser (EVDS payload -> L2 `factors`-shaped rows) ----------------
    @staticmethod
    def parse_cpi(
        payload: dict, *, series: str = CPI_TUFE_SERIES, factor: str = CPI_TUFE_FACTOR
    ) -> list[dict]:
        """``{totalCount, items:[{Tarih, <CODE>}...]}`` -> ``factors``-schema rows.

        One row per monthly reading: ``bar_date`` = first day of the reference month
        (``Tarih`` ``YYYY-M``), ``value`` = the index level, ``knowledge_date`` = the
        TÜİK release (~3rd of the next month, PIT-honest). ``ret`` is left null —
        inflation is derived downstream by the return constructor, never stored here.

        Items with a blank/non-numeric value are DROPPED, never coerced to a guessed
        number (§4). Raises ``ContractDrift`` if no valid reading survives (the value
        column is missing -> the contract changed).
        """
        field = _item_field(series)
        rows: list[dict] = []
        for it in payload.get("items", []):
            tarih = (it.get("Tarih") or "").strip()
            raw = it.get(field)
            if not tarih or "-" not in tarih:
                continue  # no period label -> drop, never guess
            try:
                y, m = (int(x) for x in tarih.split("-")[:2])
                val = float(raw)
            except (TypeError, ValueError):
                continue  # blank / "ND" / non-numeric -> drop (§4)
            rows.append(
                {
                    "factor": factor,
                    "bar_date": date(y, m, 1),
                    "value": val,
                    "ret": None,
                    "knowledge_date": _release_knowledge_date(y, m),
                    "source": EvdsAdapter.source_name,
                }
            )
        if not rows:
            raise ContractDrift(
                f"EVDS parse_cpi: no valid readings for {series} (field {field!r} "
                f"absent or all values blank) — contract drift, refusing to fabricate."
            )
        return rows

    @staticmethod
    def parse_weekly_series(
        payload: dict, *, series: str, factor: str,
        release_lag_days: int = WEEKLY_RELEASE_LAG_DAYS,
    ) -> list[dict]:
        """``{items:[{Tarih:'DD-MM-YYYY', YEARWEEK, <CODE>}...]}`` -> ``factors``-schema rows.

        For a WEEKLY series (e.g. the non-resident net-equity-flow factor): ``bar_date`` =
        the reference ``Tarih`` (the Friday the week ends on); ``value`` = the reading;
        ``knowledge_date`` = ``bar_date + release_lag_days`` (the weekly stats are released
        ~the following Thursday — PIT-honest, so a backtest cannot see a week's flow before
        it was published). ``ret`` is left null (the return constructor handles it; a flow
        factor uses ``series.LEVEL``). Blank/non-numeric items are DROPPED, never guessed
        (§4); ``ContractDrift`` if no valid reading survives.
        """
        field = _item_field(series)
        rows: list[dict] = []
        for it in payload.get("items", []):
            tarih = (it.get("Tarih") or "").strip()
            raw = it.get(field)
            try:
                bar = datetime.strptime(tarih, "%d-%m-%Y").date()
                val = float(raw)
            except (TypeError, ValueError):
                continue  # no/blank date or value -> drop (§4)
            rows.append(
                {
                    "factor": factor,
                    "bar_date": bar,
                    "value": val,
                    "ret": None,
                    "knowledge_date": bar + timedelta(days=release_lag_days),
                    "source": EvdsAdapter.source_name,
                }
            )
        if not rows:
            raise ContractDrift(
                f"EVDS parse_weekly_series: no valid readings for {series} (field {field!r} "
                f"absent or all values blank) — contract drift, refusing to fabricate."
            )
        return rows

    # --- drift guard ------------------------------------------------------
    def smoke_check(self) -> None:
        """Re-fetch the committed CPI golden window and prove the live values still
        match field-for-field. Published CPI is never revised, so this is a real
        VALUE anchor (ContractDrift on any mismatch). Writes a JSON audit report (§4).
        """
        import json
        import pathlib

        golden = (
            pathlib.Path(__file__).resolve().parents[3]
            / "tests" / "golden" / "evds" / "cpi_TP.FG.J0_2023.json"
        )
        doc = json.loads(golden.read_text())
        prov = doc["_provenance"]
        p = prov["params"]
        live = self.fetch(p["series"], start=p["startDate"], end=p["endDate"], rtype=p["type"])

        gold_items = {i["Tarih"]: i for i in doc["data"]["items"]}
        live_items = {i["Tarih"]: i for i in live.get("items", [])}
        field = _item_field(p["series"])
        drift: list[str] = []
        for tarih, gi in gold_items.items():
            li = live_items.get(tarih)
            if li is None:
                drift.append(f"{tarih}: missing in live")
                continue
            if float(li.get(field, "nan")) != float(gi[field]):
                drift.append(f"{tarih}: {gi[field]} != {li.get(field)}")

        write_run_report(
            "evds_smoke",
            {
                "source": self.source_name,
                "base_url": self.base_url,
                "series": p["series"],
                "window": f"{p['startDate']}..{p['endDate']}",
                "value_matched": len(gold_items) - len(drift),
                "drift": drift[:10],
            },
        )
        if drift:
            raise ContractDrift(f"EVDS CPI contract drift on {p['series']}: {drift[:10]}")

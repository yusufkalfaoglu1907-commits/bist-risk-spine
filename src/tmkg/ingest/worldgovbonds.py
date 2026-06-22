"""WorldGovernmentBonds adapter — Turkey 5y sovereign CDS (the rates/CDS rung, W3).

The ingestion layer is the ONLY place that touches the network (§4). Turkey 5y CDS is the
one factor with no API on Matriks/EVDS/FRED; the data-sourcing plan (data-sourcing-v2 §7 /
W3) names WorldGovernmentBonds as the free daily source (bps) — and explicitly rules out
Investing.com (ToS). This adapter hits WGB's **own public chart JSON API** (the endpoint
its historical-CDS page calls), not an HTML scrape — reproducible and structured.

Contract (verified live 2026-06-22):
    POST {base}/wp-json/common/v1/historical
        headers: Content-Type: application/json, Origin/Referer = the WGB site
                 (the endpoint returns HTTP 403 "invalid origin" without them)
        body: {"GLOBALVAR": {FUNCTION:"CDS", COUNTRY1:{SYMBOL:"13",...Turkey},
                              OBJ1:{DURATA:60 (= 5y in months)}, DATE_RIF:"2099-12-31", ...}}
    response: {"success":true, "result":{"num":N, "quote":{"1":{"CLOSE_VAL":272.94,
              "DATA_VAL":"2015-12-15",...}, ...}}}  -- daily CDS in bps back to 2015-12.
"""
from __future__ import annotations

from datetime import date

import httpx

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.base import IngestionAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

# Turkey 5y CDS — the rates/CDS-rung credit factor. SYMBOL 13 = Turkey, DURATA 60 months.
TURKEY_CDS_FACTOR = "TRCDS5Y"
_TURKEY = {"SYMBOL": "13", "PAESE": "Turkey", "PAESE_UPPERCASE": "TURKEY",
           "BANDIERA": "tr", "URL_PAGE": "turkey"}

_DEFAULT_BASE = "https://www.worldgovernmentbonds.com"
_HIST_PATH = "/wp-json/common/v1/historical"


def _globalvar(*, function: str, country: dict, durata: int, durata_string: str) -> dict:
    """The page's ``jsGlobalVars`` object the endpoint expects (FUNCTION + country + tenor)."""
    return {
        "JS_VARIABLE": "jsGlobalVars",
        "FUNCTION": function,
        "DOMESTIC": True,
        "ENDPOINT": _DEFAULT_BASE + _HIST_PATH,
        "DATE_RIF": "2099-12-31",
        "DEBUG": False,
        "OBJ": {"UNIT": "", "DECIMAL": 2, "UNIT_DELTA": "%", "DECIMAL_DELTA": 2},
        "COUNTRY1": country,
        "COUNTRY2": None,
        "OBJ1": {"DURATA_STRING": durata_string, "DURATA": durata},
        "OBJ2": None,
    }


class WorldGovBondsAdapter(IngestionAdapter):
    source_name = "worldgovbonds"

    def __init__(self, *, timeout: float = 40.0) -> None:
        self.timeout = timeout
        self.base_url = _DEFAULT_BASE

    def _headers(self) -> dict[str, str]:
        # The endpoint enforces a same-origin check (403 "invalid origin" without these).
        return {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/cds-historical-data/turkey/5-years/",
        }

    # --- network ----------------------------------------------------------
    def fetch(
        self, *, function: str = "CDS", country: dict | None = None,
        durata: int = 60, durata_string: str = "5 Years",
    ) -> dict:
        """POST the historical request and return the ``result`` object.

        Raises ``SourceUnreachable`` on transport/HTTP/non-success, ``ContractDrift`` if
        the envelope lacks ``result.quote`` — NEVER returns placeholder data (§4).
        """
        body = {"GLOBALVAR": _globalvar(
            function=function, country=country or _TURKEY,
            durata=durata, durata_string=durata_string)}
        url = self.base_url + _HIST_PATH
        try:
            resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as e:  # network / DNS / timeout
            raise SourceUnreachable(f"WGB {function} POST failed: {e}") from e
        if resp.status_code != 200:
            raise SourceUnreachable(f"WGB {function} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ContractDrift(f"WGB {function}: undecodable JSON") from e
        if not isinstance(payload, dict) or not payload.get("success"):
            raise SourceUnreachable(f"WGB {function}: non-success response: {str(payload)[:200]}")
        result = payload.get("result")
        if not isinstance(result, dict) or "quote" not in result:
            raise ContractDrift(f"WGB {function}: no result.quote: {str(payload)[:200]}")
        return result

    # --- parser (WGB result -> L2 `factors`-shaped rows) ------------------
    @staticmethod
    def parse_cds(
        result: dict, *, factor: str = TURKEY_CDS_FACTOR,
        start: str | None = None, end: str | None = None,
    ) -> list[dict]:
        """``result.quote {id:{CLOSE_VAL, DATA_VAL}}`` -> ``factors``-schema rows.

        ``bar_date`` = ``DATA_VAL``, ``value`` = ``CLOSE_VAL`` (CDS in bps). The endpoint
        returns the full history; ``start``/``end`` (ISO) clip it to the wanted window.
        ``knowledge_date = bar_date`` — a daily CDS close is known end-of-day. ``ret`` left
        null (a rate level -> ``series.DIFF`` at read time). Non-numeric/dateless quotes are
        DROPPED (§4); ``ContractDrift`` if none survive.
        """
        lo = date.fromisoformat(start) if start else None
        hi = date.fromisoformat(end) if end else None
        rows: list[dict] = []
        for q in result.get("quote", {}).values():
            d = (q.get("DATA_VAL") or "").strip()
            raw = q.get("CLOSE_VAL")
            try:
                bar = date.fromisoformat(d)
                val = float(raw)
            except (TypeError, ValueError):
                continue  # dateless / non-numeric -> drop, never guess
            if (lo and bar < lo) or (hi and bar > hi):
                continue
            rows.append({
                "factor": factor,
                "bar_date": bar,
                "value": val,
                "ret": None,
                "knowledge_date": bar,
                "source": WorldGovBondsAdapter.source_name,
            })
        if not rows:
            raise ContractDrift(
                f"WGB parse_cds: no valid quotes for {factor} in window "
                f"[{start}..{end}] — contract drift or empty window, refusing to fabricate."
            )
        return rows

    # --- drift guard ------------------------------------------------------
    def smoke_check(self) -> None:
        """Re-fetch and prove the committed golden CDS anchors still match. Past CDS closes
        are immutable, so this is a real VALUE anchor (ContractDrift on mismatch). Writes a
        JSON audit report (§4).
        """
        import json
        import pathlib

        golden = (
            pathlib.Path(__file__).resolve().parents[3]
            / "tests" / "golden" / "worldgovbonds" / "turkey_cds_5y_anchors.json"
        )
        doc = json.loads(golden.read_text())
        anchors = doc["data"]["anchors"]  # {DATA_VAL: CLOSE_VAL} fixed past closes
        result = self.fetch()
        live = {q.get("DATA_VAL"): q.get("CLOSE_VAL")
                for q in result.get("quote", {}).values()}
        drift: list[str] = []
        for d, gv in anchors.items():
            lv = live.get(d)
            if lv is None:
                drift.append(f"{d}: missing in live")
            elif abs(float(lv) - float(gv)) > 1e-6:
                drift.append(f"{d}: {gv} != {lv}")

        write_run_report(
            "worldgovbonds_smoke",
            {
                "source": self.source_name,
                "base_url": self.base_url,
                "factor": TURKEY_CDS_FACTOR,
                "anchors_checked": len(anchors),
                "value_matched": len(anchors) - len(drift),
                "drift": drift[:10],
            },
        )
        if drift:
            raise ContractDrift(f"WGB contract drift on {TURKEY_CDS_FACTOR}: {drift[:10]}")

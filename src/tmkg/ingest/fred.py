"""FRED adapter — St. Louis Fed macro series (VIX and other global-risk factors).

The ingestion layer is the ONLY place that touches the network (§4). This adapter
fetches macro time series from FRED over plain httpx — reproducible headless Python,
same posture as the EVDS and Matriks adapters.

API-version decision (2026-06-21, live-verified): FRED exposes a legacy API
(``api.stlouisfed.org/fred/...``, key as the ``api_key`` QUERY PARAM) and a newer
"Version 2" geared at **bulk / full-history** pulls (Bearer token). Our use is the
opposite of bulk — a handful of daily series (VIX, …) pulled incrementally — so we use
the **legacy api_key API**: confirmed working with our key, a dead-simple one-GET JSON
contract, and the same shape as the other adapters. The 32-char account key works for
both, so a later switch to v2 (if we ever need bulk) is free. NB the Bearer header does
NOT work on the legacy endpoint (it returns HTTP 400 ``api_key is not set``).

Contract (verified live against VIXCLS, 2026-06-21):
    GET {base}/series/observations?series_id=<S>&api_key=<KEY>&file_type=json
        &observation_start=YYYY-MM-DD&observation_end=YYYY-MM-DD
    response: {"observations": [{"date": "2025-03-17", "value": "20.51", ...}, ...]}
      -> FRED writes a missing reading as the string ``"."`` (dropped here, never
         coerced to a number — §4).
"""
from __future__ import annotations

import os
from datetime import date

import httpx

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.base import IngestionAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

# CBOE Volatility Index, daily close — the global-risk factor (design §7.1 "VIX (FRED)").
VIX_SERIES = "VIXCLS"
VIX_FACTOR = "VIX"

_DEFAULT_BASE = "https://api.stlouisfed.org/fred"
_FRED_MISSING = "."  # FRED's sentinel for "no observation"


class FredAdapter(IngestionAdapter):
    source_name = "fred"

    def __init__(self, *, timeout: float = 40.0) -> None:
        self.timeout = timeout
        self.api_key = os.getenv("FRED_API_KEY", "")
        self.base_url = os.getenv("FRED_BASE_URL", _DEFAULT_BASE).rstrip("/")

    # --- pure helpers (testable offline) ----------------------------------
    def _obs_params(self, series: str, start: str, end: str) -> dict[str, str]:
        return {
            "series_id": series,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start,
            "observation_end": end,
        }

    # --- network ----------------------------------------------------------
    def fetch(self, series: str, *, start: str, end: str) -> dict:
        """GET one FRED series over [start, end] and return the parsed payload dict.

        Raises ``SourceUnreachable`` on missing key / transport / HTTP error, and
        ``ContractDrift`` if the JSON body lacks the ``observations`` envelope —
        NEVER returns placeholder/interpolated data (§4).
        """
        if not self.api_key:
            raise SourceUnreachable(
                "FRED_API_KEY missing. Load .env: `set -a && source .env && set +a`."
            )
        url = f"{self.base_url}/series/observations"
        try:
            resp = httpx.get(url, params=self._obs_params(series, start, end),
                             timeout=self.timeout)
        except httpx.HTTPError as e:  # network / DNS / timeout
            raise SourceUnreachable(f"FRED {series} GET failed: {e}") from e
        if resp.status_code != 200:
            raise SourceUnreachable(
                f"FRED {series} HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise ContractDrift(f"FRED {series}: undecodable JSON") from e
        if not isinstance(payload, dict) or "observations" not in payload:
            raise ContractDrift(
                f"FRED {series}: unexpected envelope (no 'observations'): {str(payload)[:200]}"
            )
        return payload

    # --- parser (FRED payload -> L2 `factors`-shaped rows) ----------------
    @staticmethod
    def parse_observations(
        payload: dict, *, series: str = VIX_SERIES, factor: str = VIX_FACTOR
    ) -> list[dict]:
        """``{observations:[{date, value}...]}`` -> ``factors``-schema rows.

        ``bar_date`` = observation date, ``value`` = the level, ``ret`` left null
        (returns are an M2 read-time concern). ``knowledge_date = bar_date``: VIX is a
        market index whose daily close is known end-of-that-day and is **never revised**,
        so the observation date is also its knowledge date. (A *revisable* macro series —
        GDP etc. — would instead need ALFRED real-time vintages; not used here.)

        FRED's ``"."`` missing-value sentinel and any non-numeric value are DROPPED, never
        coerced (§4). Raises ``ContractDrift`` if no valid reading survives.
        """
        rows: list[dict] = []
        for obs in payload.get("observations", []):
            d = (obs.get("date") or "").strip()
            raw = obs.get("value")
            if not d or raw is None or str(raw).strip() == _FRED_MISSING:
                continue  # no date / FRED-missing -> drop, never guess
            try:
                val = float(raw)
                bar = date.fromisoformat(d)
            except (TypeError, ValueError):
                continue  # non-numeric value / unparseable date -> drop (§4)
            rows.append(
                {
                    "factor": factor,
                    "bar_date": bar,
                    "value": val,
                    "ret": None,
                    "knowledge_date": bar,
                    "source": FredAdapter.source_name,
                }
            )
        if not rows:
            raise ContractDrift(
                f"FRED parse_observations: no valid readings for {series} — "
                f"contract drift or empty window, refusing to fabricate."
            )
        return rows

    # --- drift guard ------------------------------------------------------
    def smoke_check(self) -> None:
        """Re-fetch the committed VIX golden window and prove the live values still
        match field-for-field. VIX is never revised, so this is a real VALUE anchor
        (ContractDrift on any mismatch). Writes a JSON audit report (§4).
        """
        import json
        import pathlib

        golden = (
            pathlib.Path(__file__).resolve().parents[3]
            / "tests" / "golden" / "fred" / "vixcls_2025-03.json"
        )
        doc = json.loads(golden.read_text())
        p = doc["_provenance"]["params"]
        live = self.fetch(p["series_id"], start=p["observation_start"],
                          end=p["observation_end"])

        gold = {o["date"]: o["value"] for o in doc["data"]["observations"]}
        live_obs = {o["date"]: o["value"] for o in live.get("observations", [])}
        drift: list[str] = []
        for d, gv in gold.items():
            lv = live_obs.get(d)
            if lv is None:
                drift.append(f"{d}: missing in live")
            elif float(lv) != float(gv):
                drift.append(f"{d}: {gv} != {lv}")

        write_run_report(
            "fred_smoke",
            {
                "source": self.source_name,
                "base_url": self.base_url,
                "series": p["series_id"],
                "window": f"{p['observation_start']}..{p['observation_end']}",
                "value_matched": len(gold) - len(drift),
                "drift": drift[:10],
            },
        )
        if drift:
            raise ContractDrift(f"FRED contract drift on {p['series_id']}: {drift[:10]}")

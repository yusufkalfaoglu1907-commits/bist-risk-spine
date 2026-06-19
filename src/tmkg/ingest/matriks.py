"""Matriks adapter — the verified data spine (data-sourcing-v2.md, Matriks_MCP_Dokumani.pdf).

The ingestion layer is the ONLY place that touches the network (§4). This adapter
uses the **REST API** (plain httpx) — not MCP — because ingestion is reproducible
Python that runs headless, and MCP tools can only be invoked by an interactive
agent, not from a module. The header-auth MCP (.mcp.json) is for interactive
agent queries only.

REST contract (verified live 2026-06-19 against https://mcp.matriks.ai/openapi.json;
see decisions/ADR-0002):
    POST  {MATRIKS_REST_URL}/tools/{tool}/execute       (tool slugs are camelCase)
    headers:
        X-API-Key:    "<MATRIKS_USERNAME>:<MATRIKS_API_KEY>"   (combined, no spaces)
        Content-Type: application/json
    body: {<params>}     e.g. {"action": "price", "symbol": "THYAO"}
    response envelope: {"content": [{"type": "text", "text": "<json-string>"}],
                        "isError": bool, "_meta": {...}}
      -> the real payload is json.loads(content[0]["text"]).

WORKING-TRANSPORT FINDING (ADR-0002): auth is the single ``X-API-Key`` header in
``<username>:<key>`` form. Sending an additional ``X-Client-ID`` header makes the
gateway return HTTP 500 ``Authentication failed`` — so it is deliberately omitted.
"""
from __future__ import annotations

import json
import math
import os
import pathlib

import httpx

from tmkg.ingest.audit import write_run_report
from tmkg.ingest.base import IngestionAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

_REPO = pathlib.Path(__file__).resolve().parents[3]
_GOLDEN = _REPO / "tests" / "golden" / "matriks"

# Logical name -> REST tool slug (camelCase, confirmed against openapi.json 2026-06-19).
# The golden samples' _provenance.tool already use these camelCase slugs, and
# _rest_endpoint falls back to the tool name itself, so callers may pass either.
TOOL_PATHS = {
    "market_price": "marketPrice",
    "historical_data": "historicalData",
    "fundamental_analysis": "fundamentalAnalysis",
    "institutional_flow": "institutionalFlow",
    "news_and_events": "newsAndEvents",
    "symbol_search": "symbolSearch",
}

# Keys that are capture annotations or live-only additive metadata — ignored when
# value-matching a golden sample against a live response (they are not market data).
_IGNORE_KEYS = frozenset({"_note", "note", "timestamp", "requestedStart", "requestedEnd"})

# The drift-guard VALUE anchors: raw, immutable, single-call captures whose live
# re-fetch must still match field-for-field (the real contract-drift teeth). The
# remaining single-call goldens were curated/reshaped at capture or are volatile
# (news feed; institutionalFlow returns the latest month regardless of period) —
# they are checked for REACHABILITY only, never a fabricated value-match. Composite
# goldens (``_provenance.params`` is a list) are M1/M2 reconciliation anchors and
# are skipped by the smoke gate. See BUILD_LOG.md 2026-06-19 / MANIFEST.md.
_VALUE_ANCHORS = frozenset({"ohlcv_EREGL_2024-11.json", "ohlcv_ASELS_2023-08.json"})

_REL_TOL = 1e-6


def _golden_contains(gold, live, path: str = "") -> list[str]:
    """Return the list of paths where ``gold`` is NOT reproduced in ``live``.

    Containment, not equality: every value pinned in the golden sample must appear
    and match in the (superset) live response. Live-only additive fields are fine.
    List elements are matched by a stable id key when present (so benchmark/period
    reordering does not register as drift). Empty list => the golden matches.
    """
    bad: list[str] = []
    if isinstance(gold, dict):
        if not isinstance(live, dict):
            return [f"{path or '.'}: expected dict, live is {type(live).__name__}"]
        for k, v in gold.items():
            if k in _IGNORE_KEYS:
                continue
            if k not in live:
                bad.append(f"{path}.{k}: missing in live")
                continue
            bad += _golden_contains(v, live[k], f"{path}.{k}")
    elif isinstance(gold, list):
        if not isinstance(live, list):
            return [f"{path or '.'}: expected list, live is {type(live).__name__}"]
        idk = _id_key(gold)
        if idk:
            lmap = {e.get(idk): e for e in live if isinstance(e, dict)}
            for e in gold:
                key = e.get(idk)
                if key not in lmap:
                    bad.append(f"{path}[{idk}={key}]: missing in live")
                    continue
                bad += _golden_contains(e, lmap[key], f"{path}[{idk}={key}]")
        else:
            for i, e in enumerate(gold):
                if i >= len(live):
                    bad.append(f"{path}[{i}]: live list shorter")
                    continue
                bad += _golden_contains(e, live[i], f"{path}[{i}]")
    elif isinstance(gold, float) and isinstance(live, (int, float)):
        if not math.isclose(gold, live, rel_tol=_REL_TOL, abs_tol=_REL_TOL):
            bad.append(f"{path}: {gold} != {live}")
    elif gold != live:
        bad.append(f"{path}: {gold!r} != {live!r}")
    return bad


def _id_key(items: list):
    """A key common to every dict element, usable to match list elements across
    reorderings (e.g. OHLCV bars by 'date', benchmark comparisons by 'benchmark')."""
    if not items or not all(isinstance(e, dict) for e in items):
        return None
    for cand in ("date", "benchmark", "period", "symbol", "code", "brokerCode"):
        if all(cand in e for e in items):
            return cand
    return None


class MatriksAdapter(IngestionAdapter):
    source_name = "matriks"

    def __init__(self, *, transport: str = "rest", timeout: float = 40.0) -> None:
        # 'rest' is the ingestion transport; 'mcp' is interactive-agent-only.
        self.transport = transport
        self.timeout = timeout
        self.username = os.getenv("MATRIKS_USERNAME", "")
        self.api_key = os.getenv("MATRIKS_API_KEY", "")
        self.rest_url = os.getenv(
            "MATRIKS_REST_URL", "https://mcp.matriks.ai/mcp-api/v1"
        ).rstrip("/")

    # --- pure helpers (testable offline) ----------------------------------
    def _rest_headers(self) -> dict[str, str]:
        """REST auth header. The gateway authenticates on ``X-API-Key`` alone in
        ``<username>:<key>`` form; an extra ``X-Client-ID`` header is REJECTED
        (HTTP 500 'Authentication failed'), so it is deliberately not sent (ADR-0002)."""
        return {
            "X-API-Key": f"{self.username}:{self.api_key}",
            "Content-Type": "application/json",
        }

    def _rest_endpoint(self, tool: str) -> str:
        slug = TOOL_PATHS.get(tool, tool)
        return f"{self.rest_url}/tools/{slug}/execute"

    # --- network (M0/M1) ---------------------------------------------------
    def fetch(self, tool: str, **params):
        """POST params to the tool's /execute endpoint and return the parsed payload.

        Raises ``SourceUnreachable`` on any transport/HTTP/tool failure — NEVER
        returns placeholder/interpolated data (§4). Raises ``ContractDrift`` if the
        response envelope is not the expected ``content[0].text`` JSON shape.
        """
        if not self.username or not self.api_key:
            raise SourceUnreachable(
                "Matriks credentials missing (MATRIKS_USERNAME / MATRIKS_API_KEY). "
                "Load .env: `set -a && source .env && set +a`."
            )
        url = self._rest_endpoint(tool)
        try:
            resp = httpx.post(
                url, headers=self._rest_headers(), json=params, timeout=self.timeout
            )
        except httpx.HTTPError as e:  # network / DNS / timeout
            raise SourceUnreachable(f"Matriks {tool} POST failed: {e}") from e
        if resp.status_code != 200:
            raise SourceUnreachable(
                f"Matriks {tool} HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            envelope = resp.json()
        except ValueError as e:
            raise ContractDrift(f"Matriks {tool}: non-JSON response") from e
        if envelope.get("isError"):
            raise SourceUnreachable(f"Matriks {tool} returned isError: {envelope}")
        try:
            text = envelope["content"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ContractDrift(
                f"Matriks {tool}: unexpected envelope shape: {str(envelope)[:200]}"
            ) from e

    # --- parsers (M1: bars + corporate actions -> L2-shaped rows) ---------
    @staticmethod
    def parse_bars(payload: dict, *, symbol: str | None = None) -> list[dict]:
        """``historicalData`` payload -> ``prices``-schema rows (no L2 write here).

        Accepts both the equity shape (``allBars`` with full OHLCV) and the FX/index
        shape (``bars`` with date+close only — USDTRY has no turnover, legit). Missing
        OHLC fields become ``None``, never a fabricated value. ``knowledge_date`` =
        ``bar_date`` (a daily close is known end-of-that-day). ``adjusted`` carries the
        vendor flag through so the return constructor knows the basis (W7 back-adjusted).
        """
        sym = symbol or payload.get("symbol")
        if sym is None:
            raise ContractDrift("parse_bars: no symbol on payload and none supplied")
        bars = payload.get("allBars")
        if bars is None:
            bars = payload.get("bars")
        if bars is None:
            raise ContractDrift("parse_bars: payload has neither 'allBars' nor 'bars'")
        adjusted = payload.get("period", {}).get("adjusted")
        rows = []
        for b in bars:
            d = b.get("date")
            if not d:
                continue  # blank bar date -> drop, never guess (§4)
            rows.append(
                {
                    "symbol": sym,
                    "bar_date": d,
                    "open": b.get("open"),
                    "high": b.get("high"),
                    "low": b.get("low"),
                    "close": b.get("close"),
                    "volume_try": b.get("volume"),
                    "quantity": b.get("quantity"),
                    "adjusted": adjusted,
                    "is_limit_lock": False,
                    "is_stale": False,
                    "knowledge_date": d,
                    "source": MatriksAdapter.source_name,
                }
            )
        return rows

    @staticmethod
    def parse_corporate_actions(payload: dict) -> dict:
        """``fundamentalAnalysis`` (``dividendsCapital``) payload -> a structured
        corporate-action record. Blank/garbage ex-date strings are **dropped and
        counted** (``refused_*``), never coerced to a guessed date — the golden
        sample warns some ``capital_increase`` rows carry empty ex-dates.

        Returns ``{symbol, capital_increase_exdates, dividends, refused_capital,
        refused_dividend}``. ``dividends`` items keep ``ex_date, gross, net, unit``.
        """
        sym = payload.get("symbol")
        cap_in = payload.get("capital_increases_exdates", []) or []
        cap_out, refused_cap = [], 0
        for ex in cap_in:
            if isinstance(ex, str) and ex.strip():
                cap_out.append(ex.strip())
            else:
                refused_cap += 1

        div_in = []
        for key in ("dividends", "dividends_2024", "dividends_2023"):
            div_in += payload.get(key, []) or []
        div_out, refused_div = [], 0
        for d in div_in:
            ex = (d.get("exDividend") or d.get("exDate") or "").strip()
            if not ex:
                refused_div += 1
                continue
            div_out.append(
                {
                    "ex_date": ex,
                    "gross": d.get("gross"),
                    "net": d.get("net"),
                    "unit": d.get("unit"),
                }
            )
        return {
            "symbol": sym,
            "capital_increase_exdates": cap_out,
            "dividends": div_out,
            "refused_capital": refused_cap,
            "refused_dividend": refused_div,
        }

    # --- drift guard (M0 [STOP] gate) -------------------------------------
    def smoke_check(self) -> None:
        """Re-fetch the golden samples and prove the live data path (scripts/
        smoke_data_access.py · M0 [STOP] gate). Writes a JSON audit report (§4).

        Two check modes (see _VALUE_ANCHORS): VALUE anchors must field-match the
        live re-fetch (ContractDrift on mismatch); the curated/volatile single-call
        goldens are proven REACHABLE only (non-empty, non-error payload). Composite
        multi-call goldens are skipped here. SourceUnreachable on any unreachable
        tool. Raises on the first hard failure — never fabricates a pass.
        """
        files = sorted(_GOLDEN.glob("*.json"))
        if not files:
            raise SourceUnreachable(f"no golden samples in {_GOLDEN}")

        matched: list[str] = []
        reached: list[str] = []
        skipped: list[dict] = []
        drift: list[dict] = []

        for f in files:
            doc = json.loads(f.read_text())
            prov = doc.get("_provenance", {})
            tool = prov.get("tool")
            params = prov.get("params")
            if isinstance(params, list) or doc.get("data") is None:
                skipped.append({"file": f.name, "reason": "composite/curated multi-call anchor (M1/M2)"})
                continue
            live = self.fetch(tool, **params)  # SourceUnreachable -> stops here
            if not isinstance(live, dict) or not live:
                raise SourceUnreachable(f"{f.name}: {tool} returned empty/invalid payload")
            if f.name in _VALUE_ANCHORS:
                bad = _golden_contains(doc["data"], live)
                if bad:
                    drift.append({"file": f.name, "tool": tool, "mismatches": bad[:10]})
                else:
                    matched.append(f.name)
            else:
                reached.append(f.name)

        report = {
            "source": self.source_name,
            "transport": self.transport,
            "rest_url": self.rest_url,
            "value_matched": matched,
            "reachable_only": reached,
            "skipped": skipped,
            "drift": drift,
        }
        write_run_report("matriks_smoke", report)

        if drift:
            raise ContractDrift(
                f"Matriks contract drift on {[d['file'] for d in drift]}: "
                f"{drift[0]['mismatches']}"
            )
        if not matched:
            raise ContractDrift(
                "no VALUE-anchor golden matched — the immutable OHLCV contract "
                "could not be reproduced; refusing to report PASS."
            )

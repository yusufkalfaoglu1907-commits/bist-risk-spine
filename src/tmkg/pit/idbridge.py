"""Id-bridge resolver — ticker ↔ ISIN ↔ kap_oid ↔ LEI over the v1 Kuzu graph.

The id-bridge is a single point of failure (CLAUDE.md §5): if a ticker resolves
to the wrong ISIN/LEI, every downstream signal for that name is silently wrong.
So it RESOLVES exactly, or REFUSES and logs — it never guesses (v1 confidence-
tiered pattern). Ambiguous lookups raise IdentityAmbiguous and are recorded in
data/cache/idbridge_report.json.

Identity is not a time-varying signal read, so this reads the graph directly (like
L2Store on the quant side) rather than through the PIT as-of filter. Time-varying
edges (MEMBER_OF / CONTROLS history) are what PITAccess.graph() will constrain.

(MKK's ``mkkMemberOid`` — the Matriks/MKK id space — is reconciled against
``kap_oid`` when Matriks ingestion lands in M1; here ``kap_oid`` is the graph's
organisation-id leg of the bridge.)
"""
from __future__ import annotations

from typing import Any

from tmkg.ingest.audit import write_run_report
from tmkg.pit.errors import IdentityAmbiguous

# The bridge legs, in the order CLAUDE.md §5 names them. All are STRING props on
# the Company node (schema/ddl.py). Every leg must round-trip to the same name.
ID_FIELDS = ("ticker", "isin", "kap_oid", "lei")


class IdBridge:
    def __init__(self, con: Any) -> None:
        self._con = con
        self.refused: list[dict] = []

    def _rows(self, field: str, value: str) -> list[dict]:
        proj = ", ".join(f"c.{f}" for f in ID_FIELDS)
        res = self._con.execute(
            f"MATCH (c:Company) WHERE c.{field} = $v RETURN {proj}, c.uuid, c.name",
            {"v": value},
        )
        cols = list(ID_FIELDS) + ["uuid", "name"]
        out = []
        while res.has_next():
            out.append(dict(zip(cols, res.get_next())))
        return out

    def resolve(self, value: str, *, field: str | None = None) -> dict | None:
        """Resolve an identifier to its full identity record.

        ``field`` names the leg ``value`` is given on; if omitted, every leg is
        tried and exactly one Company must match across all of them. Returns the
        identity dict, ``None`` if nothing matches, and raises IdentityAmbiguous
        (after logging) if more than one distinct Company matches.
        """
        if field is not None:
            if field not in ID_FIELDS:
                raise ValueError(f"unknown id field {field!r}; expected one of {ID_FIELDS}")
            matches = self._rows(field, value)
        else:
            seen: dict[str, dict] = {}
            for f in ID_FIELDS:
                for r in self._rows(f, value):
                    seen[r["uuid"]] = r
            matches = list(seen.values())

        # de-dup by uuid (a value could match the same Company on >1 leg)
        uniq = {r["uuid"]: r for r in matches}
        if not uniq:
            return None
        if len(uniq) > 1:
            self._refuse(value, field, list(uniq.values()))
            raise IdentityAmbiguous(
                f"{value!r} (field={field or 'any'}) matches {len(uniq)} companies: "
                f"{[r['ticker'] for r in uniq.values()]} — refusing to guess."
            )
        return next(iter(uniq.values()))

    def round_trip(self, ticker: str) -> dict:
        """Resolve a ticker, then confirm each non-null leg resolves back to the
        SAME Company. Raises IdentityAmbiguous on any inconsistency; returns the
        identity record when every leg agrees."""
        rec = self.resolve(ticker, field="ticker")
        if rec is None:
            raise IdentityAmbiguous(f"ticker {ticker!r} not found in graph")
        for f in ID_FIELDS:
            val = rec.get(f)
            if not val:
                continue
            back = self.resolve(val, field=f)
            if back is None or back["uuid"] != rec["uuid"]:
                raise IdentityAmbiguous(
                    f"id-bridge broken: {ticker} leg {f}={val!r} resolves to "
                    f"{None if back is None else back['ticker']}, not {ticker}"
                )
        return rec

    def _refuse(self, value, field, candidates) -> None:
        self.refused.append(
            {"value": value, "field": field, "candidates": [c["ticker"] for c in candidates]}
        )

    def flush_report(self):
        """Write the refused-resolution audit (§4 rule 3 / confidence-tiered)."""
        return write_run_report(
            "idbridge", {"refused_count": len(self.refused), "refused": self.refused}
        )

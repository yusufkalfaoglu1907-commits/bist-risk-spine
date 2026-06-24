"""PITAccess — the single sanctioned gateway to L1 (Kuzu) and L2 (DuckDB).

HARD RULE (CLAUDE.md §4/§5): signal/backtest code reads ONLY through this class.
No raw ``conn.execute`` / ``SELECT`` in L3. Every method requires an as_of date
and must refuse to return any row whose knowledge_date > as_of.

The keystone guarantee is structural, not by-discipline: ``series()`` builds the
SQL itself and ALWAYS injects ``knowledge_date <= as_of`` — there is no code path
that returns an L2 row the caller could not have known at ``as_of``. The gate that
proves it is tests/invariants/test_pit_leak.py (the PIT-leak detector).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from tmkg.pit.errors import PITViolation

# The bitemporal L2 tables PITAccess may read. Every one carries knowledge_date
# (schema.sql). Restricting to this allow-list keeps the table name — which is
# code-supplied, never user-supplied — from being a SQL-injection surface.
_L2_TABLES = frozenset(
    {
        "prices", "total_returns", "factors", "foreign_flow", "betas",
        "residuals", "residual_corr", "accounting_regime", "short_eligible",
        "signal_registry", "universe_membership", "events", "event_targets",
    }
)


class PITAccess:
    """Open one of these per as_of date; use it for all reads at that vintage."""

    def __init__(self, as_of: date, *, l1: Any = None, l2: Any = None) -> None:
        if as_of is None:
            raise PITViolation("PITAccess requires an as_of date; refusing an unbounded read.")
        self.as_of = as_of
        self._l1 = l1  # Kuzu connection (structural graph)
        self._l2 = l2  # DuckDB connection (quant store)

    # --- L2: quant store ---------------------------------------------------
    def series(
        self,
        table: str,
        *,
        symbol: str | None = None,
        columns: str = "*",
        where: str | None = None,
        latest_by: str | None = None,
    ):
        """Return a point-in-time L2 frame from ``table``, filtered so NO row has
        ``knowledge_date > as_of``. This is the only sanctioned L2 read path.

        ``latest_by``: keep only the row(s) whose value of that column is the max
        among the visible rows — the "latest KNOWN as of ``as_of``" view (e.g.
        ``latest_by='period'`` returns the most recent fundamental period that had
        been declared by ``as_of``, never one declared later). ``where`` adds an
        extra static predicate (code-supplied), never replacing the PIT filter.
        """
        if self._l2 is None:
            raise PITViolation("PITAccess.series needs an L2 (DuckDB) connection.")
        if table not in _L2_TABLES:
            raise PITViolation(f"unknown / non-bitemporal L2 table: {table!r}")

        # knowledge_date <= as_of is non-negotiable and parameter-bound.
        clauses = ["knowledge_date <= ?"]
        params: list[Any] = [self.as_of]
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if where:
            clauses.append(f"({where})")
        sql = f"SELECT {columns} FROM {table} WHERE " + " AND ".join(clauses)

        df = self._l2.execute(sql, params).df()

        if latest_by is not None and not df.empty:
            df = df[df[latest_by] == df[latest_by].max()]
        return df.reset_index(drop=True)

    # --- L1: structural graph ---------------------------------------------
    def graph(self, cypher: str, params: dict[str, Any] | None = None):
        """Run a graph query auto-constrained to knowledge_date <= as_of.

        TODO(M0 T4): inject the bitemporal predicate for time-varying edges
        (MEMBER_OF, CONTROLS history) and return edges with their Provenance.
        Implemented alongside the id-bridge resolver.
        """
        raise NotImplementedError("M0 T4: implement PIT-constrained graph reads")

    def universe(self, universe: str = "listed"):
        """The survivorship-correct as-of investable universe (the W2 wall): the
        symbols whose membership window [valid_from, valid_to] contains ``as_of``
        and that were known by ``as_of`` (knowledge_date <= as_of).

        A name delisted before ``as_of`` is absent from THIS as-of set but its row
        is retained in universe_membership — so a read at an earlier ``as_of``
        inside its window still includes it (no survivorship bias). Returns a frame
        of (symbol, universe_class).

        **Bitemporal correction.** A membership span (keyed by symbol + universe +
        valid_from) can be re-stated as facts arrive — e.g. an open membership later
        corrected to closed when a delisting is announced. Among the versions of a
        span that were KNOWN by ``as_of`` (knowledge_date <= as_of) only the latest
        is applied, so a read dated before a delisting was announced honestly shows
        the name as still-open (the market did not yet know it would delist), while a
        read after the announcement sees the closed window. This is what makes the
        as-of universe both survivorship-correct AND free of delisting look-ahead.
        """
        if self._l2 is None:
            raise PITViolation("PITAccess.universe needs an L2 (DuckDB) connection.")
        sql = (
            "WITH visible AS ("
            "  SELECT symbol, universe_class, valid_from, valid_to, "
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY symbol, universe, valid_from "
            "             ORDER BY knowledge_date DESC"
            "         ) AS _rn "
            "  FROM universe_membership "
            "  WHERE universe = ? AND knowledge_date <= ? "  # could we have known it?
            ") "
            "SELECT DISTINCT symbol, universe_class FROM visible "
            "WHERE _rn = 1 "                                 # latest known version of the span
            "AND valid_from <= ? "                           # already a member by as_of
            "AND (valid_to IS NULL OR valid_to >= ?) "       # not yet delisted at as_of
            "ORDER BY symbol"
        )
        return self._l2.execute(
            sql, [universe, self.as_of, self.as_of, self.as_of]
        ).df()

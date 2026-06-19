"""L2 quant store — DuckDB + Parquet (BUILD_PLAN.md M0).

500 names is small; DuckDB at research scale is the right call (design §5).
Parquet under ``data/l2/<table>/`` is the durable, portable layer; the DuckDB
file is the query engine + bitemporal schema (schema.sql). Reads in *signal* code
go through tmkg.pit.PITAccess, never directly through here — this class is the
ingestion/storage side (writes land bitemporal rows; PITAccess filters on read).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import duckdb

import tmkg.config as config

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


class L2Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else (config.REPO_ROOT / "data" / "l2.duckdb")
        self.parquet_root = self.db_path.parent / "l2"

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Open the embedded DuckDB database (local-first, no server)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.db_path))

    def bootstrap_schema(self) -> None:
        """Create all L2 tables from schema.sql if absent (idempotent)."""
        sql = SCHEMA_SQL.read_text()
        con = self.connect()
        try:
            con.execute(sql)
        finally:
            con.close()

    def tables(self) -> list[str]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()

    def write_parquet(self, table: str, df, *, partition_cols=None) -> Path:
        """Persist a frame to Parquet under ``data/l2/<table>/`` and insert it into
        the bitemporal DuckDB table. Idempotent at the row level: a re-land of the
        same primary key is ignored (ON CONFLICT DO NOTHING), never duplicated or
        overwritten — bitemporal history is append-only.

        Returns the Parquet path written. ``df`` is any object DuckDB can scan
        (pandas/polars/pyarrow). Column names must match the target table.
        """
        out_dir = self.parquet_root / table
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"part-{uuid.uuid4().hex}.parquet"

        con = self.connect()
        try:
            con.register("_incoming", _to_arrow(df))
            cols = [r[0] for r in con.execute("DESCRIBE _incoming").fetchall()]
            collist = ", ".join(f'"{c}"' for c in cols)
            # durable Parquet layer
            con.execute(f"COPY _incoming TO '{path}' (FORMAT PARQUET)")
            # query/index layer — append-only, PK-idempotent
            con.execute(
                f"INSERT INTO {table} ({collist}) "
                f"SELECT {collist} FROM _incoming "
                f"ON CONFLICT DO NOTHING"
            )
            con.unregister("_incoming")
        finally:
            con.close()
        return path

    def read_table(self, table: str, where: str | None = None):
        """Read a whole table back as a pandas frame (ingestion-side reconciliation
        only; signal code must use tmkg.pit.PITAccess)."""
        con = self.connect()
        try:
            q = f"SELECT * FROM {table}"
            if where:
                q += f" WHERE {where}"
            return con.execute(q).df()
        finally:
            con.close()


def _to_arrow(df):
    """Accept pandas/polars/pyarrow and hand DuckDB something it can scan."""
    if hasattr(df, "to_arrow"):  # polars
        return df.to_arrow()
    return df  # pandas / pyarrow are scanned directly by duckdb.register

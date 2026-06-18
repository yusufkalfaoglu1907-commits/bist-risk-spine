"""KuzuDB connection helper."""
from __future__ import annotations

from pathlib import Path

import kuzu

from tmkg import config


def connect(db_path: Path | str | None = None) -> kuzu.Connection:
    """Open (or create) the Kuzu database and return a Connection.

    Kuzu is embedded: the database is a directory on disk, no server needed.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = kuzu.Database(str(path))
    return kuzu.Connection(db)

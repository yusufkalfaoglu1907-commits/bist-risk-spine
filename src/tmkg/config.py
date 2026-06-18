"""Configuration — resolves paths and reads .env (optional in Phase 1)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv optional; fixtures run without it
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(os.getenv("TMKG_DB_PATH", REPO_ROOT / "data" / "tmkg.kuzu"))
RAW_DOCS_PATH = Path(os.getenv("TMKG_RAW_DOCS_PATH", REPO_ROOT / "data" / "raw_docs"))
FIXTURES_PATH = REPO_ROOT / "fixtures"

EVDS_API_KEY = os.getenv("EVDS_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GLEIF_USER_AGENT = os.getenv(
    "GLEIF_USER_AGENT", "turkish-markets-kg/0.1 (info@arteklab.com)"
)

"""Shared fixtures for the invariant + golden suites."""
from __future__ import annotations

import json
import pathlib

import pytest

GOLDEN_MATRIKS = pathlib.Path(__file__).resolve().parents[1] / "golden" / "matriks"


@pytest.fixture
def golden_dir() -> pathlib.Path:
    return GOLDEN_MATRIKS


@pytest.fixture
def load_golden():
    """load_golden('ohlcv_EREGL_2024-11.json') -> dict."""

    def _load(name: str) -> dict:
        return json.loads((GOLDEN_MATRIKS / name).read_text())

    return _load

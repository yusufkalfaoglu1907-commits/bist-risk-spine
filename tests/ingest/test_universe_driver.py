"""The universe ingestion driver's burst-abort classification (scripts/ingest_universe.py).

The §8 burst-abort guard exists to halt a long run when the gateway is DOWN. A healthy
gateway honestly reporting a dataless/delisted name (``NO_DATA_FOUND``) must NOT count
toward that streak — otherwise a cluster of dataless names falsely aborts the run. This
pins that distinction so the regression (treating "no data" as "gateway down") can't return.
"""
import importlib.util
from pathlib import Path

import pytest

from tmkg.pit.errors import SourceUnreachable

_DRIVER = Path(__file__).resolve().parents[2] / "scripts" / "ingest_universe.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("ingest_universe", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load_driver()


@pytest.mark.parametrize(
    "msg",
    [
        # the real Matriks isError envelopes (code + errorCode + Turkish text)
        'historicalData returned isError: {"error": "AAGYO için daily tarihsel veri '
        'bulunamadı.", "code": "NO_DATA_FOUND"}',
        "errorCode': 'NO_DATA_FOUND'",
        "AAGYO için daily tarihsel veri bulunamadı.",
    ],
)
def test_no_data_errors_do_not_count_as_outage(msg):
    assert driver._is_no_data(SourceUnreachable(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "Matriks historicalData HTTP 504: Gateway Time-out",
        'Matriks symbolSearch HTTP 500: {"error":"INTERNAL_ERROR","message":"Authentication failed"}',
        "Matriks historicalData POST failed: ConnectError",
    ],
)
def test_genuine_failures_are_not_classified_as_no_data(msg):
    # 504 / auth-500 / transport are the outage signals the burst guard must catch.
    assert driver._is_no_data(SourceUnreachable(msg)) is False

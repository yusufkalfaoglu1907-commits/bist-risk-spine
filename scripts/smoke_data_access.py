#!/usr/bin/env python3
"""M0 task 1 — data-access smoke test (BUILD_PLAN.md M0; the hard gate).

Proves the data path end-to-end BEFORE any other v2 code is trusted:
  1. env present (MATRIKS_API_KEY),
  2. golden samples exist + parse,
  3. the live connector is reachable and matches the golden samples.

Exits non-zero unless the path is proven. Per the data contract, it NEVER
fabricates: an unreachable source is a loud failure, not a fallback.

Run:  python scripts/smoke_data_access.py     (pythonpath=src via pytest.ini; for
      direct runs use  PYTHONPATH=src python scripts/smoke_data_access.py )
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "golden" / "matriks"


def _ok(msg: str) -> None:
    print(f"  OK   {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def check_env() -> bool:
    key = os.getenv("MATRIKS_API_KEY", "")
    if key:
        _ok("MATRIKS_API_KEY present")
        return True
    _fail("MATRIKS_API_KEY missing — load .env (it is gitignored)")
    return False


def check_golden() -> bool:
    files = sorted(GOLDEN.glob("*.json"))
    if not files:
        _fail(f"no golden samples in {GOLDEN}")
        return False
    for f in files:
        try:
            json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            _fail(f"{f.name} invalid: {e}")
            return False
    _ok(f"{len(files)} golden samples parse")
    return True


def check_connector() -> bool:
    """Re-fetch each golden sample's _provenance.params and assert a match."""
    try:
        from tmkg.ingest import MatriksAdapter
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot import MatriksAdapter: {e}")
        return False
    try:
        MatriksAdapter().smoke_check()
    except NotImplementedError:
        _fail("MatriksAdapter.smoke_check() not implemented yet (M0). "
              "Implement: re-fetch golden params, assert match; raise on drift/unreachable.")
        return False
    except Exception as e:  # noqa: BLE001  (SourceUnreachable / ContractDrift)
        _fail(f"connector check failed: {e}")
        return False
    _ok("Matriks reachable and matches golden samples")
    return True


def check_evds() -> bool:
    """Re-fetch the committed CPI golden and assert the live evds3 values match.
    EVDS is the CPI-real-TRY cross-check source (M1). Fail loud on drift/unreachable."""
    if not os.getenv("EVDS_API_KEY", ""):
        _fail("EVDS_API_KEY missing — load .env (it is gitignored)")
        return False
    try:
        from tmkg.ingest import EvdsAdapter
        EvdsAdapter().smoke_check()
    except Exception as e:  # noqa: BLE001  (SourceUnreachable / ContractDrift)
        _fail(f"EVDS check failed: {e}")
        return False
    _ok("EVDS reachable and CPI matches golden")
    return True


def main() -> int:
    print("M0 data-access smoke test")
    results = [check_env(), check_golden(), check_connector(), check_evds()]
    if all(results):
        print("PASS — data path proven; M0 may proceed.")
        return 0
    print("INCOMPLETE — fix the FAIL lines above. Do NOT fabricate data to proceed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""M9 full daily refresh chain — bring the L2 store current so every read uses the latest close.

Runs three steps in order, each a proven CLI (isolated as subprocesses so one step's crash is
captured, and each keeps writing its own §4 audit report):

  1. ingest_factors.py           refresh the core factor series (Matriks/FRED/EVDS/WGB) — incl. USDTRY
  2. daily_update.py --execute   top up prices + total_returns for STALE names (Matriks; incremental)
  3. refit_factor_model.py       rebuild betas + neutralized residuals as of today (L2-only; no gate)

Ordering matters (BUILD_LOG 2026-07-01): factors (incl. USDTRY) refresh FIRST, so when step 2
builds ``total_returns`` the USD-primary ``ret_usd`` isn't left NULL for every new day (which
would stall residuals). Step 2 also rebuilds returns with ``overwrite`` so a corrected value lands.

This is the script the launchd schedule (com.tmkg.dailyrefresh) invokes after BIST close. It writes a
combined status report to data/cache/refresh_all_report.json (per-step exit code + duration).

    PYTHONPATH=src python scripts/refresh_all.py [--dry-run] [--max-age N]

``--dry-run`` validates wiring with no network/mutation: step 1 runs in its own dry-run (plan only);
steps 2 and 3 are described but not run.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
REPORT = REPO / "data" / "cache" / "refresh_all_report.json"


def _run(label: str, argv: list[str], *, dry_run: bool) -> dict:
    """Run one step as a subprocess (same interpreter, PYTHONPATH=src); capture rc + duration."""
    cmd = [sys.executable, *argv]
    print(f"\n=== [{label}] {' '.join(argv)} ===", flush=True)
    if dry_run and label != "daily_update":
        print("  (dry-run) skipped — would run the above", flush=True)
        return {"step": label, "skipped": True, "rc": None}
    env = {"PYTHONPATH": "src"}
    t0 = time.time()
    # inherit env, just ensure PYTHONPATH; stream output through to the launchd log.
    import os
    full_env = {**os.environ, **env}
    proc = subprocess.run(cmd, cwd=str(REPO), env=full_env)
    dt = round(time.time() - t0, 1)
    print(f"  -> rc={proc.returncode} in {dt}s", flush=True)
    return {"step": label, "skipped": False, "rc": proc.returncode, "seconds": dt}


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    max_age = argv[argv.index("--max-age") + 1] if "--max-age" in argv else "7"

    started = datetime.now(timezone.utc).isoformat()
    steps = [
        ("ingest_factors", ["scripts/ingest_factors.py"]),
        ("daily_update", ["scripts/daily_update.py", *([] if dry_run else ["--execute"]),
                          "--max-age", max_age]),
        ("refit", ["scripts/refit_factor_model.py"]),
    ]

    results: list[dict] = []
    for label, step_argv in steps:
        r = _run(label, step_argv, dry_run=dry_run)
        results.append(r)
        # If a top-up/factor step hard-fails, still attempt the refit (rebuilds from whatever
        # landed; the refit self-aborts safely if the factor panel is incomplete). So: never
        # short-circuit — record everything.

    ok = all((r["skipped"] or r["rc"] == 0) for r in results)
    report = {
        "run": "refresh_all",
        "mode": "dry-run" if dry_run else "execute",
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "steps": results,
        "ok": ok,
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n=== refresh_all {'OK' if ok else 'HAD FAILURES'} -> {REPORT} ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

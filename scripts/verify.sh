#!/usr/bin/env bash
# Single verification entry point (CLAUDE.md §7 / VERIFICATION.md "Run it").
# Leave the repo GREEN between sessions, or log RED explicitly in BUILD_LOG.md.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="src:${PYTHONPATH:-}"

echo "== invariant suite (CLAUDE.md §5 guards) =="
python -m pytest tests/invariants -q

echo "== golden masters (known-answer reconciliation) =="
python -m pytest tests/golden -q

echo "== full suite (excl. slow) =="
python -m pytest -q -m "not slow"

echo "verify.sh: GREEN"

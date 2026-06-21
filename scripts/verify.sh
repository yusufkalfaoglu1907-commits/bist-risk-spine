#!/usr/bin/env bash
# Single verification entry point (CLAUDE.md §7 / VERIFICATION.md "Run it").
# Leave the repo GREEN between sessions, or log RED explicitly in BUILD_LOG.md.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="src:${PYTHONPATH:-}"

# Prefer the project venv (Python 3.13 + duckdb/kuzu/sklearn) over a bare `python`, which
# on this machine may resolve to a different interpreter without the deps installed.
if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi
echo "verify.sh: using $PY ($($PY --version 2>&1))"

echo "== invariant suite (CLAUDE.md §5 guards) =="
"$PY" -m pytest tests/invariants -q

echo "== golden masters (known-answer reconciliation) =="
"$PY" -m pytest tests/golden -q

echo "== full suite (excl. slow) =="
"$PY" -m pytest -q -m "not slow"

echo "verify.sh: GREEN"

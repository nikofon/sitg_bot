#!/usr/bin/env bash
# Canonical local/gate checks. No installs, DB creation or hidden exclusions in test.
set -euo pipefail
cd "$(dirname "$0")/.."
python=${SITG_TEST_PYTHON:-.venv/bin/python}
case "${1:-test}" in
  prepare)
    python3.12 -m venv .venv
    "$python" -m pip install -e '.[dev]'
    npm --prefix web ci
    ;;
  unit) "$python" -m pytest tests/unit -q ;;
  frontend)
    npm --prefix web run typecheck
    npm --prefix web test
    npm --prefix web run build
    ;;
  integration)
    : "${TEST_DATABASE_URL:?Source the workstream private DB record first; see docs/database-operations.md}"
    : "${SITG_TEST_DATABASE_TOKEN:?Missing workstream DB ownership receipt}"
    "$python" -m pytest tests/test_database_support.py -q
    "$python" tests/integration/database_support.py run -- \
      "$python" -m pytest tests/integration -q
    ;;
  test)
    # Run every scope even if an earlier scope fails, and preserve a failing exit.
    result=0
    for scope in unit frontend integration; do
      bash "$0" "$scope" || result=1
    done
    exit "$result"
    ;;
  lint) "$python" -m ruff check src tests migrations ;;
  *) echo "Usage: bash scripts/check.sh {prepare|unit|frontend|integration|test|lint}" >&2; exit 2 ;;
esac

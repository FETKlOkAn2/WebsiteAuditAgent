#!/usr/bin/env bash
# Run every test in one shot. Exits non-zero on any failure.
#
# Usage:
#   ./tests/run_all.sh
#
set -e

cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

echo "→ Compiling all modules…"
"$PY" -m py_compile audit_agent.py analyzer.py config.py conversion_audit.py \
                    output.py prompts.py prospector.py scraper.py sender.py
echo "  OK"

echo
echo "→ Running unit/integration suite (offline, mocked)…"
"$PY" -m unittest tests.test_reliability -v 2>&1 | tail -8

echo
echo "→ Running end-to-end smoke test (real local HTTP, stubbed LLM)…"
"$PY" tests/smoke_test.py | tail -50

echo
echo "✓ All checks passed."

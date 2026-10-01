#!/usr/bin/env bash
# Run every timetracking verify_*.py from the repo root with the project venv.
# Usage: tests/timetracking/run_all.sh [path-to-python]
set -u
cd "$(dirname "$0")/../.." || exit 1

PY="${1:-discord-bot-venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "Interpreter '$PY' not found. Pass one: tests/timetracking/run_all.sh /path/to/python"
  exit 1
fi

fail=0
for f in tests/timetracking/verify_*.py; do
  echo "=== $f ==="
  if "$PY" "$f"; then :; else
    echo "!!! FAILED: $f"
    fail=1
  fi
  echo
done

if [ "$fail" -eq 0 ]; then
  echo "All timetracking verifications passed."
else
  echo "Some verifications FAILED."
fi
exit "$fail"

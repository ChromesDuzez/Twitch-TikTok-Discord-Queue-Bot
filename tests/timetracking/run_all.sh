#!/usr/bin/env bash
# Run the timetracking test suite from the repo root with the project venv.
# Usage: tests/timetracking/run_all.sh [path-to-python] [extra pytest args...]
set -u
cd "$(dirname "$0")/../.." || exit 1

PY="${1:-discord-bot-venv/bin/python}"
shift 2>/dev/null || true
if [ ! -x "$PY" ]; then
  echo "Interpreter '$PY' not found. Pass one: tests/timetracking/run_all.sh /path/to/python"
  exit 1
fi

exec "$PY" -m pytest tests/timetracking "$@"

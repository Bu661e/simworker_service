#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-/root/isaacsim}"
PYTHON_SH="$ISAAC_SIM_ROOT/python.sh"
RUNNER_PY="$REPO_ROOT/simworker/test_gui/run_task_gui.py"

if [[ ! -x "$PYTHON_SH" ]]; then
  echo "Isaac Sim python.sh not found or not executable: $PYTHON_SH"
  exit 1
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT"
exec "$PYTHON_SH" "$RUNNER_PY" "$@"

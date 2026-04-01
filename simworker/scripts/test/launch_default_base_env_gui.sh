#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Isaac Sim 根目录优先级：环境变量 > 命令行参数 > 当前机器默认安装目录。
ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-${1:-/root/isaacsim}}"

PYTHON_SH="$ISAAC_SIM_ROOT/python.sh"
if [[ ! -x "$PYTHON_SH" ]]; then
  echo "Isaac Sim python.sh not found or not executable: $PYTHON_SH"
  exit 1
fi

TMP_SCRIPT="$(mktemp /tmp/simworker_default_base_env_gui.XXXXXX.py)"
cleanup() {
  rm -f "$TMP_SCRIPT"
}
trap cleanup EXIT

cat > "$TMP_SCRIPT" <<'PY'
from __future__ import annotations

import logging
import os
import sys

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": False})

REPO_ROOT = os.environ["SIMWORKER_REPO_ROOT"]
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from simworker.base_environments.default import create_default_tabletop_base_environment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("simworker.inspect_base_env")

handles = create_default_tabletop_base_environment(logger)

logger.info("Base environment loaded")
logger.info("Cameras: %s", ", ".join(sorted(handles.cameras.keys())))
logger.info("Close the Isaac Sim window to exit")

try:
    while simulation_app.is_running():
        handles.world.step(render=True)
except KeyboardInterrupt:
    logger.info("Interrupted by user")
finally:
    try:
        from isaacsim.core.api.world import World
    except ImportError:
        pass
    else:
        World.clear_instance()
    simulation_app.close()
PY

export SIMWORKER_REPO_ROOT="$REPO_ROOT"

cd "$REPO_ROOT"
exec "$PYTHON_SH" "$TMP_SCRIPT"

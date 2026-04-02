from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from simworker.robots.franka_api import FrankaRobotAPI


@lru_cache(maxsize=1)
def get_robot_api_text() -> str:
    api_reference_path = Path(__file__).with_name("api_reference.txt")
    return api_reference_path.read_text(encoding="utf-8").strip()

__all__ = [
    "FrankaRobotAPI",
    "get_robot_api_text",
]

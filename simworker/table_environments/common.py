from __future__ import annotations

import logging
import math
from typing import Sequence

_DEFAULT_TABLE_OBJECT_STABILIZATION_STEPS = 16


def ensure_prim_path_is_available(prim_path: str) -> None:
    from isaacsim.core.utils.prims import is_prim_path_valid

    if is_prim_path_valid(prim_path):
        raise ValueError(f"table object prim_path already exists in stage: {prim_path}")


def rollback_created_prims(logger: logging.Logger, prim_paths: Sequence[str]) -> None:
    from isaacsim.core.utils.prims import delete_prim

    for prim_path in reversed(list(prim_paths)):
        try:
            delete_prim(prim_path)
        except Exception:
            logger.exception("Failed to rollback scene prim: %s", prim_path)


def finalize_loaded_object_handles(
    world: object | None,
    logger: logging.Logger,
    handles: Sequence[object],
    *,
    stabilization_steps: int = _DEFAULT_TABLE_OBJECT_STABILIZATION_STEPS,
) -> None:
    if world is None or not handles:
        return

    # 动态物体需要先走几帧仿真稳定下来，再把默认状态更新到稳定后的位姿。
    step_render_frames(world, stabilization_steps)
    for handle in handles:
        persist_handle_default_state(logger, handle)


def persist_handle_default_state(logger: logging.Logger, handle: object) -> None:
    if not hasattr(handle, "set_default_state"):
        return
    try:
        position_xyz_m, quaternion_wxyz = handle.get_world_pose()
        handle.set_default_state(position=position_xyz_m, orientation=quaternion_wxyz)
    except Exception:
        logger.exception("Failed to persist default pose for scene object")


def step_render_frames(world: object, num_frames: int) -> None:
    for _ in range(num_frames):
        world.step(render=True)


def euler_xyz_deg_to_quaternion_wxyz(rotation_rpy_deg: Sequence[float]) -> tuple[float, float, float, float]:
    roll_deg, pitch_deg, yaw_deg = rotation_rpy_deg
    roll = math.radians(float(roll_deg))
    pitch = math.radians(float(pitch_deg))
    yaw = math.radians(float(yaw_deg))

    half_roll = roll / 2.0
    half_pitch = pitch / 2.0
    half_yaw = yaw / 2.0

    cr = math.cos(half_roll)
    sr = math.sin(half_roll)
    cp = math.cos(half_pitch)
    sp = math.sin(half_pitch)
    cy = math.cos(half_yaw)
    sy = math.sin(half_yaw)

    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return (w, x, y, z)

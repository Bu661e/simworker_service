from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from simworker.runtime import WorkerRuntime

_DEFAULT_APPROACH_CLEARANCE_M = 0.30
_DEFAULT_PICK_PLACE_EVENTS_DT = [0.008, 0.005, 1.0, 0.1, 0.05, 0.05, 0.0025, 1.0, 0.008, 0.08]


class FrankaRobotAPI:
    """对任务代码暴露的 Franka 高层动作 API。"""

    def __init__(self, runtime: WorkerRuntime, robot_handle: object, logger: logging.Logger) -> None:
        self._runtime = runtime
        self._robot = robot_handle
        self._logger = logger
        self._pick_place_controller: object | None = None
        self._articulation_controller: object | None = None

    def pick_and_place(
        self,
        pick_position: Sequence[float],
        place_position: Sequence[float],
        rotation: Sequence[float] | None = None,
        grasp_offset: Sequence[float] | None = None,
    ) -> None:
        import numpy as np

        if self._runtime.robot_status != "idle":
            raise RuntimeError("robot is busy")

        picking_position = _coerce_xyz_vector(pick_position, field_name="pick_position")
        placing_position = _coerce_xyz_vector(place_position, field_name="place_position")
        grasp_offset_xyz = (
            _coerce_xyz_vector(grasp_offset, field_name="grasp_offset")
            if grasp_offset is not None
            else np.zeros(3, dtype=np.float64)
        )
        end_effector_orientation = (
            _coerce_quaternion_wxyz(rotation, field_name="rotation") if rotation is not None else None
        )

        # PickPlaceController 的初始高度是世界坐标系下的绝对高度。
        # 这里按“目标中心点最高 z + 固定安全余量”动态计算，避免沿用官方示例里的 0.3 米绝对值后低于桌面。
        approach_center_height = float(max(picking_position[2], placing_position[2]) + _DEFAULT_APPROACH_CLEARANCE_M)

        controller = self._get_pick_place_controller()
        articulation_controller = self._get_articulation_controller()
        self._runtime.ensure_world_playing()

        actual_pick_target = picking_position + grasp_offset_xyz
        actual_place_target = placing_position + grasp_offset_xyz
        self._logger.info(
            "Starting Franka pick_and_place pick_center=%s place_center=%s grasp_offset=%s actual_pick=%s actual_place=%s",
            picking_position.tolist(),
            placing_position.tolist(),
            grasp_offset_xyz.tolist(),
            actual_pick_target.tolist(),
            actual_place_target.tolist(),
        )

        self._runtime.robot_status = "busy"
        try:
            controller.reset(
                end_effector_initial_height=approach_center_height,
                events_dt=list(_DEFAULT_PICK_PLACE_EVENTS_DT),
            )

            while not controller.is_done():
                current_joint_positions = np.asarray(self._robot.get_joint_positions(), dtype=np.float64)
                actions = controller.forward(
                    picking_position=picking_position,
                    placing_position=placing_position,
                    current_joint_positions=current_joint_positions,
                    end_effector_offset=grasp_offset_xyz,
                    end_effector_orientation=end_effector_orientation,
                )
                articulation_controller.apply_action(actions)
                # 每次动作推进都走 runtime 的统一 stepping 入口，保证与 stream 刷新共享同一套世界推进。
                self._runtime.step_world_for_robot_action()
        finally:
            self._runtime.robot_status = "idle"

        self._logger.info("Completed Franka pick_and_place")

    def _get_pick_place_controller(self) -> object:
        if self._pick_place_controller is None:
            from isaacsim.robot.manipulators.examples.franka.controllers.pick_place_controller import (
                PickPlaceController,
            )

            gripper = getattr(self._robot, "gripper", None)
            if gripper is None:
                raise RuntimeError("franka robot gripper is not initialized")

            self._pick_place_controller = PickPlaceController(
                name="simworker_franka_pick_place_controller",
                gripper=gripper,
                robot_articulation=self._robot,
            )
        return self._pick_place_controller

    def _get_articulation_controller(self) -> object:
        if self._articulation_controller is None:
            articulation_controller = self._robot.get_articulation_controller()
            if articulation_controller is None:
                raise RuntimeError("franka articulation controller is not initialized")
            self._articulation_controller = articulation_controller
        return self._articulation_controller


def _coerce_xyz_vector(value: Sequence[float], *, field_name: str):
    import numpy as np

    if len(value) != 3:
        raise ValueError(f"{field_name} must contain exactly 3 numbers")
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (3,):
        raise ValueError(f"{field_name} must contain exactly 3 numbers")
    return vector


def _coerce_quaternion_wxyz(value: Sequence[float], *, field_name: str):
    import numpy as np

    if len(value) != 4:
        raise ValueError(f"{field_name} must contain exactly 4 numbers in quaternion_wxyz order")
    quaternion = np.asarray(value, dtype=np.float64)
    if quaternion.shape != (4,):
        raise ValueError(f"{field_name} must contain exactly 4 numbers in quaternion_wxyz order")
    return quaternion

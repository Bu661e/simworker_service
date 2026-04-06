from __future__ import annotations

import numpy as np

from simworker.robots.franka_api import FrankaRobotAPI


class FakeRuntime:
    def __init__(self, *, gripper: "FakeGripper") -> None:
        self.robot_status = "idle"
        self._gripper = gripper
        self.ensure_world_playing_calls = 0
        self.step_world_calls = 0

    def ensure_world_playing(self) -> None:
        self.ensure_world_playing_calls += 1

    def step_world_for_robot_action(self) -> None:
        self.step_world_calls += 1
        self._gripper.on_step()


class FakeGripper:
    def __init__(self, *, steps_until_open: int = 3) -> None:
        self.open_count = 0
        self._steps_until_open = steps_until_open
        self._steps_since_open = 0
        self.joint_opened_positions = np.array([0.05, 0.05], dtype=np.float64)
        self._current_positions = np.array([0.0, 0.0], dtype=np.float64)

    def open(self) -> None:
        self.open_count += 1
        self._steps_since_open = 0
        self._current_positions = np.array([0.0, 0.0], dtype=np.float64)

    def on_step(self) -> None:
        if self.open_count <= 0:
            return
        self._steps_since_open += 1
        if self._steps_since_open >= self._steps_until_open:
            self._current_positions = self.joint_opened_positions.copy()

    def get_joint_positions(self) -> np.ndarray:
        return self._current_positions.copy()


class FakeRobot:
    def __init__(self, *, gripper: FakeGripper) -> None:
        self.gripper = gripper

    def get_joint_positions(self) -> np.ndarray:
        return np.zeros(9, dtype=np.float64)


class FakeController:
    def __init__(self, runtime: FakeRuntime) -> None:
        self._runtime = runtime
        self.reset_call_count = 0
        self.reset_step_counts: list[int] = []
        self.forward_call_count = 0

    def reset(self, *, end_effector_initial_height: float, events_dt: list[float]) -> None:
        assert end_effector_initial_height > 0.0
        assert len(events_dt) == 10
        self.reset_call_count += 1
        self.reset_step_counts.append(self._runtime.step_world_calls)
        self.forward_call_count = 0

    def is_done(self) -> bool:
        return self.forward_call_count >= 1

    def forward(self, **_: object) -> dict[str, str]:
        self.forward_call_count += 1
        return {"kind": "fake_action"}


class FakeArticulationController:
    def __init__(self) -> None:
        self.actions: list[object] = []

    def apply_action(self, action: object) -> None:
        self.actions.append(action)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, message: str, *args: object) -> None:
        self.records.append(("info", message % args if args else message))

    def warning(self, message: str, *args: object) -> None:
        self.records.append(("warning", message % args if args else message))


def test_pick_and_place_opens_gripper_before_each_run() -> None:
    gripper = FakeGripper(steps_until_open=3)
    runtime = FakeRuntime(gripper=gripper)
    robot = FakeRobot(gripper=gripper)
    logger = FakeLogger()
    controller = FakeController(runtime)
    articulation_controller = FakeArticulationController()
    api = FrankaRobotAPI(runtime=runtime, robot_handle=robot, logger=logger)
    api._get_pick_place_controller = lambda: controller  # type: ignore[method-assign]
    api._get_articulation_controller = lambda: articulation_controller  # type: ignore[method-assign]

    for _ in range(2):
        api.pick_and_place(
            pick_position=[0.1, 0.2, 0.3],
            place_position=[0.4, 0.5, 0.6],
        )

    assert gripper.open_count == 2
    assert controller.reset_call_count == 2
    assert controller.reset_step_counts == [3, 7]
    assert runtime.ensure_world_playing_calls == 2
    assert runtime.step_world_calls == 8
    assert len(articulation_controller.actions) == 2
    assert runtime.robot_status == "idle"

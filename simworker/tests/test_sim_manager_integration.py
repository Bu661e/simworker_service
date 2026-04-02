from __future__ import annotations

import concurrent.futures
import json
import os
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from simworker import SimManager
import simworker.sim_manager as sim_manager_module
from simworker.robots import get_robot_api_text
from simworker.tests.test_simworker_integration import (
    _ENABLE_REAL_TEST_ENV,
    _RUN_TASK_REQUEST_TIMEOUT_SEC,
    _STREAM_FPS_MAX,
    _STREAM_FPS_MIN,
    _assert_object_transform_payload,
    _assert_stream_shared_memory_header,
    _measure_stream_fps,
    _sample_streams_while_run_task_is_running,
    _worker_python,
)


@dataclass
class SimManagerTraceHarness:
    manager: SimManager
    uds_trace_path: Path
    uds_messages: list[dict[str, object]]


@pytest.fixture
def sim_manager_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimManagerTraceHarness]:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    uds_trace_path = tmp_path / "sim_manager_uds_trace.json"
    uds_messages: list[dict[str, object]] = []

    def _flush_uds_trace() -> None:
        uds_trace_path.write_text(
            json.dumps(uds_messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    original_send_json_message = sim_manager_module.send_json_message
    original_recv_json_message = sim_manager_module.recv_json_message

    def _traced_send_json_message(sock, message: dict[str, object]) -> None:
        # 只在 pytest 侧记录经由 UDS 发出的控制面 JSON，不修改生产实现。
        uds_messages.append(
            {
                "direction": "request",
                "timestamp_sec": time.time(),
                "message": message,
            }
        )
        _flush_uds_trace()
        original_send_json_message(sock, message)

    def _traced_recv_json_message(sock):
        message = original_recv_json_message(sock)
        uds_messages.append(
            {
                "direction": "response",
                "timestamp_sec": time.time(),
                "message": message,
            }
        )
        _flush_uds_trace()
        return message

    monkeypatch.setattr(sim_manager_module, "send_json_message", _traced_send_json_message)
    monkeypatch.setattr(sim_manager_module, "recv_json_message", _traced_recv_json_message)

    manager = SimManager(
        session_dir=tmp_path / "session",
        control_socket_path=tmp_path / "control.sock",
        python_bin=_worker_python(),
    )
    try:
        yield SimManagerTraceHarness(
            manager=manager,
            uds_trace_path=uds_trace_path,
            uds_messages=uds_messages,
        )
    finally:
        manager.close()
        _flush_uds_trace()


def test_sim_manager_default_env_exercises_all_interfaces(
    sim_manager_harness: SimManagerTraceHarness,
) -> None:
    sim_manager = sim_manager_harness.manager
    # 先确认 SimManager 尚未启动 worker，后续通过高层方法自动拉起。
    assert sim_manager.is_running() is False

    hello_payload = sim_manager.hello()
    assert hello_payload["worker"]["status"] == "ready"
    assert hello_payload["table_env"] == {"loaded": False, "id": None}
    assert hello_payload["objects"]["object_count"] == 0
    assert hello_payload["robot"] == {"status": "idle", "current_task_id": None}
    assert hello_payload["streams"]["active_count"] == 0
    assert sim_manager.is_running() is True

    list_table_env_payload = sim_manager.list_table_env()
    assert {item["id"] for item in list_table_env_payload["table_envs"]} == {"default", "ycb"}
    assert list_table_env_payload["table_env_count"] == 2

    api_text = sim_manager.list_api()
    assert api_text == get_robot_api_text()
    assert "pick_and_place(" in api_text
    assert "pick_position: list[float]" in api_text
    assert "grasp_offset: list[float] | None = None" in api_text

    list_camera_payload = sim_manager.list_camera()
    assert list_camera_payload["camera_count"] == 2
    assert [item["id"] for item in list_camera_payload["cameras"]] == [
        "table_overview",
        "table_top",
    ]

    robot_status_before_load = sim_manager.get_robot_status()
    assert robot_status_before_load["robot"] == {"status": "idle", "current_task_id": None}

    load_table_env_payload = sim_manager.load_table_env("default")
    assert load_table_env_payload["table_env"] == {"id": "default", "status": "loaded"}
    assert load_table_env_payload["object_count"] == 2
    assert {item["id"] for item in load_table_env_payload["objects"]} == {"red_cube", "blue_cube"}

    table_env_objects_payload = sim_manager.get_table_env_objects_info()
    assert table_env_objects_payload["table_env"] == {"loaded": True, "id": "default"}
    assert table_env_objects_payload["object_count"] == 2
    assert {item["id"] for item in table_env_objects_payload["objects"]} == {"red_cube", "blue_cube"}
    for object_payload in table_env_objects_payload["objects"]:
        _assert_object_transform_payload(object_payload)

    # 额外把对象信息落盘，便于人工对照 SimManager 这一层返回的数据结构。
    saved_payload_dir = sim_manager.session_dir.parent / "sim_manager_saved_payloads"
    saved_payload_dir.mkdir(parents=True, exist_ok=True)
    (saved_payload_dir / "default_table_env_objects_info.json").write_text(
        json.dumps(table_env_objects_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    expected_camera_prim_paths = {
        "table_top": "/World/Cameras/TableTopCamera",
        "table_overview": "/World/Cameras/TableOverviewCamera",
    }
    for camera_id, prim_path in expected_camera_prim_paths.items():
        camera_info_payload = sim_manager.get_camera_info(camera_id)
        camera_payload = camera_info_payload["camera"]
        assert camera_payload["id"] == camera_id
        assert camera_payload["status"] == "ready"
        assert camera_payload["prim_path"] == prim_path
        assert camera_payload["resolution"] == [640, 640]
        assert camera_payload["intrinsics"]["width"] == 640
        assert camera_payload["intrinsics"]["height"] == 640
        assert camera_payload["intrinsics"]["fx"] > 0.0
        assert camera_payload["intrinsics"]["fy"] > 0.0
        assert len(camera_payload["pose"]["position_xyz_m"]) == 3
        assert len(camera_payload["pose"]["quaternion_wxyz"]) == 4
        assert Path(camera_payload["rgb_image"]["ref"]["path"]).exists()
        assert Path(camera_payload["depth_image"]["ref"]["path"]).exists()

    top_stream_response = sim_manager.start_camera_stream("table_top")
    top_stream_payload = top_stream_response["stream"]
    assert top_stream_response["camera"] == {"id": "table_top"}
    _assert_stream_shared_memory_header(top_stream_payload)

    overview_stream_response = sim_manager.start_camera_stream("table_overview")
    overview_stream_payload = overview_stream_response["stream"]
    assert overview_stream_response["camera"] == {"id": "table_overview"}
    _assert_stream_shared_memory_header(overview_stream_payload)

    hello_after_stream_start = sim_manager.hello()
    assert hello_after_stream_start["streams"]["active_count"] == 2

    baseline_fps = {
        "table_top": _measure_stream_fps(top_stream_payload),
        "table_overview": _measure_stream_fps(overview_stream_payload),
    }
    for camera_id, observed_fps in baseline_fps.items():
        assert _STREAM_FPS_MIN <= observed_fps <= _STREAM_FPS_MAX, (
            f"camera {camera_id} baseline observed_fps={observed_fps:.2f} "
            f"is outside expected range [{_STREAM_FPS_MIN:.1f}, {_STREAM_FPS_MAX:.1f}]"
        )

    task_objects = table_env_objects_payload["objects"]
    run_task_code = textwrap.dedent(
        """
        def run(robot, objects):
            red_cube = next(obj for obj in objects if obj["id"] == "red_cube")
            blue_cube = next(obj for obj in objects if obj["id"] == "blue_cube")
            target_center_z = (
                blue_cube["pose"]["position_xyz_m"][2]
                + (blue_cube["scale_xyz"][2] / 2)
                + (red_cube["scale_xyz"][2] / 2)
                + 0.03
            )

            robot.pick_and_place(
                pick_position=red_cube["pose"]["position_xyz_m"],
                place_position=[
                    blue_cube["pose"]["position_xyz_m"][0],
                    blue_cube["pose"]["position_xyz_m"][1],
                    target_center_z,
                ],
                rotation=None,
                grasp_offset=None,
            )
        """
    ).strip() + "\n"

    run_task_stream_dir = sim_manager.session_dir.parent / "sim_manager_run_task_stream_samples"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        run_task_future = executor.submit(
            sim_manager.run_task,
            task_id="task-sim-manager-stream-check",
            objects=task_objects,
            code=run_task_code,
        )
        during_task_metrics = _sample_streams_while_run_task_is_running(
            top_stream_payload=top_stream_payload,
            overview_stream_payload=overview_stream_payload,
            output_dir=run_task_stream_dir,
            run_task_future=run_task_future,
        )
        run_task_payload = run_task_future.result(timeout=_RUN_TASK_REQUEST_TIMEOUT_SEC)

    assert run_task_payload["task"]["id"] == "task-sim-manager-stream-check"
    assert run_task_payload["task"]["status"] == "succeeded"
    assert run_task_payload["task"]["result"] is None
    assert isinstance(run_task_payload["task"]["started_at"], str) and run_task_payload["task"]["started_at"]
    assert isinstance(run_task_payload["task"]["finished_at"], str) and run_task_payload["task"]["finished_at"]

    hello_after_run_task = sim_manager.hello()
    assert hello_after_run_task["robot"] == {"status": "idle", "current_task_id": None}
    assert hello_after_run_task["streams"]["active_count"] == 2
    for metrics in during_task_metrics.values():
        assert metrics["saved_sample_count"] >= 1

    (run_task_stream_dir / "run_task_stream_comparison.json").write_text(
        json.dumps(
            {
                "task_id": "task-sim-manager-stream-check",
                "baseline_fps": baseline_fps,
                "during_run_task": {
                    camera_id: {
                        "observed_fps_during_run_task": metrics["observed_fps_during_run_task"],
                        "saved_sample_count": metrics["saved_sample_count"],
                    }
                    for camera_id, metrics in during_task_metrics.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    stop_top_stream_payload = sim_manager.stop_camera_stream(top_stream_payload["id"])
    assert stop_top_stream_payload["stream"] == {"id": top_stream_payload["id"], "status": "stopped"}

    stop_overview_stream_payload = sim_manager.stop_camera_stream(overview_stream_payload["id"])
    assert stop_overview_stream_payload["stream"] == {
        "id": overview_stream_payload["id"],
        "status": "stopped",
    }

    hello_after_stream_stop = sim_manager.hello()
    assert hello_after_stream_stop["streams"]["active_count"] == 0

    shutdown_payload = sim_manager.shutdown()
    assert shutdown_payload["worker"] == {"status": "shutting_down"}
    assert sim_manager.is_running() is False

    # 这个案例要求把所有经由 UDS 的输入/输出 JSON 都落盘，方便后续人工复盘。
    assert sim_manager_harness.uds_trace_path.exists()
    assert len(sim_manager_harness.uds_messages) >= 2

    request_command_types = [
        item["message"]["command_type"]
        for item in sim_manager_harness.uds_messages
        if item["direction"] == "request" and isinstance(item.get("message"), dict)
    ]
    for expected_command_type in [
        "hello",
        "list_table_env",
        "list_api",
        "list_camera",
        "get_robot_status",
        "load_table_env",
        "get_table_env_objects_info",
        "get_camera_info",
        "start_camera_stream",
        "run_task",
        "stop_camera_stream",
        "shutdown",
    ]:
        assert expected_command_type in request_command_types

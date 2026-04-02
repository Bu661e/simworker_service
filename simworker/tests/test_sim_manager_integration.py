from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from simworker.sim_manager import SimManager, SimManagerError

_ENABLE_REAL_TEST_ENV = "SIMWORKER_RUN_ISAACSIM_TESTS"
_WORKER_PYTHON_ENV = "SIMWORKER_TEST_PYTHON"


def _worker_python() -> str:
    # 默认复用当前 pytest 解释器；真实 Isaac Sim 联调时再通过环境变量切到 python.sh。
    return os.environ.get(_WORKER_PYTHON_ENV, sys.executable)


def test_sim_manager_roundtrip(tmp_path: Path) -> None:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    manager = SimManager(
        session_dir=tmp_path / "session",
        control_socket_path=tmp_path / "control.sock",
        python_bin=_worker_python(),
    )
    try:
        # 不显式调用 start()，直接验证 SimManager 会在首次请求时自动拉起 worker。
        hello_payload = manager.hello()
        assert hello_payload["worker"]["status"] == "ready"
        assert manager.process_log_path.exists()
        assert manager.is_running() is True

        list_table_env_payload = manager.list_table_env()
        assert {item["id"] for item in list_table_env_payload["table_envs"]} == {"default", "ycb"}
        assert list_table_env_payload["table_env_count"] == 2

        list_camera_payload = manager.list_camera()
        assert [item["id"] for item in list_camera_payload["cameras"]] == ["table_overview", "table_top"]
        assert list_camera_payload["camera_count"] == 2

        robot_payload = manager.get_robot_status()
        assert robot_payload["robot"] == {"status": "idle", "current_task_id": None}

        load_payload = manager.load_table_env("default")
        assert load_payload["table_env"] == {"id": "default", "status": "loaded"}
        assert load_payload["object_count"] == 2

        objects_payload = manager.get_table_env_objects_info()
        assert objects_payload["table_env"] == {"loaded": True, "id": "default"}
        assert {item["id"] for item in objects_payload["objects"]} == {"red_cube", "blue_cube"}

        stream_payload = manager.start_camera_stream("table_top")
        assert stream_payload["camera"] == {"id": "table_top"}
        assert stream_payload["stream"]["status"] == "running"
        assert stream_payload["stream"]["buffer_mode"] == "latest_frame"
        assert stream_payload["stream"]["ref"]["kind"] == "shared_memory"

        hello_after_stream_start = manager.hello()
        assert hello_after_stream_start["streams"]["active_count"] == 1

        stop_stream_payload = manager.stop_camera_stream(stream_payload["stream"]["id"])
        assert stop_stream_payload["stream"] == {
            "id": stream_payload["stream"]["id"],
            "status": "stopped",
        }

        hello_after_stream_stop = manager.hello()
        assert hello_after_stream_stop["streams"]["active_count"] == 0

        camera_payload = manager.get_camera_info("table_top")
        rgb_path = Path(camera_payload["camera"]["rgb_image"]["ref"]["path"])
        depth_path = Path(camera_payload["camera"]["depth_image"]["ref"]["path"])
        assert rgb_path.exists()
        assert depth_path.exists()

        with pytest.raises(SimManagerError) as exc_info:
            manager.get_camera_info("missing_camera")
        assert exc_info.value.command_type == "get_camera_info"
        assert str(exc_info.value) == "camera.id missing_camera does not exist"

        shutdown_payload = manager.shutdown()
        assert shutdown_payload["worker"]["status"] == "shutting_down"
        assert manager.is_running() is False
    finally:
        manager.close()

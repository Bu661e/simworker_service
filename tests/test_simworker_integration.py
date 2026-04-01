from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from simworker.protocol import recv_json_message, send_json_message

_ENABLE_REAL_TEST_ENV = "SIMWORKER_RUN_ISAACSIM_TESTS"
_WORKER_PYTHON_ENV = "SIMWORKER_TEST_PYTHON"
_STARTUP_TIMEOUT_SEC = 240.0
_SHUTDOWN_TIMEOUT_SEC = 60.0
_REQUEST_TIMEOUT_SEC = 60.0
_YCB_ASSET_ROOT = Path("/root/Downloads/YCB/Axis_Aligned_Physics")


@dataclass(frozen=True)
class WorkerProcessHandle:
    process: subprocess.Popen[str]
    session_dir: Path
    socket_path: Path
    log_path: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _worker_python() -> str:
    # 默认复用当前 pytest 解释器；如果要切到 Isaac Sim 自带 python.sh，可通过环境变量覆写。
    return os.environ.get(_WORKER_PYTHON_ENV, sys.executable)


def _tail_text_file(path: Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-max_chars:]


def _wait_for_socket(socket_path: Path, process: subprocess.Popen[str], log_path: Path) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SEC
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout_tail = _tail_text_file(log_path)
            raise RuntimeError(
                "simworker 子进程在控制 socket 就绪前退出。\n"
                f"returncode={process.returncode}\n"
                f"log_tail=\n{stdout_tail}"
            )

        if socket_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(str(socket_path))
                return
            except OSError as exc:
                last_error = exc
        time.sleep(0.2)

    stdout_tail = _tail_text_file(log_path)
    raise TimeoutError(
        "等待 simworker 控制 socket 超时。\n"
        f"socket_path={socket_path}\n"
        f"last_error={last_error!r}\n"
        f"log_tail=\n{stdout_tail}"
    )


def _send_request(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_REQUEST_TIMEOUT_SEC)
        sock.connect(str(socket_path))
        send_json_message(sock, request)
        response = recv_json_message(sock)
    if response is None:
        raise RuntimeError(f"simworker 返回了空响应: request={request}")
    return response


def _assert_success(response: dict[str, Any], request_id: str) -> dict[str, Any]:
    assert response["request_id"] == request_id
    assert response["ok"] is True
    return response["payload"]


def _assert_object_transform_payload(object_payload: dict[str, Any]) -> None:
    assert isinstance(object_payload["id"], str) and object_payload["id"]

    pose = object_payload["pose"]
    assert len(pose["position_xyz_m"]) == 3
    assert len(pose["quaternion_wxyz"]) == 4
    assert all(isinstance(value, float) for value in pose["position_xyz_m"])
    assert all(isinstance(value, float) for value in pose["quaternion_wxyz"])

    scale_xyz = object_payload["scale_xyz"]
    assert len(scale_xyz) == 3
    assert all(isinstance(value, float) for value in scale_xyz)
    assert all(value > 0.0 for value in scale_xyz)


def _shutdown_worker(handle: WorkerProcessHandle) -> None:
    if handle.process.poll() is not None:
        return

    try:
        if handle.socket_path.exists():
            response = _send_request(
                handle.socket_path,
                {
                    "request_id": "req-shutdown",
                    "command_type": "shutdown",
                    "payload": {},
                },
            )
            assert response["ok"] is True
    except Exception:
        # teardown 阶段优先保证子进程能回收，关闭失败时后面会直接 terminate。
        pass

    try:
        handle.process.wait(timeout=_SHUTDOWN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=10.0)


@pytest.fixture
def simworker_process(tmp_path: Path) -> WorkerProcessHandle:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    session_dir = tmp_path / "session"
    socket_path = tmp_path / "control.sock"
    log_path = tmp_path / "simworker.log"
    session_dir.mkdir(parents=True, exist_ok=True)

    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            _worker_python(),
            "-m",
            "simworker.entrypoint",
            "--session-dir",
            str(session_dir),
            "--control-socket-path",
            str(socket_path),
        ],
        cwd=_repo_root(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )

    handle = WorkerProcessHandle(
        process=process,
        session_dir=session_dir,
        socket_path=socket_path,
        log_path=log_path,
    )

    try:
        _wait_for_socket(socket_path, process, log_path)
        yield handle
    finally:
        _shutdown_worker(handle)
        log_handle.close()


def test_simworker_camera_roundtrip(simworker_process: WorkerProcessHandle) -> None:
    hello_response = _send_request(
        simworker_process.socket_path,
        {
            "request_id": "req-hello",
            "command_type": "hello",
            "payload": {},
        },
    )
    assert hello_response["ok"] is True
    assert hello_response["payload"]["worker"]["status"] == "ready"
    assert hello_response["payload"]["objects"]["object_count"] == 0

    list_env_response = _send_request(
        simworker_process.socket_path,
        {
            "request_id": "req-list-env",
            "command_type": "list_table_env",
            "payload": {},
        },
    )
    assert list_env_response["ok"] is True
    returned_env_ids = {item["id"] for item in list_env_response["payload"]["table_envs"]}
    assert returned_env_ids == {"default", "ycb"}
    assert list_env_response["payload"]["table_env_count"] == 2

    list_camera_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-list-camera",
                "command_type": "list_camera",
                "payload": {},
            },
        ),
        "req-list-camera",
    )
    assert list_camera_payload["camera_count"] == 2
    assert [item["id"] for item in list_camera_payload["cameras"]] == [
        "table_overview",
        "table_top",
    ]

    robot_status_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-robot-status-before-load",
                "command_type": "get_robot_status",
                "payload": {},
            },
        ),
        "req-robot-status-before-load",
    )
    assert robot_status_payload["robot"] == {"status": "idle", "current_task_id": None}

    # 先加载一套桌面环境，再抓相机，保证相机快照里已经包含桌面物体。
    load_default_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-load-default-before-camera",
                "command_type": "load_table_env",
                "payload": {
                    "table_env_id": "default",
                },
            },
        ),
        "req-load-default-before-camera",
    )
    assert load_default_payload["table_env"] == {"id": "default", "status": "loaded"}
    assert load_default_payload["object_count"] == 2

    table_env_objects_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-table-env-objects-before-camera",
                "command_type": "get_table_env_objects_info",
                "payload": {},
            },
        ),
        "req-table-env-objects-before-camera",
    )
    assert table_env_objects_payload["table_env"] == {"loaded": True, "id": "default"}
    assert table_env_objects_payload["object_count"] == 2
    assert {item["id"] for item in table_env_objects_payload["objects"]} == {"red_cube", "blue_cube"}
    for object_payload in table_env_objects_payload["objects"]:
        _assert_object_transform_payload(object_payload)

    # 当前基础环境里固定有两个相机，两个都做一遍快照与元信息校验。
    expected_camera_prim_paths = {
        "table_top": "/World/Cameras/TableTopCamera",
        "table_overview": "/World/Cameras/TableOverviewCamera",
    }
    for camera_id, prim_path in expected_camera_prim_paths.items():
        camera_response = _send_request(
            simworker_process.socket_path,
            {
                "request_id": f"req-camera-{camera_id}",
                "command_type": "get_camera_info",
                "payload": {
                    "camera": {
                        "id": camera_id,
                    }
                },
            },
        )
        assert camera_response["ok"] is True

        camera_payload = camera_response["payload"]["camera"]
        assert camera_payload["id"] == camera_id
        assert camera_payload["status"] == "ready"
        assert camera_payload["prim_path"] == prim_path
        assert camera_payload["resolution"] == [640, 640]

        intrinsics = camera_payload["intrinsics"]
        assert intrinsics["width"] == 640
        assert intrinsics["height"] == 640
        assert intrinsics["fx"] > 0.0
        assert intrinsics["fy"] > 0.0

        pose = camera_payload["pose"]
        assert len(pose["position_xyz_m"]) == 3
        assert len(pose["quaternion_wxyz"]) == 4

        rgb_ref = camera_payload["rgb_image"]["ref"]
        rgb_path = Path(rgb_ref["path"])
        assert rgb_ref["kind"] == "artifact_file"
        assert rgb_ref["content_type"] == "image/png"
        assert rgb_path.suffix == ".png"
        assert rgb_path.exists()
        assert rgb_path.stat().st_size > 0
        assert rgb_path.parent.name == "artifacts"
        assert simworker_process.session_dir in rgb_path.parents

        depth_ref = camera_payload["depth_image"]["ref"]
        depth_path = Path(depth_ref["path"])
        assert camera_payload["depth_image"]["unit"] == "meter"
        assert depth_ref["kind"] == "artifact_file"
        assert depth_ref["content_type"] == "application/x-npy"
        assert depth_path.suffix == ".npy"
        assert depth_path.exists()
        assert depth_path.stat().st_size > 0
        assert depth_path.parent.name == "artifacts"
        assert simworker_process.session_dir in depth_path.parents

    invalid_camera_response = _send_request(
        simworker_process.socket_path,
        {
            "request_id": "req-camera-missing",
            "command_type": "get_camera_info",
            "payload": {
                "camera": {
                    "id": "missing_camera",
                }
            },
        },
    )
    assert invalid_camera_response["ok"] is False
    assert invalid_camera_response["error_message"] == "camera.id missing_camera does not exist"


def test_simworker_load_default_table_env_and_query_objects(simworker_process: WorkerProcessHandle) -> None:
    before_load_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-objects-before-load",
                "command_type": "get_table_env_objects_info",
                "payload": {},
            },
        ),
        "req-objects-before-load",
    )
    assert before_load_payload["table_env"] == {"loaded": False, "id": None}
    assert before_load_payload["object_count"] == 0
    assert before_load_payload["objects"] == []

    load_default_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-load-default",
                "command_type": "load_table_env",
                "payload": {
                    "table_env_id": "default",
                },
            },
        ),
        "req-load-default",
    )
    assert load_default_payload["table_env"] == {"id": "default", "status": "loaded"}
    assert load_default_payload["object_count"] == 2
    assert {item["id"] for item in load_default_payload["objects"]} == {"red_cube", "blue_cube"}

    # 相同 table_env 重复加载应保持幂等。
    reload_default_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-reload-default",
                "command_type": "load_table_env",
                "payload": {
                    "table_env_id": "default",
                },
            },
        ),
        "req-reload-default",
    )
    assert reload_default_payload["table_env"] == {"id": "default", "status": "loaded"}
    assert reload_default_payload["object_count"] == 2
    assert {item["id"] for item in reload_default_payload["objects"]} == {"red_cube", "blue_cube"}

    objects_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-objects-after-load",
                "command_type": "get_table_env_objects_info",
                "payload": {},
            },
        ),
        "req-objects-after-load",
    )
    assert objects_payload["table_env"] == {"loaded": True, "id": "default"}
    assert objects_payload["object_count"] == 2
    returned_objects = {item["id"]: item for item in objects_payload["objects"]}
    assert set(returned_objects) == {"red_cube", "blue_cube"}
    for object_payload in returned_objects.values():
        _assert_object_transform_payload(object_payload)

    hello_after_load_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-hello-after-load",
                "command_type": "hello",
                "payload": {},
            },
        ),
        "req-hello-after-load",
    )
    assert hello_after_load_payload["table_env"] == {"loaded": True, "id": "default"}
    assert hello_after_load_payload["objects"]["object_count"] == 2

    switch_env_response = _send_request(
        simworker_process.socket_path,
        {
            "request_id": "req-switch-env",
            "command_type": "load_table_env",
            "payload": {
                "table_env_id": "ycb",
            },
        },
    )
    assert switch_env_response["request_id"] == "req-switch-env"
    assert switch_env_response["ok"] is False
    assert (
        switch_env_response["error_message"]
        == "table_env_id ycb does not match current loaded table_env_id default"
    )


def test_simworker_rejects_unsupported_table_env(simworker_process: WorkerProcessHandle) -> None:
    invalid_env_response = _send_request(
        simworker_process.socket_path,
        {
            "request_id": "req-invalid-env",
            "command_type": "load_table_env",
            "payload": {
                "table_env_id": "unknown_env",
            },
        },
    )
    assert invalid_env_response["request_id"] == "req-invalid-env"
    assert invalid_env_response["ok"] is False
    assert (
        invalid_env_response["error_message"]
        == "unsupported table_env_id: unknown_env; supported values: default, ycb"
    )


def test_simworker_load_ycb_table_env_and_query_objects(simworker_process: WorkerProcessHandle) -> None:
    if not _YCB_ASSET_ROOT.exists():
        pytest.skip(f"YCB 资产目录不存在: {_YCB_ASSET_ROOT}")

    load_ycb_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-load-ycb",
                "command_type": "load_table_env",
                "payload": {
                    "table_env_id": "ycb",
                },
            },
        ),
        "req-load-ycb",
    )
    assert load_ycb_payload["table_env"] == {"id": "ycb", "status": "loaded"}
    assert load_ycb_payload["object_count"] == 2
    assert {item["id"] for item in load_ycb_payload["objects"]} == {
        "cracker_box_1",
        "mustard_bottle_1",
    }

    objects_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-ycb-objects",
                "command_type": "get_table_env_objects_info",
                "payload": {},
            },
        ),
        "req-ycb-objects",
    )
    assert objects_payload["table_env"] == {"loaded": True, "id": "ycb"}
    assert objects_payload["object_count"] == 2
    returned_objects = {item["id"]: item for item in objects_payload["objects"]}
    assert set(returned_objects) == {"cracker_box_1", "mustard_bottle_1"}
    for object_payload in returned_objects.values():
        _assert_object_transform_payload(object_payload)

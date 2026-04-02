from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

import pytest

from simworker.camera_streams import decode_latest_frame_header, latest_frame_header_size_bytes
from simworker.protocol import recv_json_message, send_json_message

_ENABLE_REAL_TEST_ENV = "SIMWORKER_RUN_ISAACSIM_TESTS"
_WORKER_PYTHON_ENV = "SIMWORKER_TEST_PYTHON"
_STARTUP_TIMEOUT_SEC = 240.0
_SHUTDOWN_TIMEOUT_SEC = 60.0
_REQUEST_TIMEOUT_SEC = 60.0
_YCB_ASSET_ROOT_CANDIDATES = (
    Path("/root/Download/YCB/Axis_Aligned_Physics"),
    Path("/root/Downloads/YCB/Axis_Aligned_Physics"),
)
_STREAM_SAMPLE_INTERVAL_SEC = 1.0
_STREAM_SAMPLE_COUNT = 3
_EXPECTED_STREAM_FPS = 30.0
_STREAM_FPS_MIN = 24.0
_STREAM_FPS_MAX = 36.0


@dataclass(frozen=True)
class WorkerProcessHandle:
    process: subprocess.Popen[str]
    session_dir: Path
    socket_path: Path
    log_path: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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


def _stream_ref_name(stream_ref_path: str) -> str:
    if not stream_ref_path.startswith("shm://"):
        raise ValueError(f"unexpected shared memory path: {stream_ref_path}")
    return stream_ref_path.removeprefix("shm://")


def _read_latest_stream_snapshot(
    shm: shared_memory.SharedMemory,
    *,
    timeout_sec: float = 2.0,
) -> tuple[dict[str, Any], bytes]:
    """按 latest-frame 的奇偶 seq 协议读取一份自洽快照。"""
    header_size = latest_frame_header_size_bytes()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        header_start = decode_latest_frame_header(shm.buf[:header_size])
        if header_start["seq"] % 2 == 1 or header_start["data_size_bytes"] <= 0:
            time.sleep(0.01)
            continue

        data_size_bytes = header_start["data_size_bytes"]
        frame_bytes = bytes(shm.buf[header_size : header_size + data_size_bytes])
        header_end = decode_latest_frame_header(shm.buf[:header_size])
        if header_start["seq"] != header_end["seq"]:
            continue
        if header_end["seq"] % 2 == 1 or header_end["data_size_bytes"] <= 0:
            continue
        return header_end, frame_bytes

    raise TimeoutError("timed out waiting for a readable latest-frame snapshot")


def _wait_for_stream_frame_advance(
    shm: shared_memory.SharedMemory,
    *,
    last_frame_id: int,
    timeout_sec: float = 2.0,
) -> tuple[dict[str, Any], bytes]:
    """等待 stream 至少再产生一帧。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        header, frame_bytes = _read_latest_stream_snapshot(shm, timeout_sec=0.5)
        if header["frame_id"] > last_frame_id:
            return header, frame_bytes
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for stream frame to advance past frame_id={last_frame_id}")


def _save_stream_frame_png(
    frame_bytes: bytes,
    *,
    width: int,
    height: int,
    output_path: Path,
) -> None:
    """把 rgb24 原始帧保存成 PNG，方便人工查看 stream 内容。"""
    import numpy as np
    from PIL import Image

    image_array = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
    Image.fromarray(image_array, mode="RGB").save(output_path)


def _find_existing_ycb_asset_root() -> Path | None:
    for candidate in _YCB_ASSET_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


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


def _assert_camera_snapshot_payload(
    simworker_process: WorkerProcessHandle,
    *,
    camera_id: str,
    prim_path: str,
) -> None:
    """校验单个相机快照返回值，并确认 artifact 已真实落盘。"""
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


def _assert_simple_interface_sequence(
    simworker_process: WorkerProcessHandle,
    *,
    table_env_id: str,
    expected_object_ids: set[str],
    save_objects_info_json: bool = False,
) -> None:
    """按固定顺序走完 7 个简单接口，保证拍照时桌面环境已经完成导入。"""
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

    load_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-load-before-camera",
                "command_type": "load_table_env",
                "payload": {
                    "table_env_id": table_env_id,
                },
            },
        ),
        "req-load-before-camera",
    )
    assert load_payload["table_env"] == {"id": table_env_id, "status": "loaded"}
    assert load_payload["object_count"] == len(expected_object_ids)
    assert {item["id"] for item in load_payload["objects"]} == expected_object_ids

    table_env_objects_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-table-env-objects",
                "command_type": "get_table_env_objects_info",
                "payload": {},
            },
        ),
        "req-table-env-objects",
    )
    assert table_env_objects_payload["table_env"] == {"loaded": True, "id": table_env_id}
    assert table_env_objects_payload["object_count"] == len(expected_object_ids)
    assert {item["id"] for item in table_env_objects_payload["objects"]} == expected_object_ids
    for object_payload in table_env_objects_payload["objects"]:
        _assert_object_transform_payload(object_payload)

    if save_objects_info_json:
        # 前两个综合案例会把 get_table_env_objects_info 的返回 payload 落盘，
        # 方便后续人工核对桌面环境里对象的位姿与缩放信息。
        saved_payload_dir = simworker_process.session_dir.parent / "saved_payloads"
        saved_payload_dir.mkdir(parents=True, exist_ok=True)
        saved_payload_path = saved_payload_dir / f"{table_env_id}_table_env_objects_info.json"
        saved_payload_path.write_text(
            json.dumps(table_env_objects_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        assert saved_payload_path.exists()

    # 拍照放在最后执行，人工看图片时就能直观看到桌面环境是否已真正加载进场景。
    expected_camera_prim_paths = {
        "table_top": "/World/Cameras/TableTopCamera",
        "table_overview": "/World/Cameras/TableOverviewCamera",
    }
    for camera_id, prim_path in expected_camera_prim_paths.items():
        _assert_camera_snapshot_payload(
            simworker_process,
            camera_id=camera_id,
            prim_path=prim_path,
        )


def _assert_stream_shared_memory_header(stream_payload: dict[str, Any]) -> str:
    """打开共享内存，确认 header 已经包含一帧可读 RGB 数据。"""
    assert stream_payload["status"] == "running"
    assert stream_payload["buffer_mode"] == "latest_frame"
    assert stream_payload["pixel_format"] == "rgb24"
    assert stream_payload["resolution"] == [640, 640]
    assert stream_payload["ref"]["kind"] == "shared_memory"
    assert stream_payload["ref"]["layout"] == "latest_frame_v1"

    shm_name = _stream_ref_name(stream_payload["ref"]["path"])
    header_size = latest_frame_header_size_bytes()
    shm = shared_memory.SharedMemory(name=shm_name, create=False)
    try:
        deadline = time.monotonic() + 5.0
        header = decode_latest_frame_header(shm.buf[:header_size])
        while time.monotonic() < deadline and header["data_size_bytes"] == 0:
            time.sleep(0.1)
            header = decode_latest_frame_header(shm.buf[:header_size])

        assert header["magic"] == "SIMSTRM1"
        assert header["layout"] == "latest_frame_v1"
        assert header["pixel_format"] == "rgb24"
        assert header["width"] == 640
        assert header["height"] == 640
        assert header["data_size_bytes"] > 0
        assert header["frame_capacity_bytes"] >= header["data_size_bytes"]
        assert header["frame_id"] >= 1
        assert header["seq"] % 2 == 0
    finally:
        shm.close()
    return shm_name


def _assert_stream_frequency_and_save_samples(
    stream_payload: dict[str, Any],
    *,
    camera_id: str,
    output_dir: Path,
) -> str:
    """
    验证 stream 实际发布频率接近当前约定值，并每秒保存一张流帧，供人工查看。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    shm_name = _stream_ref_name(stream_payload["ref"]["path"])
    shm = shared_memory.SharedMemory(name=shm_name, create=False)
    try:
        start_header, _ = _read_latest_stream_snapshot(shm, timeout_sec=5.0)
        start_frame_id = int(start_header["frame_id"])
        start_timestamp_ns = int(start_header["timestamp_ns"])
        last_frame_id = start_frame_id

        samples: list[dict[str, Any]] = []
        end_header = start_header
        for sample_index in range(1, _STREAM_SAMPLE_COUNT + 1):
            time.sleep(_STREAM_SAMPLE_INTERVAL_SEC)
            sample_header, frame_bytes = _wait_for_stream_frame_advance(
                shm,
                last_frame_id=last_frame_id,
                timeout_sec=2.0,
            )
            sample_path = output_dir / f"{camera_id}_sample_{sample_index:02d}.png"
            _save_stream_frame_png(
                frame_bytes,
                width=int(sample_header["width"]),
                height=int(sample_header["height"]),
                output_path=sample_path,
            )
            samples.append(
                {
                    "sample_index": sample_index,
                    "frame_id": int(sample_header["frame_id"]),
                    "timestamp_ns": int(sample_header["timestamp_ns"]),
                    "path": str(sample_path),
                }
            )
            last_frame_id = int(sample_header["frame_id"])
            end_header = sample_header

        elapsed_sec = (int(end_header["timestamp_ns"]) - start_timestamp_ns) / 1_000_000_000.0
        if elapsed_sec <= 0.0:
            raise AssertionError("stream sample elapsed time must be positive")
        observed_fps = (int(end_header["frame_id"]) - start_frame_id) / elapsed_sec
        assert _STREAM_FPS_MIN <= observed_fps <= _STREAM_FPS_MAX, (
            f"camera {camera_id} observed_fps={observed_fps:.2f} "
            f"is outside expected range [{_STREAM_FPS_MIN:.1f}, {_STREAM_FPS_MAX:.1f}]"
        )

        metrics_path = output_dir / f"{camera_id}_stream_metrics.json"
        metrics_path.write_text(
            json.dumps(
                {
                    "camera_id": camera_id,
                    "stream_id": stream_payload["id"],
                    "expected_fps": _EXPECTED_STREAM_FPS,
                    "observed_fps": observed_fps,
                    "sample_interval_sec": _STREAM_SAMPLE_INTERVAL_SEC,
                    "sample_count": _STREAM_SAMPLE_COUNT,
                    "samples": samples,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        shm.close()
    return shm_name


def test_simworker_default_env_simple_interfaces_and_two_camera_snapshots(
    simworker_process: WorkerProcessHandle,
) -> None:
    _assert_simple_interface_sequence(
        simworker_process,
        table_env_id="default",
        expected_object_ids={"red_cube", "blue_cube"},
        save_objects_info_json=True,
    )


def test_simworker_ycb_env_simple_interfaces_and_two_camera_snapshots(
    simworker_process: WorkerProcessHandle,
) -> None:
    ycb_asset_root = _find_existing_ycb_asset_root()
    if ycb_asset_root is None:
        checked_paths = ", ".join(str(path) for path in _YCB_ASSET_ROOT_CANDIDATES)
        pytest.skip(f"YCB 资产目录不存在: {checked_paths}")

    _assert_simple_interface_sequence(
        simworker_process,
        table_env_id="ycb",
        expected_object_ids={"cracker_box_1", "mustard_bottle_1"},
        save_objects_info_json=True,
    )


def test_simworker_default_env_two_camera_snapshots_and_dual_streams(
    simworker_process: WorkerProcessHandle,
) -> None:
    _assert_simple_interface_sequence(
        simworker_process,
        table_env_id="default",
        expected_object_ids={"red_cube", "blue_cube"},
    )
    stream_sample_dir = simworker_process.session_dir.parent / "stream_samples"

    top_stream_response = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-start-stream-table-top",
                "command_type": "start_camera_stream",
                "payload": {
                    "camera": {
                        "id": "table_top",
                    },
                    "stream": {
                        "buffer_mode": "latest_frame",
                    },
                },
            },
        ),
        "req-start-stream-table-top",
    )
    assert top_stream_response["camera"] == {"id": "table_top"}
    top_stream_payload = top_stream_response["stream"]
    _assert_stream_shared_memory_header(top_stream_payload)

    overview_stream_response = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-start-stream-table-overview",
                "command_type": "start_camera_stream",
                "payload": {
                    "camera": {
                        "id": "table_overview",
                    },
                    "stream": {
                        "buffer_mode": "latest_frame",
                    },
                },
            },
        ),
        "req-start-stream-table-overview",
    )
    assert overview_stream_response["camera"] == {"id": "table_overview"}
    overview_stream_payload = overview_stream_response["stream"]
    _assert_stream_shared_memory_header(overview_stream_payload)

    # 对同一个相机重复启动时应该复用已有流。
    restart_top_stream_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-start-stream-table-top-again",
                "command_type": "start_camera_stream",
                "payload": {
                    "camera": {
                        "id": "table_top",
                    },
                    "stream": {
                        "buffer_mode": "latest_frame",
                    },
                },
            },
        ),
        "req-start-stream-table-top-again",
    )
    assert restart_top_stream_payload["stream"]["id"] == top_stream_payload["id"]
    assert restart_top_stream_payload["stream"]["ref"]["path"] == top_stream_payload["ref"]["path"]

    # 在两路流都启动后，检查 active_count 并做帧率与采样图验证。
    hello_after_start_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-hello-after-stream-start",
                "command_type": "hello",
                "payload": {},
            },
        ),
        "req-hello-after-stream-start",
    )
    assert hello_after_start_payload["streams"]["active_count"] == 2

    top_shm_name = _assert_stream_frequency_and_save_samples(
        top_stream_payload,
        camera_id="table_top",
        output_dir=stream_sample_dir,
    )
    overview_shm_name = _assert_stream_frequency_and_save_samples(
        overview_stream_payload,
        camera_id="table_overview",
        output_dir=stream_sample_dir,
    )

    stop_top_stream_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-stop-stream-table-top",
                "command_type": "stop_camera_stream",
                "payload": {
                    "stream": {
                        "id": top_stream_payload["id"],
                    }
                },
            },
        ),
        "req-stop-stream-table-top",
    )
    assert stop_top_stream_payload["stream"] == {"id": top_stream_payload["id"], "status": "stopped"}

    hello_after_top_stop_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-hello-after-top-stream-stop",
                "command_type": "hello",
                "payload": {},
            },
        ),
        "req-hello-after-top-stream-stop",
    )
    assert hello_after_top_stop_payload["streams"]["active_count"] == 1

    stop_overview_stream_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-stop-stream-table-overview",
                "command_type": "stop_camera_stream",
                "payload": {
                    "stream": {
                        "id": overview_stream_payload["id"],
                    }
                },
            },
        ),
        "req-stop-stream-table-overview",
    )
    assert stop_overview_stream_payload["stream"] == {
        "id": overview_stream_payload["id"],
        "status": "stopped",
    }

    hello_after_stop_payload = _assert_success(
        _send_request(
            simworker_process.socket_path,
            {
                "request_id": "req-hello-after-all-stream-stop",
                "command_type": "hello",
                "payload": {},
            },
        ),
        "req-hello-after-all-stream-stop",
    )
    assert hello_after_stop_payload["streams"]["active_count"] == 0

    with pytest.raises(FileNotFoundError):
        orphan_shm = shared_memory.SharedMemory(name=top_shm_name, create=False)
        orphan_shm.close()

    with pytest.raises(FileNotFoundError):
        orphan_shm = shared_memory.SharedMemory(name=overview_shm_name, create=False)
        orphan_shm.close()

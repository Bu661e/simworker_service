from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi.testclient import TestClient
import numpy as np
import pytest

from api.mjpeg_stream import _open_mjpeg_stream, build_mjpeg_streaming_response
from api.main import ApiSettings, create_app
from simworker.camera_streams import create_camera_stream_runtime_state
from simworker import SimManagerError
from simworker.tests.test_simworker_integration import _ENABLE_REAL_TEST_ENV, _worker_python

_ONE_BY_ONE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
_MJPEG_SAMPLE_DURATION_SEC = 5.0
_MJPEG_RECEIVED_FPS_WARN_MIN = 20.0


class FakeSimManager:
    def __init__(self, *, rgb_path: Path, depth_path: Path) -> None:
        self.rgb_path = rgb_path
        self.depth_path = depth_path
        self.ensure_started_calls = 0
        self.close_calls = 0
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raise_on: dict[str, Exception] = {}
        self.stream_counter = 0
        self.stream_states: dict[str, object] = {}
        self.current_table_env_id: str | None = None

    def ensure_started(self) -> "FakeSimManager":
        self.ensure_started_calls += 1
        self.calls.append(("ensure_started", (), {}))
        return self

    def close(self) -> None:
        self.close_calls += 1
        self.calls.append(("close", (), {}))

    def hello(self) -> dict[str, Any]:
        self._maybe_raise("hello")
        self.calls.append(("hello", (), {}))
        return {
            "worker": {"status": "ready"},
            "table_env": {
                "loaded": self.current_table_env_id is not None,
                "id": self.current_table_env_id,
            },
            "objects": {"object_count": len(self._current_table_env_objects())},
            "robot": {"status": "idle", "current_task_id": None},
            "streams": {"active_count": 0},
        }

    def list_camera(self) -> dict[str, Any]:
        self._maybe_raise("list_camera")
        self.calls.append(("list_camera", (), {}))
        return {
            "cameras": [{"id": "table_overview"}, {"id": "table_top"}],
            "camera_count": 2,
        }

    def get_camera_info(self, camera_id: str) -> dict[str, Any]:
        self._maybe_raise("get_camera_info")
        self.calls.append(("get_camera_info", (camera_id,), {}))
        return {
            "camera": {
                "id": camera_id,
                "status": "ready",
                "prim_path": "/World/Cameras/TableTopCamera",
                "mount_mode": "world",
                "resolution": [640, 640],
                "intrinsics": {
                    "fx": 533.33,
                    "fy": 533.33,
                    "cx": 320.0,
                    "cy": 320.0,
                    "width": 640,
                    "height": 640,
                },
                "pose": {
                    "position_xyz_m": [0.0, 0.0, 6.0],
                    "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5],
                },
                "rgb_image": {
                    "ref": {
                        "id": "artifact-rgb-001",
                        "kind": "artifact_file",
                        "path": str(self.rgb_path),
                        "content_type": "image/png",
                    }
                },
                "depth_image": {
                    "unit": "meter",
                    "ref": {
                        "id": "artifact-depth-001",
                        "kind": "artifact_file",
                        "path": str(self.depth_path),
                        "content_type": "application/x-npy",
                    }
                },
            }
        }

    def list_table_env(self) -> dict[str, Any]:
        self._maybe_raise("list_table_env")
        self.calls.append(("list_table_env", (), {}))
        return {
            "table_envs": [{"id": "default"}, {"id": "multi_geometry"}, {"id": "ycb"}],
            "table_env_count": 3,
        }

    def load_table_env(self, table_env_id: str) -> dict[str, Any]:
        self._maybe_raise("load_table_env")
        self.calls.append(("load_table_env", (table_env_id,), {}))
        if self.current_table_env_id is not None and self.current_table_env_id != table_env_id:
            raise ValueError(
                f"table_env_id {table_env_id} does not match current loaded table_env_id {self.current_table_env_id}"
            )
        self.current_table_env_id = table_env_id
        return {
            "table_env": {"id": table_env_id, "status": "loaded"},
            "objects": [{"id": item["id"]} for item in self._current_table_env_objects()],
            "object_count": len(self._current_table_env_objects()),
        }

    def clear_table_env(self) -> dict[str, Any]:
        self._maybe_raise("clear_table_env")
        self.calls.append(("clear_table_env", (), {}))
        previous_table_env_id = self.current_table_env_id
        if previous_table_env_id is None:
            return {
                "table_env": {"loaded": False, "id": None, "status": "empty"},
                "previous_table_env_id": None,
                "object_count": 0,
                "objects": [],
            }
        self.current_table_env_id = None
        return {
            "table_env": {"loaded": False, "id": None, "status": "cleared"},
            "previous_table_env_id": previous_table_env_id,
            "object_count": 0,
            "objects": [],
        }

    def get_table_env_objects_info(self) -> dict[str, Any]:
        self._maybe_raise("get_table_env_objects_info")
        self.calls.append(("get_table_env_objects_info", (), {}))
        return {
            "table_env": {
                "loaded": self.current_table_env_id is not None,
                "id": self.current_table_env_id,
            },
            "object_count": len(self._current_table_env_objects()),
            "objects": self._current_table_env_objects(),
        }

    def get_robot_status(self) -> dict[str, Any]:
        self._maybe_raise("get_robot_status")
        self.calls.append(("get_robot_status", (), {}))
        return {
            "robot": {"status": "idle", "current_task_id": None},
        }

    def list_api(self) -> str:
        self._maybe_raise("list_api")
        self.calls.append(("list_api", (), {}))
        return "pick_and_place(...)"

    def start_camera_stream(self, camera_id: str, *, buffer_mode: str = "latest_frame") -> dict[str, Any]:
        self._maybe_raise("start_camera_stream")
        self.calls.append(("start_camera_stream", (camera_id,), {"buffer_mode": buffer_mode}))
        self.stream_counter += 1
        stream_id = f"stream-{camera_id}-{self.stream_counter:03d}"
        stream_state = create_camera_stream_runtime_state(
            stream_id=stream_id,
            ref_id=f"stream-ref-{camera_id}-{self.stream_counter:03d}",
            camera_id=camera_id,
            resolution=(2, 2),
        )
        rgba = np.array(
            [
                [[255, 0, 0, 255], [0, 255, 0, 255]],
                [[0, 0, 255, 255], [255, 255, 0, 255]],
            ],
            dtype=np.uint8,
        )
        stream_state.write_rgb_frame(rgba)
        self.stream_states[stream_id] = stream_state
        return {
            "camera": {"id": camera_id},
            "stream": stream_state.build_control_payload(),
        }

    def stop_camera_stream(self, stream_id: str) -> dict[str, Any]:
        self._maybe_raise("stop_camera_stream")
        self.calls.append(("stop_camera_stream", (stream_id,), {}))
        stream_state = self.stream_states.pop(stream_id, None)
        if stream_state is not None:
            stream_state.close()
        return {
            "stream": {"id": stream_id, "status": "stopped"},
        }

    def run_task(self, *, task_id: str, objects: list[dict[str, Any]], code: str) -> dict[str, Any]:
        self._maybe_raise("run_task")
        self.calls.append(("run_task", (), {"task_id": task_id, "objects": objects, "code": code}))
        return {
            "task": {
                "id": task_id,
                "status": "succeeded",
                "result": None,
                "started_at": "2026-04-03T00:00:00+00:00",
                "finished_at": "2026-04-03T00:00:05+00:00",
            }
        }

    def _maybe_raise(self, method_name: str) -> None:
        exc = self.raise_on.get(method_name)
        if exc is not None:
            raise exc

    def _current_table_env_objects(self) -> list[dict[str, Any]]:
        if self.current_table_env_id == "default":
            return [
                {
                    "id": "red_cube",
                    "pose": {
                        "position_xyz_m": [0.2, 0.0, 1.55],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.06, 0.06, 0.06],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.06, 0.06, 0.06],
                    },
                    "color": [1.0, 0.0, 0.0],
                },
                {
                    "id": "blue_cube",
                    "pose": {
                        "position_xyz_m": [0.3, 0.0, 1.55],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.06, 0.06, 0.06],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.06, 0.06, 0.06],
                    },
                    "color": [0.0, 0.0, 1.0],
                },
            ]
        if self.current_table_env_id == "ycb":
            return [
                {
                    "id": "cracker_box_1",
                    "pose": {
                        "position_xyz_m": [0.2, 0.18, 1.55],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.16, 0.06, 0.21],
                    "geometry": {
                        "type": "mesh",
                        "asset_filename": "003_cracker_box.usd",
                        "semantic_label": "cracker_box",
                    },
                    "color": None,
                },
                {
                    "id": "mustard_bottle_1",
                    "pose": {
                        "position_xyz_m": [0.34, -0.1, 1.55],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.08, 0.08, 0.19],
                    "geometry": {
                        "type": "mesh",
                        "asset_filename": "006_mustard_bottle.usd",
                        "semantic_label": "mustard_bottle",
                    },
                    "color": None,
                },
            ]
        if self.current_table_env_id == "multi_geometry":
            return [
                {
                    "id": "left_plate",
                    "pose": {
                        "position_xyz_m": [-0.34, 0.01, 1.5075],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.18, 0.18, 0.015],
                    "geometry": {
                        "type": "cylinder",
                        "radius_m": 0.09,
                        "height_m": 0.015,
                    },
                    "color": [0.15, 0.75, 0.85],
                },
                {
                    "id": "right_plate",
                    "pose": {
                        "position_xyz_m": [0.34, 0.01, 1.5075],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.18, 0.18, 0.015],
                    "geometry": {
                        "type": "cylinder",
                        "radius_m": 0.09,
                        "height_m": 0.015,
                    },
                    "color": [0.95, 0.55, 0.75],
                },
                {
                    "id": "red_cube",
                    "pose": {
                        "position_xyz_m": [-0.14, 0.12, 1.57],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.08, 0.08, 0.08],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.08, 0.08, 0.08],
                    },
                    "color": [1.0, 0.0, 0.0],
                },
                {
                    "id": "blue_cube",
                    "pose": {
                        "position_xyz_m": [0.0, 0.12, 1.57],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.08, 0.08, 0.08],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.08, 0.08, 0.08],
                    },
                    "color": [0.0, 0.0, 1.0],
                },
                {
                    "id": "green_block",
                    "pose": {
                        "position_xyz_m": [0.14, 0.12, 1.56],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.12, 0.08, 0.06],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.12, 0.08, 0.06],
                    },
                    "color": [0.0, 1.0, 0.0],
                },
                {
                    "id": "yellow_block",
                    "pose": {
                        "position_xyz_m": [-0.14, -0.1, 1.56],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.10, 0.07, 0.06],
                    "geometry": {
                        "type": "cuboid",
                        "size_xyz_m": [0.10, 0.07, 0.06],
                    },
                    "color": [1.0, 1.0, 0.0],
                },
                {
                    "id": "purple_cylinder",
                    "pose": {
                        "position_xyz_m": [0.0, -0.1, 1.575],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.08, 0.08, 0.09],
                    "geometry": {
                        "type": "cylinder",
                        "radius_m": 0.04,
                        "height_m": 0.09,
                    },
                    "color": [0.6, 0.0, 0.8],
                },
                {
                    "id": "orange_cylinder",
                    "pose": {
                        "position_xyz_m": [0.14, -0.1, 1.5725],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "bbox_size_xyz_m": [0.084, 0.084, 0.085],
                    "geometry": {
                        "type": "cylinder",
                        "radius_m": 0.042,
                        "height_m": 0.085,
                    },
                    "color": [1.0, 0.5, 0.0],
                },
            ]
        return []


class _NeverDisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _create_fake_manager(case_output_dir: Path) -> FakeSimManager:
    rgb_path = case_output_dir / "rgb.png"
    depth_path = case_output_dir / "depth.npy"
    rgb_path.write_bytes(_ONE_BY_ONE_PNG_BYTES)
    depth_path.write_bytes(b"fake-npy-data")

    return FakeSimManager(rgb_path=rgb_path, depth_path=depth_path)


def _create_test_client(case_output_dir: Path) -> tuple[TestClient, FakeSimManager]:
    fake_manager = _create_fake_manager(case_output_dir)
    app = create_app(
        settings=ApiSettings(control_socket_path=str(case_output_dir / "control.sock")),
        sim_manager_factory=lambda _: fake_manager,
        start_manager_on_startup=True,
    )
    return TestClient(app), fake_manager


async def _read_first_stream_chunk_and_close(response: Any) -> bytes:
    first_chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()
    return first_chunk


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _run_api_server_subprocess(case_output_dir: Path) -> Iterator[str]:
    host = "127.0.0.1"
    port = _pick_free_port()
    base_url = f"http://{host}:{port}"
    stdout_log_path = case_output_dir / "api.stdout.log"
    stderr_log_path = case_output_dir / "api.stderr.log"
    stdout_log = stdout_log_path.open("wb")
    stderr_log = stderr_log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            **os.environ,
            "SIMWORKER_SESSION_DIR": str(case_output_dir / "session"),
            "SIMWORKER_CONTROL_SOCKET_PATH": str(case_output_dir / "control.sock"),
            "SIMWORKER_PYTHON_BIN": _worker_python(),
        },
        stdout=stdout_log,
        stderr=stderr_log,
    )
    try:
        _wait_for_api_server_ready(process, base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        stdout_log.close()
        stderr_log.close()


def _wait_for_api_server_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 300.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"uvicorn exited early with code {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=5.0)
            if response.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1.0)
    raise AssertionError(f"API server did not become ready before timeout: {last_error}")


def _extract_mjpeg_boundary(content_type: str) -> bytes:
    prefix = "boundary="
    for part in content_type.split(";"):
        normalized = part.strip()
        if normalized.startswith(prefix):
            boundary_value = normalized[len(prefix) :].strip().strip('"')
            if boundary_value:
                return f"--{boundary_value}".encode("ascii")
    raise AssertionError(f"failed to parse MJPEG boundary from content-type: {content_type!r}")


def _extract_complete_mjpeg_frames(
    buffer: bytearray,
    *,
    boundary: bytes,
) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        boundary_index = buffer.find(boundary)
        if boundary_index < 0:
            if len(buffer) > len(boundary):
                del buffer[:-len(boundary)]
            break
        if boundary_index > 0:
            del buffer[:boundary_index]

        header_end = buffer.find(b"\r\n\r\n")
        if header_end < 0:
            break

        header_block = bytes(buffer[len(boundary) + 2 : header_end])
        header_lines = header_block.decode("ascii", errors="replace").split("\r\n")
        content_length: int | None = None
        for line in header_lines:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() == "content-length":
                content_length = int(value.strip())
                break
        if content_length is None:
            raise AssertionError("MJPEG part is missing Content-Length header")

        frame_start = header_end + 4
        frame_end = frame_start + content_length
        total_needed = frame_end + 2
        if len(buffer) < total_needed:
            break

        if buffer[frame_end:total_needed] != b"\r\n":
            raise AssertionError("MJPEG part is missing trailing CRLF")

        frames.append(bytes(buffer[frame_start:frame_end]))
        del buffer[:total_needed]
    return frames


def _measure_http_mjpeg_receive_fps(
    response: httpx.Response,
    *,
    duration_sec: float,
    camera_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    boundary = _extract_mjpeg_boundary(response.headers["content-type"])
    buffer = bytearray()
    frame_sizes: list[int] = []
    frame_timestamps: list[float] = []

    for chunk in response.iter_raw():
        buffer.extend(chunk)
        frames = _extract_complete_mjpeg_frames(buffer, boundary=boundary)
        for frame_bytes in frames:
            frame_timestamps.append(time.monotonic())
            frame_sizes.append(len(frame_bytes))
            if len(frame_timestamps) >= 2 and (frame_timestamps[-1] - frame_timestamps[0]) >= duration_sec:
                elapsed_sec = frame_timestamps[-1] - frame_timestamps[0]
                observed_fps = (len(frame_timestamps) - 1) / elapsed_sec
                metrics = {
                    "camera_id": camera_id,
                    "transport": "http_mjpeg",
                    "sample_duration_target_sec": duration_sec,
                    "frame_count": len(frame_timestamps),
                    "elapsed_sec": elapsed_sec,
                    "observed_fps": observed_fps,
                    "warn_if_below_fps": _MJPEG_RECEIVED_FPS_WARN_MIN,
                    "below_warn_threshold": observed_fps < _MJPEG_RECEIVED_FPS_WARN_MIN,
                    "mean_frame_size_bytes": sum(frame_sizes) / len(frame_sizes),
                    "min_frame_size_bytes": min(frame_sizes),
                    "max_frame_size_bytes": max(frame_sizes),
                }
                output_dir.mkdir(parents=True, exist_ok=True)
                metrics_path = output_dir / f"{camera_id}_mjpeg_receive_metrics.json"
                metrics_path.write_text(
                    json.dumps(metrics, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return metrics

    raise AssertionError("MJPEG stream ended before enough frames were received to measure FPS")


def test_health_endpoint_returns_ok_payload(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    with client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "worker": {"status": "ready"},
        "table_env": {"loaded": False, "id": None},
        "objects": {"object_count": 0},
        "robot": {"status": "idle", "current_task_id": None},
        "streams": {"active_count": 0},
    }
    assert fake_manager.ensure_started_calls == 1


def test_capture_endpoint_returns_json_payload_with_download_urls(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    with client:
        response = client.post("/cameras/table_top/capture")
    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["ok"] is True
    assert response_payload["camera"]["id"] == "table_top"
    assert response_payload["capture"]["id"].startswith("capture-")
    assert response_payload["capture"]["created_at"].endswith("Z")

    rgb_ref = response_payload["camera"]["rgb_image"]["ref"]
    depth_ref = response_payload["camera"]["depth_image"]["ref"]
    assert rgb_ref["id"] == "artifact-rgb-001"
    assert rgb_ref["kind"] == "artifact_file"
    assert rgb_ref["content_type"] == "image/png"
    assert "path" not in rgb_ref
    assert rgb_ref["download_url"] == f"/captures/{response_payload['capture']['id']}/artifacts/rgb"
    assert depth_ref["id"] == "artifact-depth-001"
    assert depth_ref["kind"] == "artifact_file"
    assert depth_ref["content_type"] == "application/x-npy"
    assert "path" not in depth_ref
    assert depth_ref["download_url"] == f"/captures/{response_payload['capture']['id']}/artifacts/depth"
    assert ("get_camera_info", ("table_top",), {}) in fake_manager.calls


def test_capture_artifact_download_endpoint_returns_binary_files(case_output_dir: Path) -> None:
    client, _ = _create_test_client(case_output_dir)
    with client:
        capture_response = client.post("/cameras/table_top/capture")
        capture_payload = capture_response.json()
        capture_id = capture_payload["capture"]["id"]

        rgb_response = client.get(f"/captures/{capture_id}/artifacts/rgb")
        depth_response = client.get(f"/captures/{capture_id}/artifacts/depth")

    assert rgb_response.status_code == 200
    assert rgb_response.headers["content-type"] == "image/png"
    assert "attachment;" in rgb_response.headers["content-disposition"]
    assert rgb_response.content == _ONE_BY_ONE_PNG_BYTES

    assert depth_response.status_code == 200
    assert depth_response.headers["content-type"] == "application/x-npy"
    assert "attachment;" in depth_response.headers["content-disposition"]
    assert depth_response.content == b"fake-npy-data"


def test_capture_artifact_download_endpoint_returns_json_error_for_unknown_capture(case_output_dir: Path) -> None:
    client, _ = _create_test_client(case_output_dir)
    with client:
        response = client.get("/captures/capture-missing/artifacts/rgb")
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_message": "capture.id capture-missing does not exist",
    }


def test_stream_response_builder_returns_mjpeg_bytes_and_cleans_up(case_output_dir: Path) -> None:
    fake_manager = _create_fake_manager(case_output_dir)
    response = build_mjpeg_streaming_response(_NeverDisconnectedRequest(), fake_manager, "table_top")
    first_chunk = asyncio.run(_read_first_stream_chunk_and_close(response))
    assert response.media_type == "multipart/x-mixed-replace; boundary=frame"
    assert b"--frame" in first_chunk
    assert b"Content-Type: image/jpeg" in first_chunk
    assert b"\xff\xd8" in first_chunk
    assert any(call[0] == "start_camera_stream" for call in fake_manager.calls)
    assert any(call[0] == "stop_camera_stream" for call in fake_manager.calls)


def test_open_mjpeg_stream_unregisters_consumer_shared_memory(
    case_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_manager = _create_fake_manager(case_output_dir)
    unregister_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "api.mjpeg_stream.resource_tracker.unregister",
        lambda name, rtype: unregister_calls.append((name, rtype)),
    )

    opened_stream = _open_mjpeg_stream(fake_manager, "table_top")
    try:
        tracked_name = getattr(opened_stream.shm, "_name")
        assert unregister_calls == [(tracked_name, "shared_memory")]
    finally:
        opened_stream.shm.close()
        fake_manager.stop_camera_stream(opened_stream.stream_id)


def test_table_env_endpoints_delegate_to_sim_manager(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    with client:
        list_response = client.get("/table-envs")
        load_response = client.put("/table-env/current/default")
        objects_response = client.get("/table-env/current/objects")
        clear_response = client.delete("/table-env/current")
        objects_after_clear_response = client.get("/table-env/current/objects")
    assert list_response.status_code == 200
    assert list_response.json()["ok"] is True
    assert {item["id"] for item in list_response.json()["table_envs"]} == {
        "default",
        "multi_geometry",
        "ycb",
    }
    assert list_response.json()["table_env_count"] == 3

    assert load_response.status_code == 200
    assert load_response.json() == {
        "ok": True,
        "table_env": {"id": "default", "status": "loaded"},
        "objects": [{"id": "red_cube"}, {"id": "blue_cube"}],
        "object_count": 2,
    }

    assert objects_response.status_code == 200
    assert objects_response.json()["ok"] is True
    assert objects_response.json()["table_env"] == {"loaded": True, "id": "default"}

    assert clear_response.status_code == 200
    assert clear_response.json() == {
        "ok": True,
        "table_env": {"loaded": False, "id": None, "status": "cleared"},
        "previous_table_env_id": "default",
        "object_count": 0,
        "objects": [],
    }

    assert objects_after_clear_response.status_code == 200
    assert objects_after_clear_response.json() == {
        "ok": True,
        "table_env": {"loaded": False, "id": None},
        "object_count": 0,
        "objects": [],
    }
    assert ("list_table_env", (), {}) in fake_manager.calls
    assert ("load_table_env", ("default",), {}) in fake_manager.calls
    assert ("clear_table_env", (), {}) in fake_manager.calls
    assert ("get_table_env_objects_info", (), {}) in fake_manager.calls


def test_robot_endpoints_delegate_to_sim_manager(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    request_body = {
        "task": {
            "id": "task-001",
            "objects": [{"id": "red_cube"}],
            "code": "def run(robot, objects):\n    return None\n",
        }
    }
    with client:
        status_response = client.get("/robot/status")
        api_response = client.get("/robot/api")
        run_task_response = client.post("/robot/tasks", json=request_body)
    assert status_response.status_code == 200
    assert status_response.json() == {
        "ok": True,
        "robot": {"status": "idle", "current_task_id": None},
    }
    assert api_response.status_code == 200
    assert api_response.json() == {
        "ok": True,
        "api": "pick_and_place(...)",
    }
    assert run_task_response.status_code == 200
    assert run_task_response.json()["ok"] is True
    assert run_task_response.json()["task"]["id"] == "task-001"
    assert (
        "run_task",
        (),
        {
            "task_id": "task-001",
            "objects": [{"id": "red_cube"}],
            "code": "def run(robot, objects):\n    return None\n",
        },
    ) in fake_manager.calls


def test_worker_restart_endpoint_restarts_same_manager_instance(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    with client:
        response = client.post("/worker/restart")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["worker"] == {"status": "ready"}
    assert fake_manager.ensure_started_calls == 2
    assert fake_manager.close_calls >= 1


def test_sim_manager_errors_are_wrapped_as_ok_false_json(case_output_dir: Path) -> None:
    client, fake_manager = _create_test_client(case_output_dir)
    fake_manager.raise_on["list_camera"] = SimManagerError("camera registry unavailable")
    with client:
        response = client.get("/cameras")
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_message": "camera registry unavailable",
    }


def test_request_validation_errors_are_wrapped_as_ok_false_json(case_output_dir: Path) -> None:
    client, _ = _create_test_client(case_output_dir)
    with client:
        response = client.post("/robot/tasks", json={"task": {"id": "task-001"}})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "body.task.objects" in response.json()["error_message"]


def test_fastapi_real_integration_exercises_non_stream_interfaces(case_output_dir: Path) -> None:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    app = create_app(
        settings=ApiSettings(
            session_dir=str(case_output_dir / "session"),
            control_socket_path=str(case_output_dir / "control.sock"),
            python_bin=_worker_python(),
        ),
        start_manager_on_startup=True,
    )

    run_task_code = textwrap.dedent(
        """
        def run(robot, objects):
            return None
        """
    ).strip() + "\n"

    with TestClient(app) as client:
        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["ok"] is True
        assert health_response.json()["worker"] == {"status": "ready"}
        assert health_response.json()["table_env"] == {"loaded": False, "id": None}

        table_envs_response = client.get("/table-envs")
        assert table_envs_response.status_code == 200
        assert table_envs_response.json()["ok"] is True
        assert {item["id"] for item in table_envs_response.json()["table_envs"]} == {
            "default",
            "multi_geometry",
            "ycb",
        }

        cameras_response = client.get("/cameras")
        assert cameras_response.status_code == 200
        assert cameras_response.json()["ok"] is True
        assert [item["id"] for item in cameras_response.json()["cameras"]] == [
            "table_overview",
            "table_top",
        ]

        robot_status_response = client.get("/robot/status")
        assert robot_status_response.status_code == 200
        assert robot_status_response.json() == {
            "ok": True,
            "robot": {"status": "idle", "current_task_id": None},
        }

        robot_api_response = client.get("/robot/api")
        assert robot_api_response.status_code == 200
        assert robot_api_response.json()["ok"] is True
        assert "pick_and_place(" in robot_api_response.json()["api"]

        load_table_env_response = client.put("/table-env/current/default")
        assert load_table_env_response.status_code == 200
        assert load_table_env_response.json()["ok"] is True
        assert load_table_env_response.json()["table_env"] == {"id": "default", "status": "loaded"}

        table_env_objects_response = client.get("/table-env/current/objects")
        assert table_env_objects_response.status_code == 200
        assert table_env_objects_response.json()["ok"] is True
        assert table_env_objects_response.json()["table_env"] == {"loaded": True, "id": "default"}
        assert table_env_objects_response.json()["object_count"] == 2

        clear_table_env_response = client.delete("/table-env/current")
        assert clear_table_env_response.status_code == 200
        assert clear_table_env_response.json()["ok"] is True
        assert clear_table_env_response.json()["table_env"] == {"loaded": False, "id": None, "status": "cleared"}
        assert clear_table_env_response.json()["previous_table_env_id"] == "default"

        table_env_objects_after_clear_response = client.get("/table-env/current/objects")
        assert table_env_objects_after_clear_response.status_code == 200
        assert table_env_objects_after_clear_response.json()["ok"] is True
        assert table_env_objects_after_clear_response.json()["table_env"] == {"loaded": False, "id": None}
        assert table_env_objects_after_clear_response.json()["object_count"] == 0

        reload_table_env_response = client.put("/table-env/current/default")
        assert reload_table_env_response.status_code == 200
        assert reload_table_env_response.json()["ok"] is True
        assert reload_table_env_response.json()["table_env"] == {"id": "default", "status": "loaded"}

        capture_response = client.post("/cameras/table_top/capture")
        assert capture_response.status_code == 200
        assert capture_response.json()["ok"] is True
        assert capture_response.json()["camera"]["id"] == "table_top"

        run_task_response = client.post(
            "/robot/tasks",
            json={
                "task": {
                    "id": "task-api-real-integration",
                    "objects": table_env_objects_response.json()["objects"],
                    "code": run_task_code,
                }
            },
        )
        assert run_task_response.status_code == 200
        assert run_task_response.json()["ok"] is True
        assert run_task_response.json()["task"]["id"] == "task-api-real-integration"
        assert run_task_response.json()["task"]["status"] == "succeeded"

        restart_response = client.post("/worker/restart")
        assert restart_response.status_code == 200
        assert restart_response.json()["ok"] is True
        assert restart_response.json()["worker"] == {"status": "ready"}
        assert restart_response.json()["table_env"] == {"loaded": False, "id": None}


def test_fastapi_real_integration_streams_mjpeg_frames(case_output_dir: Path) -> None:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    with _run_api_server_subprocess(case_output_dir) as base_url:
        with httpx.Client(base_url=base_url, timeout=120.0) as client:
            load_table_env_response = client.put("/table-env/current/default")
            assert load_table_env_response.status_code == 200
            assert load_table_env_response.json()["ok"] is True

            with client.stream("GET", "/cameras/table_top/stream") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
                metrics = _measure_http_mjpeg_receive_fps(
                    response,
                    duration_sec=_MJPEG_SAMPLE_DURATION_SEC,
                    camera_id="table_top",
                    output_dir=case_output_dir / "mjpeg_metrics",
                )
                assert metrics["frame_count"] >= 2
                assert metrics["observed_fps"] > 0.0

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                health_response = client.get("/health")
                assert health_response.status_code == 200
                assert health_response.json()["ok"] is True
                if health_response.json()["streams"]["active_count"] == 0:
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("stream cleanup did not complete before timeout")

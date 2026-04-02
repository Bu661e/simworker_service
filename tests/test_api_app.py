from __future__ import annotations

import io
import json
import os
import textwrap
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from api.main import ApiSettings, create_app
from simworker import SimManagerError
from simworker.tests.test_simworker_integration import _ENABLE_REAL_TEST_ENV, _worker_python

_ONE_BY_ONE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeSimManager:
    def __init__(self, *, rgb_path: Path, depth_path: Path) -> None:
        self.rgb_path = rgb_path
        self.depth_path = depth_path
        self.ensure_started_calls = 0
        self.close_calls = 0
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raise_on: dict[str, Exception] = {}

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
            "table_env": {"loaded": False, "id": None},
            "objects": {"object_count": 0},
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
            "table_envs": [{"id": "default"}, {"id": "ycb"}],
            "table_env_count": 2,
        }

    def load_table_env(self, table_env_id: str) -> dict[str, Any]:
        self._maybe_raise("load_table_env")
        self.calls.append(("load_table_env", (table_env_id,), {}))
        return {
            "table_env": {"id": table_env_id, "status": "loaded"},
            "objects": [{"id": "red_cube"}, {"id": "blue_cube"}],
            "object_count": 2,
        }

    def get_table_env_objects_info(self) -> dict[str, Any]:
        self._maybe_raise("get_table_env_objects_info")
        self.calls.append(("get_table_env_objects_info", (), {}))
        return {
            "table_env": {"loaded": True, "id": "default"},
            "object_count": 2,
            "objects": [
                {
                    "id": "red_cube",
                    "pose": {
                        "position_xyz_m": [0.2, 0.0, 1.55],
                        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    "scale_xyz": [0.06, 0.06, 0.06],
                }
            ],
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


def _create_test_client(tmp_path: Path) -> tuple[TestClient, FakeSimManager]:
    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    rgb_path.write_bytes(_ONE_BY_ONE_PNG_BYTES)
    depth_path.write_bytes(b"fake-npy-data")

    fake_manager = FakeSimManager(rgb_path=rgb_path, depth_path=depth_path)
    app = create_app(
        settings=ApiSettings(control_socket_path="/tmp/test-control.sock"),
        sim_manager_factory=lambda _: fake_manager,
        start_manager_on_startup=True,
    )
    return TestClient(app), fake_manager


def test_health_endpoint_returns_ok_payload(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
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


def test_capture_endpoint_returns_zip_bundle(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
    with client:
        response = client.post("/cameras/table_top/capture")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["camera_info.json", "depth.npy", "rgb.png"]
    camera_info = json.loads(archive.read("camera_info.json").decode("utf-8"))
    assert camera_info["camera"]["id"] == "table_top"
    assert archive.read("rgb.png") == _ONE_BY_ONE_PNG_BYTES
    assert archive.read("depth.npy") == b"fake-npy-data"
    assert ("get_camera_info", ("table_top",), {}) in fake_manager.calls


def test_table_env_endpoints_delegate_to_sim_manager(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
    with client:
        list_response = client.get("/table-envs")
        load_response = client.put("/table-env/current/default")
        objects_response = client.get("/table-env/current/objects")
    assert list_response.status_code == 200
    assert list_response.json()["ok"] is True
    assert list_response.json()["table_env_count"] == 2

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
    assert ("list_table_env", (), {}) in fake_manager.calls
    assert ("load_table_env", ("default",), {}) in fake_manager.calls
    assert ("get_table_env_objects_info", (), {}) in fake_manager.calls


def test_robot_endpoints_delegate_to_sim_manager(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
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


def test_worker_restart_endpoint_restarts_same_manager_instance(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
    with client:
        response = client.post("/worker/restart")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["worker"] == {"status": "ready"}
    assert fake_manager.ensure_started_calls == 2
    assert fake_manager.close_calls >= 1


def test_sim_manager_errors_are_wrapped_as_ok_false_json(tmp_path: Path) -> None:
    client, fake_manager = _create_test_client(tmp_path)
    fake_manager.raise_on["list_camera"] = SimManagerError("camera registry unavailable")
    with client:
        response = client.get("/cameras")
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_message": "camera registry unavailable",
    }


def test_request_validation_errors_are_wrapped_as_ok_false_json(tmp_path: Path) -> None:
    client, _ = _create_test_client(tmp_path)
    with client:
        response = client.post("/robot/tasks", json={"task": {"id": "task-001"}})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "body.task.objects" in response.json()["error_message"]


def test_fastapi_real_integration_exercises_non_stream_interfaces(tmp_path: Path) -> None:
    if os.environ.get(_ENABLE_REAL_TEST_ENV) != "1":
        pytest.skip(
            "该测试会真实启动 Isaac Sim worker。"
            f"如需运行，请设置 {_ENABLE_REAL_TEST_ENV}=1。"
        )

    app = create_app(
        settings=ApiSettings(
            session_dir=str(tmp_path / "session"),
            control_socket_path=str(tmp_path / "control.sock"),
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
        assert {item["id"] for item in table_envs_response.json()["table_envs"]} == {"default", "ycb"}

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

        capture_response = client.post("/cameras/table_top/capture")
        assert capture_response.status_code == 200
        assert capture_response.headers["content-type"] == "application/zip"
        capture_archive = zipfile.ZipFile(io.BytesIO(capture_response.content))
        assert sorted(capture_archive.namelist()) == ["camera_info.json", "depth.npy", "rgb.png"]
        capture_camera_info = json.loads(capture_archive.read("camera_info.json").decode("utf-8"))
        assert capture_camera_info["camera"]["id"] == "table_top"

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

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from api.mjpeg_stream import build_mjpeg_streaming_response
from simworker import SimManager, SimManagerError

logger = logging.getLogger("api")


@dataclass(slots=True)
class ApiSettings:
    control_socket_path: str = "/tmp/simworker/control.sock"
    python_bin: str = "/root/isaacsim/python.sh"
    session_dir: str | None = None
    worker_module: str = "simworker.entrypoint"
    cwd: str | None = None
    startup_timeout_sec: float = 240.0
    request_timeout_sec: float = 60.0
    shutdown_timeout_sec: float = 60.0

    @classmethod
    def from_env(cls) -> "ApiSettings":
        return cls(
            control_socket_path=os.environ.get("SIMWORKER_CONTROL_SOCKET_PATH", "/tmp/simworker/control.sock"),
            python_bin=os.environ.get("SIMWORKER_PYTHON_BIN", "/root/isaacsim/python.sh"),
            session_dir=os.environ.get("SIMWORKER_SESSION_DIR") or None,
            worker_module=os.environ.get("SIMWORKER_WORKER_MODULE", "simworker.entrypoint"),
            cwd=os.environ.get("SIMWORKER_CWD") or None,
            startup_timeout_sec=_float_env("SIMWORKER_STARTUP_TIMEOUT_SEC", 240.0),
            request_timeout_sec=_float_env("SIMWORKER_REQUEST_TIMEOUT_SEC", 60.0),
            shutdown_timeout_sec=_float_env("SIMWORKER_SHUTDOWN_TIMEOUT_SEC", 60.0),
        )


class TaskSpec(BaseModel):
    id: str
    objects: list[dict[str, Any]]
    code: str


class RunTaskRequest(BaseModel):
    task: TaskSpec


class SimManagerLike(Protocol):
    def ensure_started(self) -> Any: ...
    def close(self) -> None: ...
    def hello(self) -> dict[str, Any]: ...
    def list_camera(self) -> dict[str, Any]: ...
    def get_camera_info(self, camera_id: str) -> dict[str, Any]: ...
    def start_camera_stream(self, camera_id: str, *, buffer_mode: str = "latest_frame") -> dict[str, Any]: ...
    def stop_camera_stream(self, stream_id: str) -> dict[str, Any]: ...
    def list_table_env(self) -> dict[str, Any]: ...
    def load_table_env(self, table_env_id: str) -> dict[str, Any]: ...
    def get_table_env_objects_info(self) -> dict[str, Any]: ...
    def get_robot_status(self) -> dict[str, Any]: ...
    def list_api(self) -> str: ...
    def run_task(self, *, task_id: str, objects: list[dict[str, Any]], code: str) -> dict[str, Any]: ...


SimManagerFactory = Callable[[ApiSettings], SimManagerLike]


def create_app(
    *,
    settings: ApiSettings | None = None,
    sim_manager_factory: SimManagerFactory | None = None,
    start_manager_on_startup: bool = True,
) -> FastAPI:
    resolved_settings = settings or ApiSettings.from_env()
    resolved_factory = sim_manager_factory or _build_sim_manager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sim_manager = resolved_factory(resolved_settings)
        app.state.settings = resolved_settings
        app.state.sim_manager = sim_manager
        if start_manager_on_startup:
            sim_manager.ensure_started()
        try:
            yield
        finally:
            sim_manager.close()

    app = FastAPI(title="IsaacSim Service V0", lifespan=lifespan)
    app.add_exception_handler(SimManagerError, _handle_sim_manager_error)
    app.add_exception_handler(ValueError, _handle_value_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

    @app.get("/health")
    def health(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.hello())

    @app.get("/cameras")
    def list_cameras(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.list_camera())

    @app.post("/cameras/{camera_id}/capture")
    def capture_camera(camera_id: str, sim_manager: SimManager = Depends(_get_sim_manager)) -> Response:
        camera_info_payload = sim_manager.get_camera_info(camera_id)
        archive_bytes = _build_camera_capture_zip(camera_info_payload)
        archive_name = _build_capture_archive_name(camera_info_payload)
        return Response(
            content=archive_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{archive_name}"',
            },
        )

    @app.get("/cameras/{camera_id}/stream")
    def stream_camera(
        camera_id: str,
        request: Request,
        sim_manager: SimManagerLike = Depends(_get_sim_manager),
    ) -> StreamingResponse:
        return build_mjpeg_streaming_response(request, sim_manager, camera_id)

    @app.get("/table-envs")
    def list_table_envs(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.list_table_env())

    @app.put("/table-env/current/{table_env_id}")
    def load_table_env(table_env_id: str, sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.load_table_env(table_env_id))

    @app.get("/table-env/current/objects")
    def get_table_env_objects(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.get_table_env_objects_info())

    @app.get("/robot/status")
    def get_robot_status(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return _ok_payload(sim_manager.get_robot_status())

    @app.get("/robot/api")
    def get_robot_api(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        return {
            "ok": True,
            "api": sim_manager.list_api(),
        }

    @app.post("/robot/tasks")
    def run_robot_task(
        request_body: RunTaskRequest,
        sim_manager: SimManager = Depends(_get_sim_manager),
    ) -> dict[str, Any]:
        return _ok_payload(
            sim_manager.run_task(
                task_id=request_body.task.id,
                objects=request_body.task.objects,
                code=request_body.task.code,
            )
        )

    @app.post("/worker/restart")
    def restart_worker(sim_manager: SimManager = Depends(_get_sim_manager)) -> dict[str, Any]:
        sim_manager.close()
        sim_manager.ensure_started()
        return _ok_payload(sim_manager.hello())

    return app


def _build_sim_manager(settings: ApiSettings) -> SimManager:
    return SimManager(
        session_dir=settings.session_dir,
        control_socket_path=settings.control_socket_path,
        python_bin=settings.python_bin,
        worker_module=settings.worker_module,
        cwd=settings.cwd,
        startup_timeout_sec=settings.startup_timeout_sec,
        request_timeout_sec=settings.request_timeout_sec,
        shutdown_timeout_sec=settings.shutdown_timeout_sec,
    )


def _get_sim_manager(request: Request) -> SimManagerLike:
    return request.app.state.sim_manager


def _ok_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        **payload,
    }


def _error_payload(error_message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_message": error_message,
    }


def _json_error_response(error_message: str) -> JSONResponse:
    return JSONResponse(status_code=200, content=_error_payload(error_message))


async def _handle_sim_manager_error(_: Request, exc: SimManagerError) -> JSONResponse:
    return _json_error_response(str(exc))


async def _handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return _json_error_response(str(exc))


async def _handle_request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    error_items: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = item.get("msg", "invalid request")
        error_items.append(f"{location}: {message}" if location else message)
    return _json_error_response("; ".join(error_items) or "invalid request")


async def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API exception")
    return _json_error_response(f"unexpected error: {exc}")


def _build_camera_capture_zip(camera_info_payload: dict[str, Any]) -> bytes:
    rgb_path, depth_path = _resolve_camera_artifact_paths(camera_info_payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("camera_info.json", json.dumps(camera_info_payload, ensure_ascii=False, indent=2))
        archive.writestr("rgb.png", rgb_path.read_bytes())
        archive.writestr("depth.npy", depth_path.read_bytes())
    return buffer.getvalue()


def _resolve_camera_artifact_paths(camera_info_payload: dict[str, Any]) -> tuple[Path, Path]:
    camera_payload = camera_info_payload.get("camera")
    if not isinstance(camera_payload, dict):
        raise ValueError("camera capture payload is missing camera object")

    rgb_path = _extract_artifact_path(camera_payload, "rgb_image")
    depth_path = _extract_artifact_path(camera_payload, "depth_image")
    return rgb_path, depth_path


def _extract_artifact_path(camera_payload: dict[str, Any], image_field_name: str) -> Path:
    image_payload = camera_payload.get(image_field_name)
    if not isinstance(image_payload, dict):
        raise ValueError(f"camera payload is missing {image_field_name}")

    ref_payload = image_payload.get("ref")
    if not isinstance(ref_payload, dict):
        raise ValueError(f"camera payload is missing {image_field_name}.ref")

    artifact_path = ref_payload.get("path")
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError(f"camera payload is missing {image_field_name}.ref.path")

    resolved_path = Path(artifact_path)
    if not resolved_path.exists():
        raise ValueError(f"artifact file does not exist: {resolved_path}")
    return resolved_path


def _build_capture_archive_name(camera_info_payload: dict[str, Any]) -> str:
    camera_payload = camera_info_payload.get("camera")
    camera_id = "camera"
    if isinstance(camera_payload, dict):
        payload_camera_id = camera_payload.get("id")
        if isinstance(payload_camera_id, str) and payload_camera_id:
            camera_id = payload_camera_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{camera_id}_{timestamp}.zip"


def _float_env(env_name: str, default_value: float) -> float:
    raw_value = os.environ.get(env_name)
    if raw_value is None or not raw_value.strip():
        return default_value
    return float(raw_value)


app = create_app()

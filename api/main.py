from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Protocol
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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


@dataclass(frozen=True, slots=True)
class CaptureArtifactRecord:
    path: Path
    media_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    capture_id: str
    camera_id: str
    created_at: str
    artifacts: dict[str, CaptureArtifactRecord]


class CaptureArtifactStore:
    def __init__(self) -> None:
        self._captures: dict[str, CaptureRecord] = {}
        self._lock = Lock()

    def save_capture(self, capture_record: CaptureRecord) -> None:
        with self._lock:
            self._captures[capture_record.capture_id] = capture_record

    def get_artifact(self, capture_id: str, artifact_kind: str) -> CaptureArtifactRecord:
        with self._lock:
            capture_record = self._captures.get(capture_id)

        if capture_record is None:
            raise ValueError(f"capture.id {capture_id} does not exist")

        artifact_record = capture_record.artifacts.get(artifact_kind)
        if artifact_record is None:
            raise ValueError(f"capture.id {capture_id} does not include artifact {artifact_kind}")

        if not artifact_record.path.exists():
            raise ValueError(f"artifact file does not exist: {artifact_record.path}")

        return artifact_record


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
        app.state.capture_artifact_store = CaptureArtifactStore()
        if start_manager_on_startup:
            sim_manager.ensure_started()
        try:
            yield
        finally:
            sim_manager.close()

    app = FastAPI(title="IsaacSim Service", lifespan=lifespan)
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
    def capture_camera(
        camera_id: str,
        request: Request,
        sim_manager: SimManager = Depends(_get_sim_manager),
        artifact_store: CaptureArtifactStore = Depends(_get_capture_artifact_store),
    ) -> dict[str, Any]:
        camera_info_payload = sim_manager.get_camera_info(camera_id)
        return _ok_payload(
            _build_camera_capture_response(
                camera_info_payload,
                request=request,
                artifact_store=artifact_store,
            )
        )

    @app.get("/captures/{capture_id}/artifacts/{artifact_kind}", name="download_capture_artifact")
    def download_capture_artifact(
        capture_id: str,
        artifact_kind: str,
        artifact_store: CaptureArtifactStore = Depends(_get_capture_artifact_store),
    ) -> FileResponse:
        artifact_record = artifact_store.get_artifact(capture_id, artifact_kind)
        return FileResponse(
            path=artifact_record.path,
            media_type=artifact_record.media_type,
            filename=artifact_record.filename,
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


def _get_capture_artifact_store(request: Request) -> CaptureArtifactStore:
    return request.app.state.capture_artifact_store


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


def _build_camera_capture_response(
    camera_info_payload: dict[str, Any],
    *,
    request: Request,
    artifact_store: CaptureArtifactStore,
) -> dict[str, Any]:
    camera_payload = camera_info_payload.get("camera")
    if not isinstance(camera_payload, dict):
        raise ValueError("camera capture payload is missing camera object")

    capture_id = f"capture-{uuid4().hex}"
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rgb_artifact = _build_capture_artifact_record(camera_payload, "rgb_image")
    depth_artifact = _build_capture_artifact_record(camera_payload, "depth_image")
    artifact_store.save_capture(
        CaptureRecord(
            capture_id=capture_id,
            camera_id=_extract_camera_id(camera_payload),
            created_at=created_at,
            artifacts={
                "rgb": rgb_artifact,
                "depth": depth_artifact,
            },
        )
    )

    response_payload = deepcopy(camera_info_payload)
    response_payload["capture"] = {
        "id": capture_id,
        "created_at": created_at,
    }

    response_camera_payload = response_payload.get("camera")
    if not isinstance(response_camera_payload, dict):
        raise ValueError("camera capture payload is missing camera object")

    _replace_artifact_path_with_download_url(
        response_camera_payload,
        "rgb_image",
        str(
            request.url_for(
                "download_capture_artifact",
                capture_id=capture_id,
                artifact_kind="rgb",
            )
        ),
    )
    _replace_artifact_path_with_download_url(
        response_camera_payload,
        "depth_image",
        str(
            request.url_for(
                "download_capture_artifact",
                capture_id=capture_id,
                artifact_kind="depth",
            )
        ),
    )
    return response_payload


def _build_capture_artifact_record(camera_payload: dict[str, Any], image_field_name: str) -> CaptureArtifactRecord:
    ref_payload = _extract_artifact_ref(camera_payload, image_field_name)
    content_type = ref_payload.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise ValueError(f"camera payload is missing {image_field_name}.ref.content_type")

    artifact_path = _extract_artifact_path(camera_payload, image_field_name)
    return CaptureArtifactRecord(
        path=artifact_path,
        media_type=content_type,
        filename=artifact_path.name,
    )


def _extract_artifact_ref(camera_payload: dict[str, Any], image_field_name: str) -> dict[str, Any]:
    image_payload = camera_payload.get(image_field_name)
    if not isinstance(image_payload, dict):
        raise ValueError(f"camera payload is missing {image_field_name}")

    ref_payload = image_payload.get("ref")
    if not isinstance(ref_payload, dict):
        raise ValueError(f"camera payload is missing {image_field_name}.ref")
    return ref_payload


def _extract_artifact_path(camera_payload: dict[str, Any], image_field_name: str) -> Path:
    ref_payload = _extract_artifact_ref(camera_payload, image_field_name)
    artifact_path = ref_payload.get("path")
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError(f"camera payload is missing {image_field_name}.ref.path")

    resolved_path = Path(artifact_path)
    if not resolved_path.exists():
        raise ValueError(f"artifact file does not exist: {resolved_path}")
    return resolved_path


def _replace_artifact_path_with_download_url(
    camera_payload: dict[str, Any],
    image_field_name: str,
    download_url: str,
) -> None:
    ref_payload = _extract_artifact_ref(camera_payload, image_field_name)
    ref_payload.pop("path", None)
    ref_payload["download_url"] = download_url


def _extract_camera_id(camera_payload: dict[str, Any]) -> str:
    camera_id = camera_payload.get("id")
    if not isinstance(camera_id, str) or not camera_id:
        raise ValueError("camera payload is missing camera.id")
    return camera_id


def _float_env(env_name: str, default_value: float) -> float:
    raw_value = os.environ.get(env_name)
    if raw_value is None or not raw_value.strip():
        return default_value
    return float(raw_value)


app = create_app()

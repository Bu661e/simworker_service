from __future__ import annotations

from simworker.protocol import ControlRequest, ControlResponse
from simworker.robots import get_robot_api_text
from simworker.runtime import WorkerRuntime
from simworker.table_environments import list_table_environment_ids

# 旧对象导入接口已经被 table_env 预设加载替代，继续调用时直接返回明确迁移提示。
_REMOVED_COMMAND_MESSAGES = {
    "add_objects": "command_type add_objects has been removed; use load_table_env instead",
    "add_scene_objects": "command_type add_scene_objects has been removed; use load_table_env instead",
    "get_scene_objects_info": "command_type get_scene_objects_info has been removed; use get_table_env_objects_info instead",
}


class CommandDispatcher:
    def __init__(self, runtime: WorkerRuntime) -> None:
        self._runtime = runtime
        # 先把命令分发点集中起来，后续补业务实现时不需要改主循环。
        self._handlers = {
            "hello": self._handle_hello,
            "list_table_env": self._handle_list_table_env,
            "list_api": self._handle_list_api,
            "list_camera": self._handle_list_camera,
            "load_table_env": self._handle_load_table_env,
            "get_table_env_objects_info": self._handle_get_table_env_objects_info,
            "get_robot_status": self._handle_get_robot_status,
            "get_camera_info": self._handle_get_camera_info,
            "start_camera_stream": self._handle_start_camera_stream,
            "stop_camera_stream": self._handle_stop_camera_stream,
            "run_task": self._handle_run_task,
            "shutdown": self._handle_shutdown,
        }

    def handle(self, request: ControlRequest) -> ControlResponse:
        self._runtime.logger.info(
            "Handling command request_id=%s command_type=%s",
            request.request_id,
            request.command_type,
        )
        handler = self._handlers.get(request.command_type)
        if handler is None:
            removed_message = _REMOVED_COMMAND_MESSAGES.get(request.command_type)
            if removed_message is not None:
                return ControlResponse.error(
                    request_id=request.request_id,
                    error_message=removed_message,
                )
            return ControlResponse.error(
                request_id=request.request_id,
                error_message=f"unknown command_type: {request.command_type}",
            )

        response = handler(request)
        self._runtime.logger.info(
            "Completed command request_id=%s command_type=%s ok=%s",
            request.request_id,
            request.command_type,
            response.ok,
        )
        return response

    def _handle_hello(self, request: ControlRequest) -> ControlResponse:
        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.build_hello_payload(),
        )

    def _handle_list_table_env(self, request: ControlRequest) -> ControlResponse:
        table_env_ids = list_table_environment_ids()
        return ControlResponse.success(
            request_id=request.request_id,
            payload={
                "table_envs": [{"id": table_env_id} for table_env_id in table_env_ids],
                "table_env_count": len(table_env_ids),
            },
        )

    def _handle_list_api(self, request: ControlRequest) -> ControlResponse:
        return ControlResponse.success(
            request_id=request.request_id,
            payload={
                "api": get_robot_api_text(),
            },
        )

    def _handle_list_camera(self, request: ControlRequest) -> ControlResponse:
        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.build_list_camera_payload(),
        )

    def _handle_load_table_env(self, request: ControlRequest) -> ControlResponse:
        table_env_id = _expect_non_empty_string(request.payload.get("table_env_id"), "payload.table_env_id")
        loaded_objects = self._runtime.load_table_env(table_env_id)
        return ControlResponse.success(
            request_id=request.request_id,
            payload={
                "table_env": {
                    "id": self._runtime.table_env_id,
                    "status": "loaded",
                },
                "objects": [{"id": self._runtime.get_handle_object_id(scene_object)} for scene_object in loaded_objects],
                "object_count": len(self._runtime.objects),
            },
        )

    def _handle_get_table_env_objects_info(self, request: ControlRequest) -> ControlResponse:
        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.build_table_env_objects_payload(),
        )

    def _handle_get_robot_status(self, request: ControlRequest) -> ControlResponse:
        return ControlResponse.success(
            request_id=request.request_id,
            payload={"robot": self._runtime.build_robot_payload()},
        )

    def _handle_get_camera_info(self, request: ControlRequest) -> ControlResponse:
        camera_payload = request.payload.get("camera")
        if not isinstance(camera_payload, dict):
            raise ValueError("payload.camera must be an object")

        camera_id = _expect_non_empty_string(camera_payload.get("id"), "payload.camera.id")
        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.build_camera_info_payload(camera_id),
        )

    def _handle_start_camera_stream(self, request: ControlRequest) -> ControlResponse:
        camera_payload = request.payload.get("camera")
        if not isinstance(camera_payload, dict):
            raise ValueError("payload.camera must be an object")

        stream_payload = request.payload.get("stream", {})
        if not isinstance(stream_payload, dict):
            raise ValueError("payload.stream must be an object")

        camera_id = _expect_non_empty_string(camera_payload.get("id"), "payload.camera.id")
        buffer_mode = stream_payload.get("buffer_mode", "latest_frame")
        if not isinstance(buffer_mode, str) or not buffer_mode:
            raise ValueError("payload.stream.buffer_mode must be a non-empty string")

        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.start_camera_stream(camera_id, buffer_mode=buffer_mode),
        )

    def _handle_stop_camera_stream(self, request: ControlRequest) -> ControlResponse:
        stream_payload = request.payload.get("stream")
        if not isinstance(stream_payload, dict):
            raise ValueError("payload.stream must be an object")

        stream_id = _expect_non_empty_string(stream_payload.get("id"), "payload.stream.id")
        return ControlResponse.success(
            request_id=request.request_id,
            payload=self._runtime.stop_camera_stream(stream_id),
        )

    def _handle_run_task(self, request: ControlRequest) -> ControlResponse:
        task_payload = request.payload.get("task")
        if not isinstance(task_payload, dict):
            return ControlResponse.error(
                request_id=request.request_id,
                error_message="payload.task must be an object",
                payload={"task": {"id": None, "status": "failed", "result": None}},
            )

        task_id = task_payload.get("id") if isinstance(task_payload.get("id"), str) and task_payload.get("id") else None
        try:
            validated_task_id = _expect_non_empty_string(task_payload.get("id"), "payload.task.id")
            task_code = _expect_non_empty_string(task_payload.get("code"), "payload.task.code")
            task_objects = _expect_task_objects(task_payload.get("objects"))
            payload = self._runtime.run_task(
                task_id=validated_task_id,
                task_code=task_code,
                task_objects=task_objects,
            )
            return ControlResponse.success(
                request_id=request.request_id,
                payload=payload,
            )
        except ValueError as exc:
            return ControlResponse.error(
                request_id=request.request_id,
                error_message=str(exc),
                payload={"task": {"id": task_id, "status": "failed", "result": None}},
            )
        except Exception as exc:
            self._runtime.logger.exception("run_task failed for task_id=%s", task_id)
            return ControlResponse.error(
                request_id=request.request_id,
                error_message=f"Task failed: {_format_exception_message(exc)}",
                payload={"task": {"id": task_id, "status": "failed", "result": None}},
            )

    def _handle_shutdown(self, request: ControlRequest) -> ControlResponse:
        self._runtime.request_shutdown()
        return ControlResponse.success(
            request_id=request.request_id,
            payload={"worker": {"status": self._runtime.worker_status}},
        )


def _expect_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _expect_task_objects(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("payload.task.objects must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"payload.task.objects[{index}] must be an object")
    return value


def _format_exception_message(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__

from __future__ import annotations

from simworker.protocol import ControlRequest, ControlResponse
from simworker.runtime import WorkerRuntime
from simworker.table_environments import list_table_environment_ids

_RECOGNIZED_UNIMPLEMENTED_COMMANDS = {
    "start_camera_stream",
    "stop_camera_stream",
    "run_task",
}

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
            "list_camera": self._handle_list_camera,
            "load_table_env": self._handle_load_table_env,
            "get_table_env_objects_info": self._handle_get_table_env_objects_info,
            "get_robot_status": self._handle_get_robot_status,
            "get_camera_info": self._handle_get_camera_info,
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
            if request.command_type in _RECOGNIZED_UNIMPLEMENTED_COMMANDS:
                return ControlResponse.error(
                    request_id=request.request_id,
                    error_message=f"command_type {request.command_type} is not implemented yet",
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

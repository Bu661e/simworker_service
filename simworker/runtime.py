from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from simworker.base_environments import BaseEnvironmentHandles, create_default_tabletop_base_environment
from simworker.camera_streams import CameraStreamRuntimeState, create_camera_stream_runtime_state
from simworker.robots import FrankaRobotAPI
from simworker.table_environments import ensure_supported_table_environment_id, load_table_environment

_CAMERA_CAPTURE_RENDER_STEPS = 2
_TABLE_ENV_CLEAR_RENDER_STEPS = 2
_STREAM_LOOP_RENDER_PERIOD_SEC = 1.0 / 30.0
_STREAM_LOOP_IDLE_WAIT_SEC = 0.50


@dataclass(slots=True)
class WorkerRuntime:
    session_dir: Path
    run_dir: Path
    artifacts_dir: Path
    logger: logging.Logger
    simulation_app: object | None = None
    base_environment: BaseEnvironmentHandles | None = None
    robot_api: FrankaRobotAPI | None = None
    worker_status: str = "starting"
    robot_status: str = "idle"
    current_task_id: str | None = None
    table_env_id: str | None = None
    # 这里直接保存 table_env 创建出的 handle；位姿和缩放统一在查询时直接从 handle 现查。
    objects: list[object] = field(default_factory=list)
    artifact_counters: dict[str, int] = field(default_factory=dict)
    stream_counters: dict[str, int] = field(default_factory=dict)
    streams_by_id: dict[str, CameraStreamRuntimeState] = field(default_factory=dict)
    stream_ids_by_camera: dict[str, str] = field(default_factory=dict)
    simulation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # stream 刷新改为主线程 idle tick 模式，避免在后台线程里直接调用 Isaac Sim API。
    last_stream_publish_monotonic: float = 0.0
    shutdown_requested: bool = False

    @classmethod
    def bootstrap(cls, session_dir: Path) -> "WorkerRuntime":
        session_dir = session_dir.resolve()
        session_dir.mkdir(parents=True, exist_ok=True)

        # 每次 worker 运行创建独立 run_dir，避免日志和 artifacts 混在一起。
        run_dir = _allocate_run_dir(session_dir)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        logger = _configure_logger(run_dir / "worker.log")

        runtime = cls(
            session_dir=session_dir,
            run_dir=run_dir,
            artifacts_dir=artifacts_dir,
            logger=logger,
        )
        runtime.initialize()
        return runtime

    def initialize(self) -> None:
        self.logger.info("Initializing simworker runtime in %s", self.run_dir)
        try:
            self.simulation_app = self._bootstrap_simulation_app()
            self.base_environment = create_default_tabletop_base_environment(self.logger)
            if self.robot is None:
                raise RuntimeError("base environment robot is not initialized")
            self.robot_api = FrankaRobotAPI(runtime=self, robot_handle=self.robot, logger=self.logger)
            self.worker_status = "ready"
            self.logger.info(
                "Simworker runtime is ready (object_count=%s)",
                len(self.objects),
            )
        except Exception:
            self.worker_status = "error"
            self.logger.exception("Failed to initialize simworker runtime")
            raise

    def _bootstrap_simulation_app(self) -> object:
        try:
            from isaacsim import SimulationApp
        except ImportError as exc:
            raise RuntimeError("isaacsim is required") from exc

        self.logger.info("Bootstrapping Isaac Sim SimulationApp")
        return SimulationApp({"headless": True})


    def build_hello_payload(self) -> dict[str, Any]:
        return {
            "worker": {"status": self.worker_status},
            "table_env": {
                "loaded": self.table_env_id is not None,
                "id": self.table_env_id,
            },
            "objects": {
                "object_count": len(self.objects),
            },
            "robot": self.build_robot_payload(),
            "streams": {
                "active_count": self.active_stream_count,
            },
        }

    def build_robot_payload(self) -> dict[str, Any]:
        return {
            "status": self.robot_status,
            "current_task_id": self.current_task_id,
        }

    def build_table_env_objects_payload(self) -> dict[str, Any]:
        with self.simulation_lock:
            return {
                "table_env": {
                    "loaded": self.table_env_id is not None,
                    "id": self.table_env_id,
                },
                "object_count": len(self.objects),
                "objects": [self._build_object_transform_payload(scene_object) for scene_object in self.objects],
            }

    def build_list_camera_payload(self) -> dict[str, Any]:
        # 控制面只需要先知道有哪些 camera.id 可用，具体详情再走 get_camera_info。
        camera_ids = sorted(self.cameras)
        return {
            "cameras": [{"id": camera_id} for camera_id in camera_ids],
            "camera_count": len(camera_ids),
        }

    @property
    def active_stream_count(self) -> int:
        with self.simulation_lock:
            return sum(1 for stream_state in self.streams_by_id.values() if stream_state.status == "running")

    @property
    def world(self) -> object | None:
        if self.base_environment is None:
            return None
        return self.base_environment.world

    @property
    def cameras(self) -> dict[str, object]:
        if self.base_environment is None:
            return {}
        return self.base_environment.cameras

    @property
    def camera_configs(self) -> dict[str, object]:
        if self.base_environment is None:
            return {}
        return self.base_environment.camera_configs

    @property
    def robot(self) -> object | None:
        if self.base_environment is None:
            return None
        return self.base_environment.robot

    @property
    def table(self) -> object | None:
        if self.base_environment is None:
            return None
        return self.base_environment.table

    def start_camera_stream(self, camera_id: str, *, buffer_mode: str) -> dict[str, Any]:
        if buffer_mode != "latest_frame":
            raise ValueError(f"unsupported stream.buffer_mode: {buffer_mode}; supported values: latest_frame")

        camera = self.cameras.get(camera_id)
        if camera is None:
            raise ValueError(f"camera.id {camera_id} does not exist")

        with self.simulation_lock:
            existing_stream_id = self.stream_ids_by_camera.get(camera_id)
            if existing_stream_id is not None:
                existing_stream = self.streams_by_id.get(existing_stream_id)
                if existing_stream is not None and existing_stream.status == "running":
                    return {
                        "camera": {"id": camera_id},
                        "stream": existing_stream.build_control_payload(),
                    }
                if existing_stream is not None:
                    self._remove_stream_state(existing_stream)

            width, height = camera.get_resolution()
            stream_id, ref_id = self._allocate_stream_ids(camera_id)
            stream_state = create_camera_stream_runtime_state(
                stream_id=stream_id,
                ref_id=ref_id,
                camera_id=camera_id,
                resolution=(int(width), int(height)),
            )
            try:
                # 首帧返回前先推进少量渲染帧，避免刚启动 stream 时读到空帧或旧帧。
                self._step_render_frames(_CAMERA_CAPTURE_RENDER_STEPS)
                rgba_image = self._capture_camera_rgba(camera)
                stream_state.write_rgb_frame(rgba_image)
            except Exception:
                stream_state.close()
                raise

            self.streams_by_id[stream_state.stream_id] = stream_state
            self.stream_ids_by_camera[camera_id] = stream_state.stream_id
            self.last_stream_publish_monotonic = time.monotonic()
            return {
                "camera": {"id": camera_id},
                "stream": stream_state.build_control_payload(),
            }

    def stop_camera_stream(self, stream_id: str) -> dict[str, Any]:
        with self.simulation_lock:
            stream_state = self.streams_by_id.get(stream_id)
            if stream_state is None:
                raise ValueError(f"stream.id {stream_id} does not exist")

            stream_state.mark_stopped()
            self._remove_stream_state(stream_state)
            return {
                "stream": {
                    "id": stream_id,
                    "status": "stopped",
                }
            }

    def load_table_env(self, table_env_id: str) -> list[object]:
        with self.simulation_lock:
            # table_env 是 worker 自己负责的硬编码桌面配置，不再从控制面接收复杂对象 JSON。
            if self.table_env_id is not None:
                if self.table_env_id == table_env_id:
                    self.logger.info("Table environment already loaded: %s", table_env_id)
                    return list(self.objects)
                # 先校验请求 id 是否真的受支持，这样 unknown 不会被误报成“切换环境失败”。
                ensure_supported_table_environment_id(table_env_id)
                raise ValueError(
                    f"table_env_id {table_env_id} does not match current loaded table_env_id {self.table_env_id}"
                )

            loaded_objects = load_table_environment(self, table_env_id)
            self._ensure_unique_handle_object_ids(loaded_objects)
            self.objects = list(loaded_objects)
            self.table_env_id = table_env_id
            self.logger.info("Loaded table environment: %s (object_count=%s)", table_env_id, len(loaded_objects))
            return loaded_objects

    def clear_table_env(self) -> dict[str, Any]:
        with self.simulation_lock:
            if self.table_env_id is None:
                self.logger.info("clear_table_env called with no loaded table environment")
                return {
                    "table_env": {
                        "loaded": False,
                        "id": None,
                        "status": "empty",
                    },
                    "previous_table_env_id": None,
                    "object_count": 0,
                    "objects": [],
                }

            loaded_table_env_id = self.table_env_id
            handles_to_clear = list(self.objects)
            failed_handles: list[object] = []
            for handle in handles_to_clear:
                object_id, prim_path = self._describe_table_object_handle(handle)
                try:
                    self._remove_table_object_handle(handle)
                except Exception:
                    failed_handles.append(handle)
                    self.logger.exception(
                        "Failed to remove table environment object: table_env_id=%s object_id=%s prim_path=%s",
                        loaded_table_env_id,
                        object_id,
                        prim_path,
                    )

            if handles_to_clear:
                self._step_render_frames(_TABLE_ENV_CLEAR_RENDER_STEPS)

            self.objects = failed_handles
            if failed_handles:
                remaining_object_ids = [self._describe_table_object_handle(handle)[0] for handle in failed_handles]
                raise RuntimeError(
                    "failed to fully clear table environment "
                    f"{loaded_table_env_id}; remaining objects: {remaining_object_ids}"
                )

            self.table_env_id = None
            self.logger.info(
                "Cleared table environment: %s (removed_object_count=%s)",
                loaded_table_env_id,
                len(handles_to_clear),
            )
            return {
                "table_env": {
                    "loaded": False,
                    "id": None,
                    "status": "cleared",
                },
                "previous_table_env_id": loaded_table_env_id,
                "object_count": 0,
                "objects": [],
            }

    def build_camera_info_payload(self, camera_id: str) -> dict[str, Any]:
        import numpy as np

        with self.simulation_lock:
            camera = self.cameras.get(camera_id)
            if camera is None:
                raise ValueError(f"camera.id {camera_id} does not exist")

            camera_config = self.camera_configs.get(camera_id)
            if not isinstance(camera_config, dict):
                raise RuntimeError(f"camera.id {camera_id} metadata is missing")

            if self.world is None:
                raise RuntimeError("base environment world is not initialized")

            # 拍快照前先推进少量渲染帧，保证 RGB / depth 与当前 stage 状态一致。
            self._step_render_frames(_CAMERA_CAPTURE_RENDER_STEPS)
            rgba_image = self._capture_camera_rgba(camera)
            current_frame = camera.get_current_frame(clone=True)
            depth_image = current_frame.get("distance_to_image_plane")
            if depth_image is None:
                raise RuntimeError(
                    f"camera {camera_id} depth annotator did not return distance_to_image_plane data"
                )
            depth_image = np.asarray(depth_image, dtype=np.float32)

            snapshot_tag = datetime.now().strftime("%H-%M-%S-%f")
            rgb_ref = self._write_rgb_artifact(camera_id, snapshot_tag, rgba_image)
            depth_ref = self._write_depth_artifact(camera_id, snapshot_tag, depth_image)

            intrinsics_matrix = np.asarray(camera.get_intrinsics_matrix(), dtype=float)
            camera_position, camera_orientation_wxyz = camera.get_world_pose(camera_axes="world")
            width, height = camera.get_resolution()
            return {
                "camera": {
                    "id": camera_id,
                    "status": "ready",
                    "prim_path": camera_config["prim_path"],
                    "mount_mode": camera_config["mount_mode"],
                    "resolution": [int(width), int(height)],
                    "intrinsics": {
                        "fx": float(intrinsics_matrix[0, 0]),
                        "fy": float(intrinsics_matrix[1, 1]),
                        "cx": float(intrinsics_matrix[0, 2]),
                        "cy": float(intrinsics_matrix[1, 2]),
                        "width": int(width),
                        "height": int(height),
                    },
                    "pose": {
                        "position_xyz_m": [
                            float(camera_position[0]),
                            float(camera_position[1]),
                            float(camera_position[2]),
                        ],
                        "quaternion_wxyz": [
                            float(camera_orientation_wxyz[0]),
                            float(camera_orientation_wxyz[1]),
                            float(camera_orientation_wxyz[2]),
                            float(camera_orientation_wxyz[3]),
                        ],
                    },
                    "rgb_image": {
                        "ref": rgb_ref,
                    },
                    "depth_image": {
                        "unit": "meter",
                        "ref": depth_ref,
                    },
                }
            }

    def _ensure_unique_handle_object_ids(self, handles: Sequence[object]) -> None:
        object_ids: set[str] = set()
        for handle in handles:
            object_id = self.get_handle_object_id(handle)
            if object_id in object_ids:
                raise ValueError(f"duplicate table environment object id: {object_id}")
            object_ids.add(object_id)

    def get_handle_object_id(self, handle: object) -> str:
        # 查询和协议层都只依赖对象 id；这里兼容 Isaac Sim handle 上常见的 object_id / name 两种入口。
        object_id = getattr(handle, "object_id", None)
        if callable(object_id):
            object_id = object_id()
        if isinstance(object_id, str) and object_id:
            return object_id

        object_name = getattr(handle, "name", None)
        if callable(object_name):
            object_name = object_name()
        if isinstance(object_name, str) and object_name:
            return object_name

        raise ValueError(f"failed to resolve object id from handle: {handle!r}")

    def get_handle_prim_path(self, handle: object) -> str:
        prim_path = getattr(handle, "prim_path", None)
        if callable(prim_path):
            prim_path = prim_path()
        if isinstance(prim_path, str) and prim_path:
            return prim_path

        prim = getattr(handle, "prim", None)
        if callable(prim):
            prim = prim()
        if prim is not None and hasattr(prim, "GetPath"):
            resolved_path = prim.GetPath()
            prim_path_text = str(resolved_path)
            if prim_path_text:
                return prim_path_text

        raise ValueError(f"failed to resolve prim path from handle: {handle!r}")

    def _describe_table_object_handle(self, handle: object) -> tuple[str, str]:
        try:
            object_id = self.get_handle_object_id(handle)
        except Exception:
            object_id = "<unknown-object-id>"
        try:
            prim_path = self.get_handle_prim_path(handle)
        except Exception:
            prim_path = "<unknown-prim-path>"
        return object_id, prim_path

    def _remove_table_object_handle(self, handle: object) -> None:
        from isaacsim.core.utils.prims import delete_prim, is_prim_path_valid

        object_id = self.get_handle_object_id(handle)
        prim_path = self.get_handle_prim_path(handle)
        scene = getattr(self.world, "scene", None)
        if scene is not None and hasattr(scene, "object_exists") and scene.object_exists(object_id):
            scene.remove_object(object_id)
            return

        if is_prim_path_valid(prim_path):
            delete_prim(prim_path)
            return

        self.logger.info(
            "Table environment object is already absent from stage: object_id=%s prim_path=%s",
            object_id,
            prim_path,
        )

    def _build_object_transform_payload(self, handle: object) -> dict[str, Any]:
        position_xyz_m, quaternion_wxyz = handle.get_world_pose()
        scale_xyz = handle.get_world_scale()
        return {
            "id": self.get_handle_object_id(handle),
            "pose": {
                "position_xyz_m": [float(position_xyz_m[0]), float(position_xyz_m[1]), float(position_xyz_m[2])],
                "quaternion_wxyz": [
                    float(quaternion_wxyz[0]),
                    float(quaternion_wxyz[1]),
                    float(quaternion_wxyz[2]),
                    float(quaternion_wxyz[3]),
                ],
            },
            "scale_xyz": [float(scale_xyz[0]), float(scale_xyz[1]), float(scale_xyz[2])],
        }

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.worker_status = "shutting_down"

    def run_task(self, *, task_id: str, task_code: str, task_objects: list[dict[str, Any]]) -> dict[str, Any]:
        if self.robot_api is None:
            raise RuntimeError("robot API is not initialized")
        if self.current_task_id is not None:
            raise RuntimeError(f"another task is already running: {self.current_task_id}")

        compiled_code = compile(task_code, f"<simworker-task:{task_id}>", "exec")
        task_namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "__name__": "__simworker_task__",
        }
        exec(compiled_code, task_namespace, task_namespace)
        run_callable = task_namespace.get("run")
        if not callable(run_callable):
            raise ValueError("payload.task.code must define a callable run(robot, objects)")

        started_at = _utc_now_isoformat()
        self.current_task_id = task_id
        self.logger.info("Starting run_task task_id=%s object_count=%s", task_id, len(task_objects))
        try:
            result = run_callable(self.robot_api, task_objects)
            if result is not None:
                # 协议层当前不消费 run() 返回值，这里仅打日志，避免误以为结果会自动回传。
                self.logger.info("run_task task_id=%s returned a value that will be ignored", task_id)
        finally:
            self.current_task_id = None

        finished_at = _utc_now_isoformat()
        self.logger.info("Completed run_task task_id=%s", task_id)
        return {
            "task": {
                "id": task_id,
                "status": "succeeded",
                "result": None,
                "started_at": started_at,
                "finished_at": finished_at,
            }
        }

    def ensure_world_playing(self) -> None:
        with self.simulation_lock:
            self._ensure_world_playing_locked()

    def step_world_for_robot_action(self) -> None:
        # 机器人动作执行期间也必须继续沿用同一套世界推进逻辑，并按需刷新视频流。
        with self.simulation_lock:
            if self.world is None:
                raise RuntimeError("base environment world is not initialized")

            self._ensure_world_playing_locked()
            self.world.step(render=True)
            self._publish_stream_frames_after_current_step_locked(force=False)

    def _step_render_frames(self, num_frames: int) -> None:
        with self.simulation_lock:
            if self.world is None:
                raise RuntimeError("base environment world is not initialized")
            self._ensure_world_playing_locked()
            for _ in range(num_frames):
                self.world.step(render=True)

    def _capture_camera_rgba(self, camera: object) -> Any:
        # 这里尽量直接复用 Isaac Sim 返回的数组，减少流热路径上的额外拷贝。
        return camera.get_rgba()

    def _allocate_stream_ids(self, camera_id: str) -> tuple[str, str]:
        next_index = self.stream_counters.get(camera_id, 0) + 1
        self.stream_counters[camera_id] = next_index
        return (
            f"stream-{camera_id}-{next_index:03d}",
            f"stream-ref-{camera_id}-{next_index:03d}",
        )

    def publish_camera_stream_frames_if_due(self, *, force: bool = False) -> None:
        # 真实 Isaac Sim 环境下，world.step / camera.get_rgba 需要留在主线程执行。
        # 因此这里由 worker 主循环在空闲时主动 tick，而不是开后台线程刷新。
        with self.simulation_lock:
            if self.world is None:
                return

            running_streams = self._get_running_streams_locked()
            if not running_streams:
                return

            now = time.monotonic()
            if not self._should_publish_running_streams_locked(now=now, force=force):
                return

            self._ensure_world_playing_locked()
            self.world.step(render=True)
            self._publish_stream_frames_after_current_step_locked(force=True, now=now, running_streams=running_streams)

    def _ensure_world_playing_locked(self) -> None:
        if self.world is None:
            raise RuntimeError("base environment world is not initialized")
        if hasattr(self.world, "is_playing") and not self.world.is_playing():
            self.world.play()

    def _get_running_streams_locked(self) -> list[CameraStreamRuntimeState]:
        return [stream_state for stream_state in self.streams_by_id.values() if stream_state.status == "running"]

    def _should_publish_running_streams_locked(self, *, now: float, force: bool) -> bool:
        if force:
            return True
        if self.last_stream_publish_monotonic <= 0.0:
            return True
        return (now - self.last_stream_publish_monotonic) >= _STREAM_LOOP_RENDER_PERIOD_SEC

    def _publish_stream_frames_after_current_step_locked(
        self,
        *,
        force: bool,
        now: float | None = None,
        running_streams: Sequence[CameraStreamRuntimeState] | None = None,
    ) -> None:
        if running_streams is None:
            running_streams = self._get_running_streams_locked()
        if not running_streams:
            return

        publish_now = time.monotonic() if now is None else now
        if not self._should_publish_running_streams_locked(now=publish_now, force=force):
            return

        self.last_stream_publish_monotonic = publish_now
        for stream_state in running_streams:
            camera = self.cameras.get(stream_state.camera_id)
            if camera is None:
                stream_state.mark_error()
                self.logger.error(
                    "Active stream camera no longer exists: stream_id=%s camera_id=%s",
                    stream_state.stream_id,
                    stream_state.camera_id,
                )
                continue

            try:
                rgba_image = self._capture_camera_rgba(camera)
                stream_state.write_rgb_frame(rgba_image)
            except Exception:
                stream_state.mark_error()
                self.logger.exception(
                    "Failed to publish latest frame for stream_id=%s camera_id=%s",
                    stream_state.stream_id,
                    stream_state.camera_id,
                )

    def _remove_stream_state(self, stream_state: CameraStreamRuntimeState) -> None:
        self.streams_by_id.pop(stream_state.stream_id, None)
        current_stream_id = self.stream_ids_by_camera.get(stream_state.camera_id)
        if current_stream_id == stream_state.stream_id:
            self.stream_ids_by_camera.pop(stream_state.camera_id, None)
        if not self.streams_by_id:
            self.last_stream_publish_monotonic = 0.0
        stream_state.close()

    def _cleanup_all_streams(self) -> None:
        with self.simulation_lock:
            stream_states = list(self.streams_by_id.values())
            self.streams_by_id.clear()
            self.stream_ids_by_camera.clear()
            self.last_stream_publish_monotonic = 0.0
            for stream_state in stream_states:
                try:
                    stream_state.close()
                except Exception:
                    self.logger.exception("Failed to cleanup camera stream: %s", stream_state.stream_id)

    def _allocate_artifact_id(self, artifact_kind: str) -> str:
        # artifact id 在一次 worker 运行内单调递增，便于控制面日志和产物文件做关联。
        next_index = self.artifact_counters.get(artifact_kind, 0) + 1
        self.artifact_counters[artifact_kind] = next_index
        return f"artifact-{artifact_kind}-{next_index:03d}"

    def _build_artifact_ref(self, artifact_id: str, artifact_path: Path, content_type: str) -> dict[str, str]:
        return {
            "id": artifact_id,
            "kind": "artifact_file",
            "path": str(artifact_path),
            "content_type": content_type,
        }

    def _write_rgb_artifact(self, camera_id: str, snapshot_tag: str, rgba_image: Any) -> dict[str, str]:
        from PIL import Image

        artifact_id = self._allocate_artifact_id("rgb")
        artifact_path = self.artifacts_dir / f"{camera_id}_rgb_{snapshot_tag}.png"
        rgb_image = rgba_image[:, :, :3] if rgba_image.shape[-1] == 4 else rgba_image
        Image.fromarray(rgb_image, mode="RGB").save(artifact_path)
        return self._build_artifact_ref(artifact_id, artifact_path, "image/png")

    def _write_depth_artifact(self, camera_id: str, snapshot_tag: str, depth_image: Any) -> dict[str, str]:
        import numpy as np

        artifact_id = self._allocate_artifact_id("depth")
        artifact_path = self.artifacts_dir / f"{camera_id}_depth_{snapshot_tag}.npy"
        np.save(artifact_path, depth_image.astype(np.float32, copy=False))
        return self._build_artifact_ref(artifact_id, artifact_path, "application/x-npy")

    def close(self) -> None:
        self._cleanup_all_streams()
        self.robot_api = None
        if self.world is not None:
            try:
                from isaacsim.core.api.world import World
            except ImportError:
                pass
            else:
                World.clear_instance()
            self.base_environment = None
        if self.simulation_app is not None:
            self.logger.info("Closing SimulationApp")
            self.simulation_app.close()


def _allocate_run_dir(session_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base_dir = session_dir / timestamp
    if not base_dir.exists():
        base_dir.mkdir(parents=True, exist_ok=False)
        return base_dir

    suffix = 1
    while True:
        # 同一秒内重复启动时追加序号，保持 run_dir 唯一。
        candidate = session_dir / f"{timestamp}_{suffix:02d}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        suffix += 1


def _configure_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"simworker.{log_path.parent.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

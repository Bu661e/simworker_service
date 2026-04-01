from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from simworker.base_environments import BaseEnvironmentHandles, create_default_tabletop_base_environment
from simworker.table_environments import ensure_supported_table_environment_id, load_table_environment


@dataclass(slots=True)
class WorkerRuntime:
    session_dir: Path
    run_dir: Path
    artifacts_dir: Path
    logger: logging.Logger
    simulation_app: object | None = None
    base_environment: BaseEnvironmentHandles | None = None
    worker_status: str = "starting"
    robot_status: str = "idle"
    current_task_id: str | None = None
    table_env_id: str | None = None
    # 这里直接保存 table_env 创建出的 handle；位姿和缩放统一在查询时直接从 handle 现查。
    objects: list[object] = field(default_factory=list)
    stream_statuses: dict[str, str] = field(default_factory=dict)
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
        return {
            "table_env": {
                "loaded": self.table_env_id is not None,
                "id": self.table_env_id,
            },
            "object_count": len(self.objects),
            "objects": [self._build_object_transform_payload(scene_object) for scene_object in self.objects],
        }

    @property
    def active_stream_count(self) -> int:
        return sum(1 for status in self.stream_statuses.values() if status == "running")

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
    def robot(self) -> object | None:
        if self.base_environment is None:
            return None
        return self.base_environment.robot

    @property
    def table(self) -> object | None:
        if self.base_environment is None:
            return None
        return self.base_environment.table

    def load_table_env(self, table_env_id: str) -> list[object]:
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

    def close(self) -> None:
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

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from simworker.table_environments.default import load_default_table_environment
from simworker.table_environments.multi_geometry import load_multi_geometry_table_environment
from simworker.table_environments.ycb import load_ycb_table_environment

if TYPE_CHECKING:
    from simworker.runtime import WorkerRuntime

_TABLE_ENV_BUILDERS: dict[str, Callable[[WorkerRuntime], list[object]]] = {
    "default": load_default_table_environment,
    "multi_geometry": load_multi_geometry_table_environment,
    "ycb": load_ycb_table_environment,
}


def ensure_supported_table_environment_id(table_env_id: str) -> None:
    # 统一在这里收口 table_env_id 的合法值校验，避免各调用点各写一套错误文案。
    if table_env_id in _TABLE_ENV_BUILDERS:
        return
    supported_ids = ", ".join(sorted(_TABLE_ENV_BUILDERS))
    raise ValueError(f"unsupported table_env_id: {table_env_id}; supported values: {supported_ids}")


def load_table_environment(runtime: WorkerRuntime, table_env_id: str) -> list[object]:
    ensure_supported_table_environment_id(table_env_id)
    builder = _TABLE_ENV_BUILDERS[table_env_id]
    return list(builder(runtime))


def list_table_environment_ids() -> list[str]:
    return sorted(_TABLE_ENV_BUILDERS)


__all__ = [
    "ensure_supported_table_environment_id",
    "list_table_environment_ids",
    "load_table_environment",
]

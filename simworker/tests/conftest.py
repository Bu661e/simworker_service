from __future__ import annotations

import os
import re
import shutil
from hashlib import sha1
from datetime import datetime
from pathlib import Path

import pytest

_TEST_OUTPUT_ROOT_ENV = "SIMWORKER_TEST_OUTPUT_ROOT"
_CASE_DIR_NAME_BY_TEST_ID: dict[tuple[str, str], str] = {
    (
        "test_simworker_integration",
        "test_simworker_default_env_simple_interfaces_and_two_camera_snapshots",
    ): "sw_default_simple",
    (
        "test_simworker_integration",
        "test_simworker_ycb_env_simple_interfaces_and_two_camera_snapshots",
    ): "sw_ycb_simple",
    (
        "test_simworker_integration",
        "test_simworker_multi_geometry_env_simple_interfaces_and_two_camera_snapshots",
    ): "sw_multi_geometry_simple",
    (
        "test_simworker_integration",
        "test_simworker_default_env_two_camera_snapshots_and_dual_streams",
    ): "sw_default_streams",
    (
        "test_simworker_integration",
        "test_simworker_default_env_run_task_keeps_dual_streams_publishing",
    ): "sw_default_run_task",
    (
        "test_sim_manager_integration",
        "test_sim_manager_default_env_exercises_all_interfaces",
    ): "sm_default_all",
}


def _sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    if not sanitized:
        raise ValueError("test output directory component is empty after sanitization")
    return sanitized


def _default_test_run_output_root() -> Path:
    simworker_dir = Path(__file__).resolve().parents[1]
    run_name = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return simworker_dir / "test_runs" / run_name


def _fallback_case_dir_name(file_stem: str, test_name: str) -> str:
    file_hint = file_stem.replace("test_", "")
    digest = sha1(f"{file_stem}::{test_name}".encode("utf-8")).hexdigest()[:8]
    return f"{file_hint}_{digest}"


def _resolve_case_dir_name(file_stem: str, test_name: str) -> str:
    explicit_name = _CASE_DIR_NAME_BY_TEST_ID.get((file_stem, test_name))
    if explicit_name is not None:
        return explicit_name
    return _fallback_case_dir_name(file_stem, test_name)


@pytest.fixture(scope="session")
def test_run_output_root() -> Path:
    configured_root_text = os.environ.get(_TEST_OUTPUT_ROOT_ENV)
    if configured_root_text:
        output_root = Path(configured_root_text).expanduser().resolve()
    else:
        output_root = _default_test_run_output_root().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


@pytest.fixture
def case_output_dir(request: pytest.FixtureRequest, test_run_output_root: Path) -> Path:
    file_stem = _sanitize_path_component(Path(str(request.node.fspath)).stem)
    test_name = _sanitize_path_component(request.node.name)
    case_dir = test_run_output_root / _resolve_case_dir_name(file_stem, test_name)
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir

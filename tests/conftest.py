from __future__ import annotations

import os
import sys
import shutil
from datetime import datetime
from pathlib import Path
from hashlib import sha1

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TEST_OUTPUT_ROOT_ENV = "API_TEST_OUTPUT_ROOT"
_CASE_DIR_NAME_BY_TEST_NAME: dict[str, str] = {
    "test_health_endpoint_returns_ok_payload": "api_health",
    "test_capture_endpoint_returns_json_payload_with_download_urls": "api_capture_json",
    "test_capture_artifact_download_endpoint_returns_binary_files": "api_capture_download",
    "test_capture_artifact_download_endpoint_returns_json_error_for_unknown_capture": "api_capture_missing",
    "test_stream_response_builder_returns_mjpeg_bytes_and_cleans_up": "api_stream_builder",
    "test_open_mjpeg_stream_unregisters_consumer_shared_memory": "api_open_mjpeg_stream",
    "test_table_env_endpoints_delegate_to_sim_manager": "api_table_env",
    "test_robot_endpoints_delegate_to_sim_manager": "api_robot",
    "test_worker_restart_endpoint_restarts_same_manager_instance": "api_worker_restart",
    "test_sim_manager_errors_are_wrapped_as_ok_false_json": "api_sim_manager_error",
    "test_request_validation_errors_are_wrapped_as_ok_false_json": "api_validation_error",
    "test_fastapi_real_integration_exercises_non_stream_interfaces": "api_real_non_stream",
    "test_fastapi_real_integration_streams_mjpeg_frames": "api_real_mjpeg",
}


def _default_test_run_output_root() -> Path:
    run_name = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return _REPO_ROOT / "test_runs" / run_name


def _fallback_case_dir_name(test_name: str) -> str:
    digest = sha1(test_name.encode("utf-8")).hexdigest()[:8]
    return f"api_{digest}"


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
    case_dir_name = _CASE_DIR_NAME_BY_TEST_NAME.get(request.node.name)
    if case_dir_name is None:
        case_dir_name = _fallback_case_dir_name(request.node.name)
    case_dir = test_run_output_root / case_dir_name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir

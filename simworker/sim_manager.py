from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, TextIO

from simworker.protocol import recv_json_message, send_json_message

_DEFAULT_STARTUP_TIMEOUT_SEC = 240.0
_DEFAULT_REQUEST_TIMEOUT_SEC = 60.0
_DEFAULT_SHUTDOWN_TIMEOUT_SEC = 60.0


class SimManagerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        command_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.command_type = command_type
        self.payload = payload or {}


class SimManager:
    def __init__(
        self,
        *,
        session_dir: str | Path,
        control_socket_path: str | Path,
        python_bin: str = sys.executable,
        worker_module: str = "simworker.entrypoint",
        cwd: str | Path | None = None,
        startup_timeout_sec: float = _DEFAULT_STARTUP_TIMEOUT_SEC,
        request_timeout_sec: float = _DEFAULT_REQUEST_TIMEOUT_SEC,
        shutdown_timeout_sec: float = _DEFAULT_SHUTDOWN_TIMEOUT_SEC,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.control_socket_path = Path(control_socket_path).expanduser().resolve()
        self.python_bin = python_bin
        self.worker_module = worker_module
        # 默认以仓库根目录作为工作目录，保证 `-m simworker.entrypoint` 在本地开发环境可直接运行。
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else Path(__file__).resolve().parents[1]
        self.startup_timeout_sec = startup_timeout_sec
        self.request_timeout_sec = request_timeout_sec
        self.shutdown_timeout_sec = shutdown_timeout_sec
        self.extra_env = dict(extra_env or {})

        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._process_log_handle: TextIO | None = None
        self._request_counter = 0

    @property
    def process_log_path(self) -> Path:
        return self.session_dir / "simworker.log"

    def __enter__(self) -> "SimManager":
        self.ensure_started()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running_unlocked()

    def start(self) -> "SimManager":
        with self._lock:
            if self._is_running_unlocked():
                self._request("hello", {}, auto_start=False)
                return self

            self.session_dir.mkdir(parents=True, exist_ok=True)
            self.control_socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self.control_socket_path.exists():
                self.control_socket_path.unlink()

            self._open_process_log_handle()
            try:
                process = subprocess.Popen(
                    [
                        self.python_bin,
                        "-m",
                        self.worker_module,
                        "--session-dir",
                        str(self.session_dir),
                        "--control-socket-path",
                        str(self.control_socket_path),
                    ],
                    cwd=str(self.cwd),
                    env=self._build_subprocess_env(),
                    stdout=self._process_log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception:
                self._close_process_log_handle_unlocked()
                raise
            self._process = process
            try:
                self._wait_for_socket_ready_unlocked()
                # 按设计约定，`hello` 既是健康检查也是 ready probe。
                self._request("hello", {}, auto_start=False)
            except Exception:
                self._force_cleanup_process_unlocked()
                raise
            return self

    def ensure_started(self) -> "SimManager":
        return self.start()

    def _call_command(self, command_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # API 层不需要感知控制协议 JSON；这里只暴露内部统一发送入口。
        with self._lock:
            return self._request(command_type, payload or {}, auto_start=True)

    def hello(self) -> dict[str, Any]:
        return self._call_command("hello", {})

    def list_table_env(self) -> dict[str, Any]:
        return self._call_command("list_table_env", {})

    def list_api(self) -> str:
        payload = self._call_command("list_api", {})
        api_text = payload.get("api")
        if not isinstance(api_text, str) or not api_text:
            raise SimManagerError(
                "worker returned invalid list_api payload: payload.api must be a non-empty string",
                command_type="list_api",
                payload=payload,
            )
        return api_text

    def list_camera(self) -> dict[str, Any]:
        return self._call_command("list_camera", {})

    def load_table_env(self, table_env_id: str) -> dict[str, Any]:
        if not table_env_id:
            raise ValueError("table_env_id must be a non-empty string")
        return self._call_command("load_table_env", {"table_env_id": table_env_id})

    def get_table_env_objects_info(self) -> dict[str, Any]:
        return self._call_command("get_table_env_objects_info", {})

    def get_robot_status(self) -> dict[str, Any]:
        return self._call_command("get_robot_status", {})

    def get_camera_info(self, camera_id: str) -> dict[str, Any]:
        if not camera_id:
            raise ValueError("camera_id must be a non-empty string")
        return self._call_command("get_camera_info", {"camera": {"id": camera_id}})

    def start_camera_stream(self, camera_id: str, *, buffer_mode: str = "latest_frame") -> dict[str, Any]:
        if not camera_id:
            raise ValueError("camera_id must be a non-empty string")
        if not buffer_mode:
            raise ValueError("buffer_mode must be a non-empty string")
        return self._call_command(
            "start_camera_stream",
            {
                "camera": {"id": camera_id},
                "stream": {"buffer_mode": buffer_mode},
            },
        )

    def stop_camera_stream(self, stream_id: str) -> dict[str, Any]:
        if not stream_id:
            raise ValueError("stream_id must be a non-empty string")
        return self._call_command(
            "stop_camera_stream",
            {
                "stream": {"id": stream_id},
            },
        )

    def run_task(self, *, task_id: str, objects: list[dict[str, Any]], code: str) -> dict[str, Any]:
        if not task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(objects, list):
            raise ValueError("objects must be a list")
        if not code:
            raise ValueError("code must be a non-empty string")
        return self._call_command(
            "run_task",
            {
                "task": {
                    "id": task_id,
                    "objects": objects,
                    "code": code,
                }
            },
        )

    def shutdown(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running_unlocked():
                self._close_process_log_handle_unlocked()
                return {"worker": {"status": "stopped"}}

            shutdown_payload = self._request("shutdown", {}, auto_start=False)
            self._wait_for_worker_exit_unlocked()
            self._close_process_log_handle_unlocked()
            return shutdown_payload

    def close(self) -> None:
        with self._lock:
            try:
                if self._is_running_unlocked():
                    try:
                        self._request("shutdown", {}, auto_start=False)
                    except Exception:
                        # 进入 close 流程时优先保证资源最终回收，不把关闭失败再向外扩散成二次错误。
                        pass
                self._wait_for_worker_exit_unlocked()
            finally:
                self._force_cleanup_process_unlocked()

    def _request(self, command_type: str, payload: dict[str, Any], *, auto_start: bool) -> dict[str, Any]:
        if auto_start:
            self.start()

        if not self._is_running_unlocked():
            raise SimManagerError(f"worker is not running; cannot send command {command_type}")

        request_id = self._next_request_id_unlocked(command_type)
        request_message = {
            "request_id": request_id,
            "command_type": command_type,
            "payload": payload,
        }

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.request_timeout_sec)
                sock.connect(str(self.control_socket_path))
                send_json_message(sock, request_message)
                response = recv_json_message(sock)
        except TimeoutError as exc:
            log_tail = self._tail_process_log_unlocked()
            self._force_cleanup_process_unlocked()
            raise SimManagerError(
                "timed out waiting for worker response "
                f"for command {command_type}; worker process was terminated.\n"
                f"log_tail=\n{log_tail}",
                request_id=request_id,
                command_type=command_type,
            ) from exc
        except OSError as exc:
            raise SimManagerError(
                f"failed to send command {command_type} to worker socket {self.control_socket_path}: {exc}"
            ) from exc

        if response is None:
            raise SimManagerError(
                f"worker returned no response for command {command_type}",
                request_id=request_id,
                command_type=command_type,
            )
        if response.get("request_id") != request_id:
            raise SimManagerError(
                f"worker returned mismatched request_id for command {command_type}",
                request_id=request_id,
                command_type=command_type,
                payload=response if isinstance(response, dict) else {},
            )

        payload_obj = response.get("payload", {})
        if not isinstance(payload_obj, dict):
            raise SimManagerError(
                f"worker returned invalid payload type for command {command_type}",
                request_id=request_id,
                command_type=command_type,
            )

        if response.get("ok") is not True:
            error_message = response.get("error_message")
            if not isinstance(error_message, str) or not error_message:
                error_message = f"worker command {command_type} failed without an error_message"
            raise SimManagerError(
                error_message,
                request_id=request_id,
                command_type=command_type,
                payload=payload_obj,
            )

        return payload_obj

    def _is_running_unlocked(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._close_process_log_handle_unlocked()
        return self._can_connect_socket_unlocked()

    def _wait_for_socket_ready_unlocked(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_sec
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise SimManagerError(
                    "simworker process exited before control socket became ready.\n"
                    f"returncode={self._process.returncode}\n"
                    f"log_tail=\n{self._tail_process_log_unlocked()}"
                )
            try:
                if self._can_connect_socket_unlocked():
                    return
            except OSError as exc:
                last_error = exc
            time.sleep(0.2)

        raise SimManagerError(
            "timed out waiting for simworker control socket to become ready.\n"
            f"socket_path={self.control_socket_path}\n"
            f"last_error={last_error!r}\n"
            f"log_tail=\n{self._tail_process_log_unlocked()}"
        )

    def _wait_for_worker_exit_unlocked(self) -> None:
        if self._process is not None:
            try:
                self._process.wait(timeout=self.shutdown_timeout_sec)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=10.0)
            finally:
                self._process = None
            return

        deadline = time.monotonic() + self.shutdown_timeout_sec
        while time.monotonic() < deadline:
            if not self._can_connect_socket_unlocked():
                return
            time.sleep(0.2)

    def _can_connect_socket_unlocked(self) -> bool:
        if not self.control_socket_path.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(self.control_socket_path))
            return True
        except OSError:
            return False

    def _next_request_id_unlocked(self, command_type: str) -> str:
        self._request_counter += 1
        return f"req-{self._request_counter:06d}-{command_type}"

    def _build_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _open_process_log_handle(self) -> None:
        if self._process_log_handle is not None and not self._process_log_handle.closed:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # 把子进程 stdout/stderr 固定落到 session 根目录，方便 API 层快速排查启动期问题。
        self._process_log_handle = self.process_log_path.open("a", buffering=1, encoding="utf-8")

    def _close_process_log_handle_unlocked(self) -> None:
        if self._process_log_handle is None:
            return
        self._process_log_handle.close()
        self._process_log_handle = None

    def _tail_process_log_unlocked(self, max_chars: int = 8000) -> str:
        if not self.process_log_path.exists():
            return ""
        content = self.process_log_path.read_text(encoding="utf-8", errors="replace")
        return content[-max_chars:]

    def _force_cleanup_process_unlocked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10.0)
        self._process = None
        self._close_process_log_handle_unlocked()

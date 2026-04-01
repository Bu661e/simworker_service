from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_FRAME_HEADER = struct.Struct(">I")
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ControlProtocolError(RuntimeError):
    """Raised when the control socket stream violates the framing contract."""


class ConnectionClosed(ControlProtocolError):
    """Raised when the peer closes the socket while a frame is in flight."""


@dataclass(slots=True, frozen=True)
class ControlRequest:
    request_id: str
    command_type: str
    payload: dict[str, Any]

    @classmethod
    def from_json_obj(cls, value: object) -> "ControlRequest":
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")

        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")

        command_type = value.get("command_type")
        if not isinstance(command_type, str) or not command_type:
            raise ValueError("command_type must be a non-empty string")

        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        return cls(request_id=request_id, command_type=command_type, payload=payload)


@dataclass(slots=True, frozen=True)
class ControlResponse:
    request_id: str | None
    ok: bool
    payload: dict[str, Any]
    error_message: str | None = None

    @classmethod
    def success(cls, request_id: str | None, payload: dict[str, Any]) -> "ControlResponse":
        return cls(request_id=request_id, ok=True, payload=payload)

    @classmethod
    def error(
        cls,
        request_id: str | None,
        error_message: str,
        payload: dict[str, Any] | None = None,
    ) -> "ControlResponse":
        return cls(
            request_id=request_id,
            ok=False,
            error_message=error_message,
            payload=payload or {},
        )

    def to_json_obj(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": self.ok,
            "payload": self.payload,
        }
        if self.request_id is not None:
            response["request_id"] = self.request_id
        if self.error_message is not None:
            response["error_message"] = self.error_message
        return response


def send_json_message(sock: socket.socket, message: dict[str, Any]) -> None:
    # 控制面固定使用“4 字节长度头 + UTF-8 JSON”分帧，避免依赖换行符。
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ControlProtocolError("message exceeds maximum control frame size")
    sock.sendall(_FRAME_HEADER.pack(len(payload)))
    sock.sendall(payload)


def recv_json_message(sock: socket.socket) -> dict[str, Any] | None:
    header = _read_exact(sock, _FRAME_HEADER.size)
    if header is None:
        return None

    (payload_size,) = _FRAME_HEADER.unpack(header)
    if payload_size > _MAX_MESSAGE_BYTES:
        raise ControlProtocolError(f"message size {payload_size} exceeds limit {_MAX_MESSAGE_BYTES}")

    payload_bytes = _read_exact(sock, payload_size)
    if payload_bytes is None:
        raise ConnectionClosed("peer closed socket before completing frame payload")

    try:
        message = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlProtocolError("invalid UTF-8 JSON payload") from exc

    if not isinstance(message, dict):
        raise ControlProtocolError("top-level message must decode to a JSON object")
    return message


def _read_exact(sock: socket.socket, size: int) -> bytes | None:
    remaining = size
    chunks = bytearray()
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            if not chunks:
                return None
            raise ConnectionClosed("peer closed socket before frame boundary")
        chunks.extend(chunk)
        remaining -= len(chunk)
    return bytes(chunks)


class UnixSocketControlServer:
    def __init__(self, socket_path: Path, logger: Any) -> None:
        self._socket_path = socket_path
        self._logger = logger
        self._server_socket: socket.socket | None = None

    def __enter__(self) -> "UnixSocketControlServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()

        # 当前设计按单连接控制面处理，便于把命令执行语义保持为严格串行。
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(self._socket_path))
        server_socket.listen(1)
        self._server_socket = server_socket
        self._logger.info("Control socket listening on %s", self._socket_path)

    def serve(
        self,
        handle_request: Callable[[ControlRequest], ControlResponse],
        should_stop: Callable[[], bool],
    ) -> None:
        if self._server_socket is None:
            raise RuntimeError("server socket is not started")

        while not should_stop():
            conn, _ = self._server_socket.accept()
            self._logger.info("Accepted control connection on %s", self._socket_path)
            with conn:
                while not should_stop():
                    try:
                        raw_message = recv_json_message(conn)
                    except ConnectionClosed as exc:
                        self._logger.info("Control connection closed: %s", exc)
                        break
                    except ControlProtocolError as exc:
                        self._logger.warning("Protocol error on control connection: %s", exc)
                        break

                    if raw_message is None:
                        self._logger.info("Control client disconnected cleanly")
                        break

                    # 对格式错误的请求尽量回带 request_id，便于上层做失败关联。
                    request_id = raw_message.get("request_id") if isinstance(raw_message.get("request_id"), str) else None
                    try:
                        request = ControlRequest.from_json_obj(raw_message)
                        response = handle_request(request)
                    except ValueError as exc:
                        response = ControlResponse.error(request_id=request_id, error_message=str(exc))
                    except Exception:
                        self._logger.exception("Unhandled exception while processing control request")
                        response = ControlResponse.error(
                            request_id=request_id,
                            error_message="internal worker error",
                        )

                    send_json_message(conn, response.to_json_obj())
                    if should_stop():
                        return

    def close(self) -> None:
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None
        if self._socket_path.exists():
            self._socket_path.unlink()

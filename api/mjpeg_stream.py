from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from multiprocessing import resource_tracker
from multiprocessing import shared_memory
from typing import Any, AsyncIterator, Protocol

from fastapi import Request
from fastapi.responses import StreamingResponse
from PIL import Image

from simworker.camera_streams import decode_latest_frame_header, latest_frame_header_size_bytes

logger = logging.getLogger("api.mjpeg")

_MJPEG_BOUNDARY = "frame"
_MJPEG_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
_STREAM_HEADER_SIZE_BYTES = latest_frame_header_size_bytes()
_READABLE_SNAPSHOT_TIMEOUT_SEC = 2.0
_WAIT_FOR_NEXT_FRAME_TIMEOUT_SEC = 5.0
_POLL_INTERVAL_SEC = 0.01


class StreamCapableSimManager(Protocol):
    def start_camera_stream(self, camera_id: str, *, buffer_mode: str = "latest_frame") -> dict[str, Any]: ...
    def stop_camera_stream(self, stream_id: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class OpenedMjpegStream:
    stream_id: str
    shm: shared_memory.SharedMemory


def build_mjpeg_streaming_response(
    request: Request,
    sim_manager: StreamCapableSimManager,
    camera_id: str,
) -> StreamingResponse:
    opened_stream = _open_mjpeg_stream(sim_manager, camera_id)
    return StreamingResponse(
        _iter_mjpeg_multipart_bytes(request, sim_manager, opened_stream),
        media_type=_MJPEG_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


def _open_mjpeg_stream(sim_manager: StreamCapableSimManager, camera_id: str) -> OpenedMjpegStream:
    stream_payload = sim_manager.start_camera_stream(camera_id, buffer_mode="latest_frame")
    stream_object = stream_payload.get("stream")
    if not isinstance(stream_object, dict):
        raise ValueError("camera stream payload is missing stream object")

    stream_id = stream_object.get("id")
    if not isinstance(stream_id, str) or not stream_id:
        raise ValueError("camera stream payload is missing stream.id")

    buffer_mode = stream_object.get("buffer_mode")
    if buffer_mode != "latest_frame":
        raise ValueError(f"unsupported stream buffer_mode: {buffer_mode!r}")

    pixel_format = stream_object.get("pixel_format")
    if pixel_format != "rgb24":
        raise ValueError(f"unsupported stream pixel_format: {pixel_format!r}")

    ref_payload = stream_object.get("ref")
    if not isinstance(ref_payload, dict):
        raise ValueError("camera stream payload is missing stream.ref")

    ref_path = ref_payload.get("path")
    if not isinstance(ref_path, str) or not ref_path:
        raise ValueError("camera stream payload is missing stream.ref.path")

    shm_name = _shared_memory_name_from_path(ref_path)
    try:
        shm = shared_memory.SharedMemory(name=shm_name, create=False)
        _unregister_consumer_shared_memory(shm)
    except Exception:
        try:
            sim_manager.stop_camera_stream(stream_id)
        except Exception:
            logger.exception("Failed to stop stream after shared memory attach failure: stream_id=%s", stream_id)
        raise

    return OpenedMjpegStream(
        stream_id=stream_id,
        shm=shm,
    )


async def _iter_mjpeg_multipart_bytes(
    request: Request,
    sim_manager: StreamCapableSimManager,
    opened_stream: OpenedMjpegStream,
) -> AsyncIterator[bytes]:
    last_frame_id = -1
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                header, frame_bytes = await asyncio.to_thread(
                    _wait_for_next_frame,
                    opened_stream.shm,
                    last_frame_id=last_frame_id,
                    timeout_sec=_WAIT_FOR_NEXT_FRAME_TIMEOUT_SEC,
                )
            except TimeoutError:
                continue

            jpeg_bytes = await asyncio.to_thread(
                _encode_rgb24_frame_as_jpeg,
                frame_bytes,
                width=header["width"],
                height=header["height"],
            )
            last_frame_id = header["frame_id"]
            yield _build_mjpeg_part(jpeg_bytes)
    finally:
        try:
            opened_stream.shm.close()
        except Exception:
            logger.exception("Failed to close MJPEG shared memory: stream_id=%s", opened_stream.stream_id)
        try:
            sim_manager.stop_camera_stream(opened_stream.stream_id)
        except Exception:
            logger.exception("Failed to stop MJPEG stream: stream_id=%s", opened_stream.stream_id)


def _build_mjpeg_part(jpeg_bytes: bytes) -> bytes:
    headers = (
        f"--{_MJPEG_BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg_bytes)}\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + jpeg_bytes + b"\r\n"


def _encode_rgb24_frame_as_jpeg(frame_bytes: bytes, *, width: int, height: int) -> bytes:
    if len(frame_bytes) != width * height * 3:
        raise ValueError(
            f"rgb24 frame size mismatch: expected {width * height * 3} bytes, got {len(frame_bytes)} bytes"
        )
    image = Image.frombuffer("RGB", (width, height), frame_bytes, "raw", "RGB", 0, 1)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _shared_memory_name_from_path(ref_path: str) -> str:
    if not ref_path.startswith("shm://"):
        raise ValueError(f"unexpected shared memory path: {ref_path}")
    shm_name = ref_path.removeprefix("shm://")
    if not shm_name:
        raise ValueError("shared memory path is missing name")
    return shm_name


def _unregister_consumer_shared_memory(shm: shared_memory.SharedMemory) -> None:
    tracked_name = getattr(shm, "_name", None)
    if not isinstance(tracked_name, str) or not tracked_name:
        return
    try:
        resource_tracker.unregister(tracked_name, "shared_memory")
    except Exception:
        logger.debug("Failed to unregister shared memory from resource_tracker: %s", tracked_name, exc_info=True)


def _wait_for_next_frame(
    shm: shared_memory.SharedMemory,
    *,
    last_frame_id: int,
    timeout_sec: float,
) -> tuple[dict[str, Any], bytes]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        header, frame_bytes = _read_latest_stream_snapshot(shm, timeout_sec=_READABLE_SNAPSHOT_TIMEOUT_SEC)
        if header["frame_id"] > last_frame_id:
            return header, frame_bytes
        time.sleep(_POLL_INTERVAL_SEC)
    raise TimeoutError(f"timed out waiting for stream frame after frame_id={last_frame_id}")


def _read_latest_stream_snapshot(
    shm: shared_memory.SharedMemory,
    *,
    timeout_sec: float,
) -> tuple[dict[str, Any], bytes]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        header_start = decode_latest_frame_header(shm.buf[:_STREAM_HEADER_SIZE_BYTES])
        if header_start["seq"] % 2 == 1 or header_start["data_size_bytes"] <= 0:
            time.sleep(_POLL_INTERVAL_SEC)
            continue

        data_size_bytes = header_start["data_size_bytes"]
        frame_bytes = bytes(shm.buf[_STREAM_HEADER_SIZE_BYTES : _STREAM_HEADER_SIZE_BYTES + data_size_bytes])
        header_end = decode_latest_frame_header(shm.buf[:_STREAM_HEADER_SIZE_BYTES])
        if header_start["seq"] != header_end["seq"]:
            continue
        if header_end["seq"] % 2 == 1 or header_end["data_size_bytes"] <= 0:
            continue
        return header_end, frame_bytes

    raise TimeoutError("timed out waiting for readable latest-frame snapshot")

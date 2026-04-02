from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Any, Sequence

_STREAM_MAGIC = b"SIMSTRM1"
_STREAM_LAYOUT = "latest_frame_v1"
_PIXEL_FORMAT_RGB24 = "rgb24"
_HEADER_STRUCT = struct.Struct("<8s16sQIIIIIQQ16s44x")
_HEADER_SIZE = _HEADER_STRUCT.size
_SHARED_MEMORY_NAME_MAX_LEN = 64


@dataclass(slots=True)
class CameraStreamRuntimeState:
    stream_id: str
    ref_id: str
    camera_id: str
    shm: shared_memory.SharedMemory
    resolution: tuple[int, int]
    frame_capacity_bytes: int
    buffer_mode: str = "latest_frame"
    pixel_format: str = _PIXEL_FORMAT_RGB24
    status: str = "running"
    seq: int = 0
    frame_id: int = 0
    last_timestamp_ns: int = 0
    # 这里直接缓存 header+frame_data 的总视图，避免每帧都重复拼 buffer 对象。
    buffer: memoryview = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.buffer = self.shm.buf
        self._write_header(
            seq=self.seq,
            width=self.resolution[0],
            height=self.resolution[1],
            stride_bytes=self.resolution[0] * 3,
            data_size_bytes=0,
            timestamp_ns=0,
            frame_id=0,
        )

    @property
    def shm_name(self) -> str:
        return self.shm.name

    def build_control_payload(self) -> dict[str, Any]:
        return {
            "id": self.stream_id,
            "status": self.status,
            "buffer_mode": self.buffer_mode,
            "pixel_format": self.pixel_format,
            "resolution": [self.resolution[0], self.resolution[1]],
            "ref": {
                "id": self.ref_id,
                "kind": "shared_memory",
                "path": f"shm://{self.shm_name}",
                "layout": _STREAM_LAYOUT,
            },
        }

    def write_rgb_frame(self, rgba_image: Any) -> None:
        import numpy as np

        rgba_array = np.asarray(rgba_image)
        width = int(rgba_array.shape[1])
        height = int(rgba_array.shape[0])
        rgb_image = rgba_array[:, :, :3] if int(rgba_array.shape[2]) == 4 else rgba_array
        if rgb_image.dtype != np.uint8:
            rgb_image = rgb_image.astype(np.uint8, copy=False)
        if not rgb_image.flags.c_contiguous:
            rgb_image = np.ascontiguousarray(rgb_image)
        frame_view = memoryview(rgb_image).cast("B")
        data_size_bytes = frame_view.nbytes
        if data_size_bytes > self.frame_capacity_bytes:
            raise ValueError(
                f"stream frame size {data_size_bytes} exceeds capacity {self.frame_capacity_bytes}"
            )

        stride_bytes = width * 3
        timestamp_ns = time.time_ns()
        frame_id = self.frame_id + 1
        write_seq = self.seq + 1
        # latest-frame 采用奇偶 seq 协议:
        # 先写奇数 seq 表示“写入中”，写完 frame_data 后再写偶数 seq 表示“当前帧可读”。
        self._write_header(
            seq=write_seq,
            width=width,
            height=height,
            stride_bytes=stride_bytes,
            data_size_bytes=data_size_bytes,
            timestamp_ns=timestamp_ns,
            frame_id=frame_id,
        )
        self.buffer[_HEADER_SIZE : _HEADER_SIZE + data_size_bytes] = frame_view
        publish_seq = write_seq + 1
        self._write_header(
            seq=publish_seq,
            width=width,
            height=height,
            stride_bytes=stride_bytes,
            data_size_bytes=data_size_bytes,
            timestamp_ns=timestamp_ns,
            frame_id=frame_id,
        )
        self.seq = publish_seq
        self.frame_id = frame_id
        self.last_timestamp_ns = timestamp_ns
        self.resolution = (width, height)

    def mark_error(self) -> None:
        self.status = "error"

    def mark_stopped(self) -> None:
        self.status = "stopped"

    def close(self) -> None:
        try:
            self.buffer.release()
        except ValueError:
            pass
        self.shm.close()
        try:
            self.shm.unlink()
        except FileNotFoundError:
            pass

    def _write_header(
        self,
        *,
        seq: int,
        width: int,
        height: int,
        stride_bytes: int,
        data_size_bytes: int,
        timestamp_ns: int,
        frame_id: int,
    ) -> None:
        header = _HEADER_STRUCT.pack(
            _STREAM_MAGIC,
            _encode_ascii_fixed(_STREAM_LAYOUT, 16),
            int(seq),
            int(width),
            int(height),
            int(stride_bytes),
            int(data_size_bytes),
            int(self.frame_capacity_bytes),
            int(timestamp_ns),
            int(frame_id),
            _encode_ascii_fixed(self.pixel_format, 16),
        )
        self.buffer[:_HEADER_SIZE] = header


def create_camera_stream_runtime_state(
    *,
    stream_id: str,
    ref_id: str,
    camera_id: str,
    resolution: Sequence[int],
) -> CameraStreamRuntimeState:
    width = int(resolution[0])
    height = int(resolution[1])
    frame_capacity_bytes = width * height * 3
    shm = shared_memory.SharedMemory(
        name=_build_shared_memory_name(stream_id),
        create=True,
        size=_HEADER_SIZE + frame_capacity_bytes,
    )
    return CameraStreamRuntimeState(
        stream_id=stream_id,
        ref_id=ref_id,
        camera_id=camera_id,
        shm=shm,
        resolution=(width, height),
        frame_capacity_bytes=frame_capacity_bytes,
    )


def latest_frame_header_size_bytes() -> int:
    return _HEADER_SIZE


def decode_latest_frame_header(header_bytes: bytes | bytearray | memoryview) -> dict[str, Any]:
    (
        magic,
        layout,
        seq,
        width,
        height,
        stride_bytes,
        data_size_bytes,
        frame_capacity_bytes,
        timestamp_ns,
        frame_id,
        pixel_format,
    ) = _HEADER_STRUCT.unpack(bytes(header_bytes[:_HEADER_SIZE]))
    return {
        "magic": magic.rstrip(b"\x00").decode("ascii", errors="ignore"),
        "layout": layout.rstrip(b"\x00").decode("ascii", errors="ignore"),
        "seq": int(seq),
        "width": int(width),
        "height": int(height),
        "stride_bytes": int(stride_bytes),
        "data_size_bytes": int(data_size_bytes),
        "frame_capacity_bytes": int(frame_capacity_bytes),
        "timestamp_ns": int(timestamp_ns),
        "frame_id": int(frame_id),
        "pixel_format": pixel_format.rstrip(b"\x00").decode("ascii", errors="ignore"),
    }


def _build_shared_memory_name(stream_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]+", "_", stream_id).strip("_").lower()
    if not sanitized:
        sanitized = "stream"
    suffix = format(time.time_ns() & 0xFFFFFFFF, "08x")
    max_base_len = max(1, _SHARED_MEMORY_NAME_MAX_LEN - len(suffix) - 1)
    return f"{sanitized[:max_base_len]}_{suffix}"


def _encode_ascii_fixed(value: str, size: int) -> bytes:
    encoded = value.encode("ascii", errors="ignore")[:size]
    return encoded.ljust(size, b"\x00")

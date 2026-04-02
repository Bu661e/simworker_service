from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from simworker.handlers import CommandDispatcher
from simworker.protocol import UnixSocketControlServer
from simworker.runtime import WorkerRuntime


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--control-socket-path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    runtime: WorkerRuntime | None = None
    try:
        runtime = WorkerRuntime.bootstrap(Path(args.session_dir))
        dispatcher = CommandDispatcher(runtime)
        with UnixSocketControlServer(Path(args.control_socket_path), runtime.logger) as server:
            # 控制面保持同步请求/同步响应；是否退出由 runtime 状态统一决定。
            server.serve(
                handle_request=dispatcher.handle,
                should_stop=lambda: runtime.shutdown_requested,
                idle_callback=runtime.publish_camera_stream_frames_if_due,
            )
    except Exception:
        if runtime is None:
            print("Failed to bootstrap simworker runtime", file=sys.stderr)
        else:
            runtime.logger.exception("Simworker process exited with an error")
        return 1
    finally:
        if runtime is not None:
            # 无论正常 shutdown 还是异常退出，都在这里做最终资源回收。
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

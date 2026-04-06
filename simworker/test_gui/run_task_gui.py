from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from simworker.runtime import WorkerRuntime


def _default_session_dir() -> Path:
    return REPO_ROOT / "simworker" / "test_gui" / "runs"


class GuiWorkerRuntime(WorkerRuntime):
    def _bootstrap_simulation_app(self) -> object:
        try:
            from isaacsim import SimulationApp
        except ImportError as exc:
            raise RuntimeError("isaacsim is required") from exc

        self.logger.info("Bootstrapping Isaac Sim SimulationApp in GUI mode")
        return SimulationApp({"headless": False})


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch Isaac Sim in GUI mode, optionally load a tabletop scene, "
            "and execute a task code file against the current scene objects."
        )
    )
    parser.add_argument(
        "--session-dir",
        default=str(_default_session_dir()),
        help="Directory used to store worker logs and artifacts",
    )
    parser.add_argument(
        "--table-env",
        default="default",
        help="Scene to load: base|empty|none|default|multi|multi_geometry|ycb",
    )
    parser.add_argument(
        "--objects-file",
        help="JSON file containing the run_task objects list; if omitted, objects are read from the loaded scene",
    )
    parser.add_argument(
        "--code-file",
        required=True,
        help="Python file defining run(robot, objects)",
    )
    parser.add_argument(
        "--task-id",
        help="Task id passed into runtime.run_task(); defaults to the code file stem",
    )
    parser.add_argument(
        "--close-on-complete",
        action="store_true",
        help="Close Isaac Sim automatically after the task completes",
    )
    return parser


def _normalize_table_env_id(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in {"base", "empty", "none"}:
        return None
    if normalized == "multi":
        return "multi_geometry"
    if normalized in {"default", "multi_geometry", "ycb"}:
        return normalized
    raise ValueError(
        "unsupported --table-env value: "
        f"{value!r}; expected one of base|empty|none|default|multi|multi_geometry|ycb"
    )


def _load_objects_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"objects file is not valid JSON: {path}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"objects file must contain a JSON list: {path}")
    return payload


def _load_task_objects(runtime: WorkerRuntime, *, objects_file: Path | None) -> list[dict[str, Any]]:
    if objects_file is not None:
        return _load_objects_from_file(objects_file)

    payload = runtime.build_table_env_objects_payload()
    scene_objects = payload.get("objects")
    if not isinstance(scene_objects, list):
        raise RuntimeError("runtime returned invalid scene objects payload")
    return scene_objects


def _read_code_file(path: Path) -> str:
    code_text = path.read_text(encoding="utf-8")
    if not code_text.strip():
        raise ValueError(f"code file is empty: {path}")
    return code_text


def _run_gui_loop(runtime: WorkerRuntime) -> None:
    simulation_app = runtime.simulation_app
    world = runtime.world
    if simulation_app is None or world is None:
        raise RuntimeError("runtime is not initialized")

    runtime.ensure_world_playing()
    while simulation_app.is_running():
        world.step(render=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    session_dir = Path(args.session_dir).expanduser().resolve()
    objects_file = Path(args.objects_file).expanduser().resolve() if args.objects_file else None
    code_file = Path(args.code_file).expanduser().resolve()
    task_id = args.task_id or code_file.stem
    table_env_id = _normalize_table_env_id(args.table_env)

    runtime: WorkerRuntime | None = None
    try:
        runtime = GuiWorkerRuntime.bootstrap(session_dir)
        runtime.logger.info("GUI runner started")
        runtime.logger.info("Session dir: %s", session_dir)
        runtime.logger.info("Task code file: %s", code_file)
        if objects_file is not None:
            runtime.logger.info("Task objects file: %s", objects_file)

        if table_env_id is None:
            runtime.logger.info("Keeping base environment only; no table environment will be loaded")
        else:
            runtime.logger.info("Loading table environment: %s", table_env_id)
            runtime.load_table_env(table_env_id)

        task_objects = _load_task_objects(runtime, objects_file=objects_file)
        runtime.logger.info("Resolved task objects count: %s", len(task_objects))

        task_code = _read_code_file(code_file)
        task_result = runtime.run_task(task_id=task_id, task_code=task_code, task_objects=task_objects)
        runtime.logger.info("Task finished: %s", json.dumps(task_result, ensure_ascii=False))

        if args.close_on_complete:
            return 0

        runtime.logger.info("Task completed. Isaac Sim GUI will stay open until the window is closed.")
        _run_gui_loop(runtime)
        return 0
    except KeyboardInterrupt:
        if runtime is not None:
            runtime.logger.info("Interrupted by user")
        return 130
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

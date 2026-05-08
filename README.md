# simworker_service

`simworker_service` 是一层很薄的 FastAPI 服务，用来管理唯一一个 Isaac Sim worker 进程。
当前仓库的重点不是做通用调度平台，而是提供一个单用户、单 worker、便于调试的仿真控制入口。

## 设计边界

- 只支持单用户、单 worker
- API 层尽量薄，只负责 HTTP 编排和响应包装
- 仿真控制主逻辑收敛在 `simworker/` 和 `SimManager`
- 单帧采集返回 JSON 元数据和下载链接，不直接回传压缩包
- 视频流接口固定为 `MJPEG`

## 仓库结构

- [`api/`](api/)：FastAPI 应用入口和 MJPEG streaming 实现
- [`simworker/`](simworker/)：`SimManager`、worker 入口、运行时、桌面环境和机器人 API
- [`tests/`](tests/)：API 层测试
- [`simworker/tests/`](simworker/tests/)：`SimWorker` 和 `SimManager` 真实集成测试

## 相关文档

- [`simworker_service.md`](simworker_service.md)：API 层设计文档
- [`simworker/sim_manager_introduction.md`](simworker/sim_manager_introduction.md)：`SimManager` API 文档
- [`tests/README.md`](tests/README.md)：API 测试说明
- [`simworker/tests/README.md`](simworker/tests/README.md)：worker / manager 集成测试说明
- [`simworker/test_gui/README.md`](simworker/test_gui/README.md)：GUI 手工联调说明

## 环境准备

当前 Python 版本固定为 `3.10`，依赖和虚拟环境统一使用 `uv` 管理。

安装运行依赖：

```bash
uv sync
```

安装测试依赖：

```bash
uv sync --group dev
```

之后所有命令都建议通过 `uv run` 执行。

如果要真实拉起 Isaac Sim worker，还需要可用的 Isaac Sim 解释器。默认路径是：

```text
/root/isaacsim/python.sh
```

## 启动服务

FastAPI 入口在 [`api/main.py`](api/main.py)。

常用环境变量：

- `SIMWORKER_CONTROL_SOCKET_PATH`：worker 控制 socket 路径，默认 `/tmp/simworker/control.sock`
- `SIMWORKER_PYTHON_BIN`：启动 worker 的 Python 解释器，默认 `/root/isaacsim/python.sh`
- `SIMWORKER_SESSION_DIR`：worker session 根目录；不传时使用 `simworker/runs/`
- `SIMWORKER_STARTUP_TIMEOUT_SEC`：worker 启动超时
- `SIMWORKER_REQUEST_TIMEOUT_SEC`：单次请求超时
- `SIMWORKER_SHUTDOWN_TIMEOUT_SEC`：worker 关闭超时

启动命令：

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 18080
```

开发模式：

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 18080 --reload
```

说明：

- 应用默认会在启动阶段调用一次 `SimManager.ensure_started()`，所以首次启动可能会等待 Isaac Sim worker 就绪。
- 如果只是改 API 层代码而不想在启动时真实拉起 worker，可以在测试里通过 `create_app(..., start_manager_on_startup=False)` 关闭这一步。

## API 概览

当前对外接口大致分为四类：

- 基础状态：`GET /health`
- 相机能力：`GET /cameras`、`POST /cameras/{camera_id}/capture`、`GET /cameras/{camera_id}/stream`
- 桌面环境：`GET /table-envs`、`PUT /table-env/current/{table_env_id}`、`DELETE /table-env/current`、`GET /table-env/current/objects`
- 机器人与 worker：`GET /robot/status`、`GET /robot/api`、`POST /robot/tasks`、`POST /worker/restart`

返回约定：

- 除文件下载和 MJPEG 流接口外，其余接口都返回 JSON
- 正常响应统一带 `ok: true`
- 错误响应统一带 `ok: false` 和 `error_message`

更完整的接口定义见 [`simworker_service.md`](simworker_service.md)。

## 测试

默认建议显式加 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，避免机器上的外部 `pytest` 插件污染环境。

运行 API 层测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest tests -q
```

只跑 API 主测试文件：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest tests/test_api_app.py -q
```

运行 `simworker` 集成测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
uv run pytest simworker/tests -q
```

运行 FastAPI + 真实 Isaac Sim worker 非流式集成测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
uv run pytest tests/test_api_app.py::test_fastapi_real_integration_exercises_non_stream_interfaces -q
```

运行 FastAPI + 真实 Isaac Sim worker 的 MJPEG 拉流测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
uv run pytest tests/test_api_app.py::test_fastapi_real_integration_streams_mjpeg_frames -q
```

说明：

- `tests/` 里的默认测试不要求真实 GPU 环境。
- `simworker/tests/` 和 API 真实集成测试会真实拉起 Isaac Sim worker，需要 GPU 和 Isaac Sim 环境可用。
- 更细的测试目录、产物落盘位置和单测说明，分别见 [`tests/README.md`](tests/README.md) 和 [`simworker/tests/README.md`](simworker/tests/README.md)。

# simworker_service

## Python 环境

本仓库使用 `uv` 管理 Python 环境，当前版本固定为 Python `3.10`。

初始化环境：

```bash
uv sync
```

之后所有命令都建议通过 `uv run` 执行。

## FastAPI 启动

当前 FastAPI 入口文件是 [api/main.py](/root/simworker_service/api/main.py)。

启动命令：

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 18080
```

开发模式可使用：

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 18080 --reload
```

安装测试依赖：

```bash
uv sync --group dev
```

运行 API 骨架单元测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest tests/test_api_app.py -q
```

运行 FastAPI + 真实 Isaac Sim worker 集成测试：

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

- 这里显式加 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，是为了避免当前机器上的外部 ROS `pytest` 插件污染测试环境。
- 第二条命令会真实启动 Isaac Sim worker，需要 GPU 环境可用。
- 当前视频流方案已经固定为 `MJPEG`。

## 相关文档

- [simworker_service.md](/root/simworker_service/simworker_service.md)
- [simworker/sim_manager_introduction.md](/root/simworker_service/simworker/sim_manager_introduction.md)

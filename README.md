# simworker_service

## Python 环境

本仓库使用 `uv` 管理 Python 环境，当前版本固定为 Python `3.10`。

初始化环境：

```bash
uv sync
```

之后所有命令都建议通过 `uv run` 执行。

## FastAPI 启动

当前仓库已经完成 `uv` 环境初始化，但还没有正式落具体的 FastAPI app 入口文件。

当 FastAPI 入口准备好之后，启动命令使用下面的形式：

```bash
uv run uvicorn <python_module>:app --host 0.0.0.0 --port 8000
```

开发模式可使用：

```bash
uv run uvicorn <python_module>:app --host 0.0.0.0 --port 8000 --reload
```

例如，如果后续入口文件是 `api/main.py`，并且其中定义了 `app = FastAPI()`，则启动命令为：

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## 相关文档

- [isaacsim_service_v0.md](/root/simworker_service/isaacsim_service_v0.md)
- [simworker/sim_manager_introduction.md](/root/simworker_service/simworker/sim_manager_introduction.md)
